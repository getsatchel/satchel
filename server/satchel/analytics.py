"""Workspace analytics for the Observability page.

One pass over the questions (session turns), tool calls, feedback and signals of a period and
the period before it gives: headline numbers with their previous value, answer quality, adoption,
grounding, performance, and how closely agents follow Satchel's workflow. Eval sessions are left
out, as everywhere else in observability.
"""

from __future__ import annotations

import math
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import Integer, case, func, select
from sqlalchemy.orm import Session

from satchel.api.schemas import (
    Adherence,
    AdherenceBreach,
    AdherenceClient,
    AdherenceRule,
    Adoption,
    Analytics,
    ClientCount,
    ClientQuality,
    ConnectionPerf,
    DayClientQuestions,
    DayValue,
    Grounding,
    Headline,
    NameCount,
    NegativeFeedback,
    Performance,
    PersonStat,
    Quality,
    Redaction,
    SlowCall,
    ToolPerf,
    UnservedItem,
    WeekFeedback,
    WeekPeople,
)
from satchel.curation.redact import truncate
from satchel.store.models import (
    AppUser,
    ContextItem,
    ContextVersion,
    Feedback,
    ServedContext,
    SessionTurn,
    Signal,
    ToolCall,
)
from satchel.store.models import Session as SessionRow
from satchel.store.scopes import session_not_eval

DATA_TOOLS = {"run_sql", "query_metrics"}
CONTEXT_TOOLS = {"get_context_pack", "search_context"}
METRIC_TOOLS = {"list_metrics", "describe_metric", "query_metrics"}

# The workflow every agent is told to follow (the MCP server instructions). Each rule says
# which questions (or sessions, or calls) it applies to and when it was followed.
RULES: dict[str, str] = {
    "started": "Started the session with start_analysis",
    "context_first": "Read context before querying data",
    "metrics_first": "Checked governed metrics before writing SQL",
    "reported": "Finished the question with report_answer",
    "in_question": "Passed the session and question on every call",
}


@dataclass
class _Turn:
    id: int
    session_id: uuid.UUID
    seq: int
    question: str | None
    answered: bool
    at: datetime
    user_id: uuid.UUID | None
    client: str
    sensitive: bool


@dataclass
class _Call:
    id: int
    session_id: uuid.UUID
    turn_id: int | None
    tool: str
    at: datetime
    latency_ms: int | None
    error: bool


def _rate(n: int, d: int) -> float | None:
    return round(n / d, 4) if d else None


def _pct(values: list[int], q: float) -> int | None:
    if not values:
        return None
    v = sorted(values)
    return v[max(0, math.ceil(q * len(v)) - 1)]


def _week(d: datetime) -> date:
    day = d.date()
    return day - timedelta(days=day.weekday())


def _text(level: Redaction, value: str | None) -> str | None:
    if value is None or level == "none":
        return value
    return truncate(value) if level == "redacted" else None


def _turn_rules(t: _Turn, turn_calls: list[_Call], session_calls: list[_Call]) -> dict[str, bool]:
    """The workflow rules that apply to one question, and whether each was followed."""
    tools = {c.tool for c in turn_calls}
    out: dict[str, bool] = {}
    data = [c for c in turn_calls if c.tool in DATA_TOOLS]
    if data:
        first = min(c.at for c in data)
        # Context read earlier in the session counts: follow-up questions reuse it.
        out["context_first"] = any(c.tool in CONTEXT_TOOLS and c.at <= first for c in session_calls)
    sql = [c for c in turn_calls if c.tool == "run_sql"]
    if sql:
        first_sql = min(c.at for c in sql)
        out["metrics_first"] = any(
            c.tool in METRIC_TOOLS or c.tool == "get_context_pack"
            for c in session_calls if c.at <= first_sql and c.tool != "run_sql"
        )
    if tools - {"start_analysis"}:
        out["reported"] = "report_answer" in tools
    return out


class _Data:
    """Everything the analytics read, loaded once for both periods."""

    def __init__(self, s: Session, now: datetime, days: int) -> None:
        self.now = now
        self.since = now - timedelta(days=days)
        self.prev = self.since - timedelta(days=days)
        self.turns = [
            _Turn(tid, sid, seq, q, status == "answered", at, uid, client or "unknown", bool(sens))
            for tid, sid, seq, q, status, at, uid, client, sens in s.execute(
                select(SessionTurn.id, SessionTurn.session_id, SessionTurn.seq,
                       SessionTurn.question, SessionTurn.status, SessionTurn.started_at,
                       SessionRow.user_id, SessionRow.client, SessionRow.sensitive)
                .join(SessionRow, SessionRow.id == SessionTurn.session_id)
                .where(SessionTurn.started_at >= self.prev, session_not_eval())
            )
        ]
        self.sessions_started = {
            sid: at for sid, at in s.execute(
                select(SessionRow.id, SessionRow.started_at)
                .where(SessionRow.started_at >= self.prev, session_not_eval())
            )
        }
        self.calls = [
            _Call(cid, sid, tid, tool, at, lat, err is not None)
            for cid, sid, tid, tool, at, lat, err in s.execute(
                select(ToolCall.id, ToolCall.session_id, ToolCall.turn_id, ToolCall.tool,
                       ToolCall.created_at, ToolCall.latency_ms, ToolCall.error)
                .join(SessionRow, SessionRow.id == ToolCall.session_id)
                .where(ToolCall.created_at >= self.prev, session_not_eval())
                .order_by(ToolCall.created_at, ToolCall.id)
            )
        ]
        self.calls_by_session: dict[uuid.UUID, list[_Call]] = defaultdict(list)
        self.calls_by_turn: dict[int, list[_Call]] = defaultdict(list)
        for c in self.calls:
            self.calls_by_session[c.session_id].append(c)
            if c.turn_id is not None:
                self.calls_by_turn[c.turn_id].append(c)
        self.feedback = list(s.execute(
            select(Feedback.rating, Feedback.created_at, Feedback.session_id, Feedback.comment,
                   Feedback.source, SessionRow.client, SessionRow.sensitive, SessionRow.user_id)
            .join(SessionRow, SessionRow.id == Feedback.session_id)
            .where(Feedback.created_at >= self.prev, session_not_eval())
            .order_by(Feedback.created_at.desc())
        ))
        self.doubts = list(s.execute(
            select(Signal.session_id, Signal.turn_id, Signal.created_at)
            .join(SessionRow, SessionRow.id == Signal.session_id)
            .where(Signal.kind == "user_doubt", Signal.created_at >= self.prev,
                   session_not_eval())
        ))
        self.gaps = s.scalar(
            select(func.count()).select_from(Signal)
            .join(SessionRow, SessionRow.id == Signal.session_id)
            .where(Signal.kind == "gap_flagged", Signal.created_at >= self.since,
                   session_not_eval())
        ) or 0
        # Arguments only for the calls whose arguments the analytics read.
        self.answers = [
            (tid, args) for tid, args in s.execute(
                select(ToolCall.turn_id, ToolCall.args)
                .join(SessionRow, SessionRow.id == ToolCall.session_id)
                .where(ToolCall.tool == "report_answer", ToolCall.created_at >= self.since,
                       session_not_eval())
            )
        ]
        self.metric_queries = [
            args for (args,) in s.execute(
                select(ToolCall.args)
                .join(SessionRow, SessionRow.id == ToolCall.session_id)
                .where(ToolCall.tool == "query_metrics", ToolCall.created_at >= self.since,
                       session_not_eval())
            )
        ]
        first_seen = {
            uid: at for uid, at in s.execute(
                select(SessionRow.user_id, func.min(SessionRow.started_at))
                .where(SessionRow.user_id.is_not(None), session_not_eval())
                .group_by(SessionRow.user_id)
            )
        }
        self.first_seen: dict[uuid.UUID, datetime] = {
            u: at for u, at in first_seen.items() if u is not None}
        self.users = {
            uid: name or email or "unknown"
            for uid, name, email in s.execute(
                select(AppUser.id, AppUser.display_name, AppUser.email)
            )
        }
        # From the data tool calls: query results themselves expire and are pruned.
        conn = func.coalesce(ToolCall.result_meta["connection"].astext, "unknown")
        self.queries = list(s.execute(
            select(conn, func.count(),
                   func.sum(case((ToolCall.result_meta["truncated"].astext == "true", 1),
                                 else_=0)),
                   func.avg(ToolCall.result_meta["rows"].astext.cast(Integer)),
                   func.sum(case((ToolCall.error.is_not(None), 1), else_=0)))
            .join(SessionRow, SessionRow.id == ToolCall.session_id)
            .where(ToolCall.tool.in_(DATA_TOOLS), ToolCall.created_at >= self.since,
                   session_not_eval())
            .group_by(conn)
            .order_by(func.count().desc())
        ))
        served_ids = (
            select(ContextVersion.item_id)
            .join(ServedContext, ServedContext.version_id == ContextVersion.id)
            .join(ToolCall, ToolCall.id == ServedContext.tool_call_id)
            .where(ToolCall.created_at >= self.since)
        )
        unserved = (
            select(ContextItem.key, ContextItem.type)
            .where(ContextItem.state == "published", ContextItem.id.not_in(served_ids))
        )
        self.unserved_count = s.scalar(select(func.count()).select_from(unserved.subquery())) or 0
        self.unserved = list(s.execute(unserved.order_by(ContextItem.key).limit(10)))

    def turns_in(self, current: bool) -> list[_Turn]:
        if current:
            return [t for t in self.turns if t.at >= self.since]
        return [t for t in self.turns if self.prev <= t.at < self.since]

    def calls_in(self, current: bool) -> list[_Call]:
        if current:
            return [c for c in self.calls if c.at >= self.since]
        return [c for c in self.calls if self.prev <= c.at < self.since]


def _headline_values(d: _Data, current: bool) -> dict[str, float | None]:
    turns = d.turns_in(current)
    calls = d.calls_in(current)
    fb = [f for f in d.feedback if (f.created_at >= d.since) == current]
    up = sum(1 for f in fb if f.rating > 0)
    down = sum(1 for f in fb if f.rating < 0)
    answered = [t for t in turns if t.answered]
    governed = sql_only = 0
    for t in answered:
        tools = {c.tool for c in d.calls_by_turn.get(t.id, [])}
        if "query_metrics" in tools:
            governed += 1
        elif "run_sql" in tools:
            sql_only += 1
    applicable = followed = 0
    for t in turns:
        rules = _turn_rules(t, d.calls_by_turn.get(t.id, []),
                            d.calls_by_session.get(t.session_id, []))
        if rules:
            applicable += 1
            followed += all(rules.values())
    return {
        "questions": len(turns),
        "people": len({t.user_id for t in turns if t.user_id}),
        "answered_rate": _rate(len(answered), len(turns)),
        "thumbs_up_rate": _rate(up, up + down),
        "governed_rate": _rate(governed, governed + sql_only),
        "adherence_rate": _rate(followed, applicable),
        "p95_latency_ms": _pct([c.latency_ms for c in calls if c.latency_ms is not None], 0.95),
    }


def compute(s: Session, days: int, level: Redaction, now: datetime | None = None) -> Analytics:
    """All the analytics for the last ``days`` days. ``level`` redacts question and comment text
    for people who may not read it in full (sensitive sessions show none)."""
    d = _Data(s, now or datetime.now(UTC), days)
    cur, prev = _headline_values(d, True), _headline_values(d, False)
    headline = Headline(**{k: {"value": cur[k], "previous": prev[k]} for k in cur})
    turns = d.turns_in(True)
    calls = d.calls_in(True)
    fb = [f for f in d.feedback if f.created_at >= d.since]

    # ---- quality
    weeks: dict[date, list[int]] = defaultdict(lambda: [0, 0])
    by_client: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    for t in turns:
        by_client[t.client][0] += 1
    for f in fb:
        w = weeks[_week(f.created_at)]
        cl = by_client[f.client or "unknown"]
        if f.rating > 0:
            w[0] += 1
            cl[1] += 1
        elif f.rating < 0:
            w[1] += 1
            cl[2] += 1
    answered = [t for t in turns if t.answered]
    doubted = {(r.turn_id or r.session_id) for r in d.doubts if r.created_at >= d.since}
    reports = [a for _, a in d.answers]
    quality = Quality(
        per_week=[WeekFeedback(week=w, up=u, down=n) for w, (u, n) in sorted(weeks.items())],
        by_client=[ClientQuality(client=c, questions=q, up=u, down=n)
                   for c, (q, u, n) in sorted(by_client.items(), key=lambda x: -x[1][0])],
        doubt_rate=_rate(len(doubted), len(answered)),
        low_confidence_rate=_rate(sum(1 for a in reports if a.get("confidence") == "low"),
                                  len(reports)),
        caveat_rate=_rate(sum(1 for a in reports if a.get("caveats")), len(reports)),
        recent_negative=[
            NegativeFeedback(
                session_id=f.session_id, client=f.client, source=f.source, at=f.created_at,
                comment=_text("metadata_only" if f.sensitive and level != "none" else level,
                              f.comment),
            )
            for f in fb if f.rating < 0
        ][:10],
    )

    # ---- adoption
    per_day: Counter[tuple[date, str]] = Counter((t.at.date(), t.client) for t in turns)
    week_people: dict[date, set[uuid.UUID]] = defaultdict(set)
    people: dict[uuid.UUID, dict[str, Any]] = {}
    for t in turns:
        if not t.user_id:
            continue
        week_people[_week(t.at)].add(t.user_id)
        p = people.setdefault(t.user_id, {"questions": 0, "last": t.at, "up": 0, "down": 0})
        p["questions"] += 1
        p["last"] = max(p["last"], t.at)
    for f in fb:
        if f.user_id in people:
            people[f.user_id]["up" if f.rating > 0 else "down"] += 1
    new = sum(1 for u in people if d.first_seen.get(u, d.now) >= d.since)
    adoption = Adoption(
        per_day=[DayClientQuestions(day=k[0], client=k[1], questions=n)
                 for k, n in sorted(per_day.items())],
        people_per_week=[WeekPeople(week=w, people=len(u)) for w, u in sorted(week_people.items())],
        clients=[ClientCount(client=c, questions=n)
                 for c, n in Counter(t.client for t in turns).most_common()],
        new_people=new,
        returning_people=len(people) - new,
        people=sorted(
            (PersonStat(user_id=u, label=d.users.get(u, "unknown"), questions=p["questions"],
                        last_active=p["last"], up=p["up"], down=p["down"],
                        new=d.first_seen.get(u, d.now) >= d.since)
             for u, p in people.items()),
            key=lambda p: (-p.questions, p.label),
        )[:50],
    )

    # ---- grounding
    governed = sql_only = no_data = with_context = 0
    for t in answered:
        tc = d.calls_by_turn.get(t.id, [])
        tools = {c.tool for c in tc}
        if "query_metrics" in tools:
            governed += 1
        elif "run_sql" in tools:
            sql_only += 1
        else:
            no_data += 1
        done = max((c.at for c in tc if c.tool == "report_answer"), default=None)
        if done and any(c.tool in CONTEXT_TOOLS and c.at <= done
                        for c in d.calls_by_session.get(t.session_id, [])):
            with_context += 1
    metrics: Counter[str] = Counter()
    for a in d.metric_queries:
        for m in a.get("metrics") or []:
            metrics[str(m)] += 1
    tables: Counter[str] = Counter(
        str(x) for a in reports for x in (a.get("tables_used") or [])
    )
    grounding = Grounding(
        answered=len(answered), governed=governed, sql_only=sql_only, no_data=no_data,
        with_context=with_context, gaps_flagged=d.gaps,
        top_metrics=[NameCount(name=k, count=n) for k, n in metrics.most_common(10) if n],
        top_tables=[NameCount(name=k, count=n) for k, n in tables.most_common(10)],
        unserved_count=d.unserved_count,
        unserved=[UnservedItem(key=k, type=t) for k, t in d.unserved],
    )

    # ---- performance
    by_tool: dict[str, list[_Call]] = defaultdict(list)
    by_day: dict[date, list[int]] = defaultdict(list)
    for c in calls:
        by_tool[c.tool].append(c)
        if c.latency_ms is not None:
            by_day[c.at.date()].append(c.latency_ms)
    performance = Performance(
        tools=sorted(
            (ToolPerf(tool=t, calls=len(cs), errors=sum(c.error for c in cs),
                      p50_ms=_pct([c.latency_ms for c in cs if c.latency_ms is not None], 0.5),
                      p95_ms=_pct([c.latency_ms for c in cs if c.latency_ms is not None], 0.95))
             for t, cs in by_tool.items()),
            key=lambda x: (-x.calls, x.tool),
        ),
        p95_per_day=[DayValue(day=k, value=_pct(v, 0.95)) for k, v in sorted(by_day.items())],
        connections=[ConnectionPerf(connection=n, queries=q, truncated=int(tr or 0),
                                    errors=int(e or 0),
                                    avg_rows=float(r) if r is not None else None)
                     for n, q, tr, r, e in d.queries],
        slowest=[
            SlowCall(session_id=c.session_id, tool=c.tool, latency_ms=c.latency_ms or 0, at=c.at)
            for c in sorted((c for c in calls if c.latency_ms is not None),
                            key=lambda c: -(c.latency_ms or 0))[:5]
        ],
    )

    # ---- adherence
    counts: dict[str, list[int]] = {k: [0, 0] for k in RULES}
    client_counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    breaches: list[AdherenceBreach] = []
    for t in sorted(turns, key=lambda t: t.at, reverse=True):
        rules = _turn_rules(t, d.calls_by_turn.get(t.id, []),
                            d.calls_by_session.get(t.session_id, []))
        for k, ok in rules.items():
            counts[k][0] += 1
            counts[k][1] += ok
        if rules:
            client_counts[t.client][0] += 1
            client_counts[t.client][1] += all(rules.values())
        missed = [k for k, ok in rules.items() if not ok]
        if missed and len(breaches) < 10:
            breaches.append(AdherenceBreach(
                session_id=t.session_id, seq=t.seq, client=t.client, at=t.at, rules=missed,
                question=_text("metadata_only" if t.sensitive and level != "none" else level,
                               t.question),
            ))
    for sid, started in d.sessions_started.items():
        sc = d.calls_by_session.get(sid, [])
        if started >= d.since and sc:
            counts["started"][0] += 1
            counts["started"][1] += sc[0].tool == "start_analysis"
    counts["in_question"] = [len(calls), sum(1 for c in calls if c.turn_id is not None)]
    adherence = Adherence(
        rules=[AdherenceRule(rule=k, label=RULES[k], applies=a, followed=f)
               for k, (a, f) in counts.items()],
        by_client=[AdherenceClient(client=c, questions=a, followed=f)
                   for c, (a, f) in sorted(client_counts.items(), key=lambda x: -x[1][0])],
        recent=breaches,
    )
    return Analytics(days=days, headline=headline, quality=quality, adoption=adoption,
                     grounding=grounding, performance=performance, adherence=adherence)
