import type { ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { endpoints } from "@/api/endpoints";
import type { Analytics, AnalyticsDelta, ObservabilitySummary } from "@/api/types";
import {
  EmptyState,
  ErrorBox,
  Feedback,
  ICONS,
  ItemLink,
  Loading,
  Meter,
  PageHeader,
  SectionHeading,
  Segmented,
  StatCell,
  StatGrid,
  TableCard,
  Tabs,
  useWide,
} from "@/components/ui";
import { fullTime, ms, timeAgo } from "@/lib/format";
import { useTheme } from "@/lib/theme";

// Thumbs up and down use the good and bad tones (DESIGN.md), not series colours.
const TONES = { light: { up: "#1c7446", down: "#be3226" }, dark: { up: "#6ac48a", down: "#f08070" } };

// Series colours: cobalt (the accent) first, then hues spaced around it at similar weight.
export const PALETTE = {
  light: ["#2f45e8", "#1f8a8a", "#b7791f", "#7a4fd0", "#5b6472", "#c2417a"],
  dark: ["#8796ff", "#5cc4c4", "#e0a84e", "#b394f2", "#9aa3b0", "#f07aa8"],
};

type Tab = "overview" | "quality" | "adoption" | "grounding" | "performance" | "adherence";
const TABS: { id: Tab; label: string }[] = [
  { id: "overview", label: "Overview" },
  { id: "quality", label: "Quality" },
  { id: "adoption", label: "Adoption" },
  { id: "grounding", label: "Grounding" },
  { id: "performance", label: "Performance" },
  { id: "adherence", label: "Adherence" },
];

export const pct = (v: number | null | undefined) => (v == null ? "–" : `${Math.round(v * 100)}%`);

/** "+12% vs previous 30 days": a count's relative change, a rate's change in points. */
export function change(d: AnalyticsDelta, kind: "count" | "rate", days: number): string | undefined {
  // No earlier data (a new workspace, or nothing to compare): say nothing rather than repeat it on every tile.
  if (d.value == null || d.previous == null) return undefined;
  if (kind === "rate") {
    const pts = Math.round((d.value - d.previous) * 100);
    return `${pts > 0 ? "+" : ""}${pts} pts vs previous ${days} days`;
  }
  if (d.previous === 0) return undefined;
  const rel = Math.round(((d.value - d.previous) / d.previous) * 100);
  return `${rel > 0 ? "+" : ""}${rel}% vs previous ${days} days`;
}

/** Observability: how agents use Satchel, how good and how grounded the answers are, and how fast. */
export function Observability() {
  const [sp, setSp] = useSearchParams();
  const days = [7, 90].find((d) => String(d) === sp.get("days")) ?? 30;
  const tab = TABS.find((t) => t.id === sp.get("tab"))?.id ?? "overview";
  const set = (key: string, value: string | null) =>
    setSp(
      (prev) => {
        const n = new URLSearchParams(prev);
        if (value === null) n.delete(key);
        else n.set(key, value);
        return n;
      },
      { replace: true },
    );
  const q = useQuery({ queryKey: ["analytics", days], queryFn: () => endpoints.analytics(days), placeholderData: (p) => p });

  return (
    <>
      <PageHeader
        title="Observability"
        sub="Who asks agents about your data, how good and how grounded the answers are, and whether agents follow Satchel's workflow."
      />
      <Tabs tabs={TABS} value={tab} onChange={(t) => set("tab", t === "overview" ? null : t)} />
      <div className="mb-6 mt-6">
        <Segmented
          label="Time range"
          value={days}
          onChange={(d) => set("days", d === 30 ? null : String(d))}
          options={[
            { id: 7, label: "7 days" },
            { id: 30, label: "30 days" },
            { id: 90, label: "90 days" },
          ]}
        />
      </div>
      <div>
        {q.isPending && <Loading />}
        {q.isError && <ErrorBox error={q.error} />}
        {q.data && q.data.headline.questions.value === 0 && tab !== "grounding" ? (
          <EmptyState title="No questions in this period." muted="Connect Claude or another MCP client and ask a data question.">
            These pages fill in as agents use Satchel. Setup steps are on Home and in the docs (integrations/claude).
          </EmptyState>
        ) : (
          q.data && (
            <div className="space-y-9">
              {tab === "overview" && <OverviewTab a={q.data} />}
              {tab === "quality" && <QualityTab a={q.data} />}
              {tab === "adoption" && <AdoptionTab a={q.data} />}
              {tab === "grounding" && <GroundingTab a={q.data} />}
              {tab === "performance" && <PerformanceTab a={q.data} />}
              {tab === "adherence" && <AdherenceTab a={q.data} />}
            </div>
          )
        )}
      </div>
    </>
  );
}

// ---- shared parts --------------------------------------------------------------------------

function Section({ title, icon, children, actions }: { title: string; icon: string; children: ReactNode; actions?: ReactNode }) {
  return (
    <section aria-label={title}>
      <SectionHeading icon={icon} title={title} actions={actions} />
      {children}
    </section>
  );
}

/** Stacked bars over time: one row per day or week, one series per key. */
function Bars({ rows, keys, x, label, fills }: { rows: Record<string, number | string>[]; keys: string[]; x: string; label: string; fills?: Record<string, string> }) {
  const [theme] = useTheme();
  const colors = PALETTE[theme];
  const wide = useWide("(min-width: 640px)");
  if (rows.length === 0) return <div className="card p-4 text-sm text-muted">Nothing in this period.</div>;
  return (
    <div className="card h-60 p-4 sm:h-72" role="img" aria-label={label}>
      <ResponsiveContainer>
        <BarChart data={rows} margin={{ top: 4, right: 8, left: wide ? -20 : -12, bottom: 0 }}>
          <CartesianGrid vertical={false} />
          <XAxis dataKey={x} tickFormatter={(d: string) => d.slice(5)} tickLine={false} />
          <YAxis allowDecimals={false} tickLine={false} axisLine={false} />
          <Tooltip cursor={{ fill: "rgb(128 128 128 / 0.12)" }} />
          {keys.length > 1 && <Legend iconType="square" iconSize={9} wrapperStyle={{ fontSize: 12 }} />}
          {keys.map((k, i) => (
            <Bar key={k} dataKey={k} stackId="s" fill={fills?.[k] ?? colors[i % colors.length]} maxBarSize={36} />
          ))}
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

/** Pivot `[{day, client, n}]` into chart rows keyed by client. */
function pivot<T>(items: T[], x: (t: T) => string, key: (t: T) => string, n: (t: T) => number) {
  const keys = [...new Set(items.map(key))].sort();
  const rows = new Map<string, Record<string, number | string>>();
  for (const t of items) {
    const row = rows.get(x(t)) ?? { x: x(t) };
    row[key(t)] = ((row[key(t)] as number) ?? 0) + n(t);
    rows.set(x(t), row);
  }
  return { keys, rows: [...rows.values()].sort((a, b) => String(a.x).localeCompare(String(b.x))) };
}

function SessionLink({ id, children }: { id: string; children: ReactNode }) {
  return (
    <Link className="text-accent hover:underline" to={`/sessions/${id}`}>
      {children}
    </Link>
  );
}

function When({ at }: { at: string }) {
  return (
    <span className="whitespace-nowrap text-muted" title={fullTime(at)}>
      {timeAgo(at)}
    </span>
  );
}

function Empty({ children }: { children: ReactNode }) {
  return <div className="card p-4 text-sm text-muted">{children}</div>;
}

// ---- tabs ----------------------------------------------------------------------------------

function OverviewTab({ a }: { a: Analytics }) {
  const h = a.headline;
  const summary = useQuery({ queryKey: ["summary", a.days], queryFn: () => endpoints.summary(a.days) });
  const q = pivot(a.adoption.per_day, (d) => d.day, (d) => d.client, (d) => d.questions);
  return (
    <>
      <div className="card overflow-hidden">
        <StatGrid>
          <StatCell label="Questions" tone="info" value={(h.questions.value ?? 0).toLocaleString()} secondary={change(h.questions, "count", a.days)} info="Questions agents worked on." />
          <StatCell label="People" value={(h.people.value ?? 0).toLocaleString()} secondary={change(h.people, "count", a.days)} info="People whose agents asked at least one question." />
          <StatCell label="Thumbs up" tone="good" value={pct(h.thumbs_up_rate.value)} secondary={change(h.thumbs_up_rate, "rate", a.days)} info="Share of thumbs that were up, from people and agents." />
          <StatCell label="Answered" value={pct(h.answered_rate.value)} secondary={change(h.answered_rate, "rate", a.days)} info="Questions that ended with report_answer." />
        </StatGrid>
        <div className="border-t border-line">
          <StatGrid>
            <StatCell label="Governed" value={pct(h.governed_rate.value)} secondary={change(h.governed_rate, "rate", a.days)} info="Data answers that used a governed metric rather than only raw SQL." to="/observability?tab=grounding" />
            <StatCell label="Adherence" value={pct(h.adherence_rate.value)} secondary={change(h.adherence_rate, "rate", a.days)} info="Questions that followed every workflow rule that applied to them." to="/observability?tab=adherence" />
            <StatCell label="p95 latency" glyph="ring" value={ms(h.p95_latency_ms.value)} secondary={change(h.p95_latency_ms, "count", a.days)} info="95th percentile of tool call latency." to="/observability?tab=performance" />
            <StatCell label="Gaps flagged" tone="warn" glyph="triangle" value={a.grounding.gaps_flagged.toLocaleString()} info="Times an agent said the context did not cover what it needed." to="/inbox" />
          </StatGrid>
        </div>
      </div>
      <Section title="Questions per day, by client" icon={ICONS.layers}>
        <Bars rows={q.rows} keys={q.keys} x="x" label="Questions per day by client" />
      </Section>
      {summary.isError && <ErrorBox error={summary.error} />}
      {summary.data && <SignalsAndServed s={summary.data} />}
    </>
  );
}

function SignalsAndServed({ s }: { s: ObservabilitySummary }) {
  const [theme] = useTheme();
  const colors = PALETTE[theme];
  const wide = useWide("(min-width: 640px)");
  const maxServed = Math.max(1, ...s.most_served_items.map((t) => t.served));
  return (
    <div className="grid gap-9 xl:grid-cols-2">
      <Section title="Signals by kind" icon={ICONS.alert}>
        {s.signals_by_kind.length === 0 ? (
          <Empty>No signals in this period.</Empty>
        ) : (
          <div className="card p-4" style={{ height: Math.max(120, s.signals_by_kind.length * 30 + 52) }}>
            <ResponsiveContainer>
              <BarChart data={s.signals_by_kind} layout="vertical" margin={{ top: 0, right: 12, left: 0, bottom: 0 }}>
                <CartesianGrid horizontal={false} />
                <XAxis type="number" allowDecimals={false} tickLine={false} />
                <YAxis
                  type="category"
                  dataKey="kind"
                  width={wide ? 130 : 96}
                  tickLine={false}
                  tick={{ fontSize: 11 }}
                  tickFormatter={(k: string) => (!wide && k.length > 14 ? `${k.slice(0, 13)}…` : k)}
                />
                <Tooltip cursor={{ fill: "rgb(128 128 128 / 0.12)" }} />
                <Bar dataKey="count" fill={colors[0]} radius={[0, 2, 2, 0]} barSize={14} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        )}
      </Section>
      <Section title="Most-served items" icon={ICONS.book}>
        {s.most_served_items.length === 0 ? (
          <Empty>No context was served in this period.</Empty>
        ) : (
          <TableCard>
            <thead className="border-b border-line">
              <tr>
                <th className="th w-full">Item</th>
                <th className="th hidden sm:table-cell" />
                <th className="th text-right">Served</th>
                <th className="th hidden text-right sm:table-cell">Feedback</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {s.most_served_items.map((i) => (
                <tr key={i.key}>
                  <td className="td">
                    <ItemLink itemKey={i.key} />
                  </td>
                  <td className="td hidden w-28 sm:table-cell">
                    <Meter value={(i.served / maxServed) * 100} label={`${i.key} share of served`} />
                  </td>
                  <td className="td text-right tabular-nums">{i.served}</td>
                  <td className="td hidden text-right sm:table-cell">
                    <Feedback up={i.thumbs_up} down={i.thumbs_down} />
                  </td>
                </tr>
              ))}
            </tbody>
          </TableCard>
        )}
      </Section>
    </div>
  );
}

function QualityTab({ a }: { a: Analytics }) {
  const [theme] = useTheme();
  const qa = a.quality;
  const h = a.headline;
  return (
    <>
      <div className="card overflow-hidden">
        <StatGrid>
          <StatCell label="Thumbs up" tone="good" value={pct(h.thumbs_up_rate.value)} secondary={change(h.thumbs_up_rate, "rate", a.days)} info="Share of thumbs that were up." />
          <StatCell label="Doubted" tone="warn" value={pct(qa.doubt_rate)} info="Answered questions that a person doubted or corrected." />
          <StatCell label="Low confidence" value={pct(qa.low_confidence_rate)} info="Answers the agent reported with low confidence." />
          <StatCell label="With caveats" value={pct(qa.caveat_rate)} info="Answers the agent reported with at least one caveat." />
        </StatGrid>
      </div>
      <Section title="Thumbs per week" icon={ICONS.thumbUp}>
        <Bars rows={qa.per_week.map((w) => ({ x: w.week, up: w.up, down: w.down }))} keys={["up", "down"]} x="x" label="Thumbs up and down per week" fills={TONES[theme]} />
      </Section>
      <Section title="By client" icon={ICONS.layers}>
        <TableCard>
          <thead className="border-b border-line">
            <tr>
              <th className="th w-full">Client</th>
              <th className="th text-right">Questions</th>
              <th className="th text-right">Feedback</th>
              <th className="th text-right">Up</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {qa.by_client.map((c) => (
              <tr key={c.client}>
                <td className="td font-mono">{c.client}</td>
                <td className="td text-right tabular-nums">{c.questions}</td>
                <td className="td text-right">
                  <Feedback up={c.up} down={c.down} />
                </td>
                <td className="td text-right tabular-nums">{c.up + c.down ? pct(c.up / (c.up + c.down)) : "–"}</td>
              </tr>
            ))}
          </tbody>
        </TableCard>
      </Section>
      <Section title="Recent thumbs down" icon={ICONS.thumbDown}>
        {qa.recent_negative.length === 0 ? (
          <Empty>No thumbs down in this period.</Empty>
        ) : (
          <TableCard>
            <tbody className="divide-y divide-line">
              {qa.recent_negative.map((f) => (
                <tr key={`${f.session_id}${f.at}`}>
                  <td className="td w-full">
                    <SessionLink id={f.session_id}>{f.comment || "No comment"}</SessionLink>
                    <div className="mt-0.5 text-xs text-muted">
                      {f.client ?? "unknown"} · {f.source === "human" ? "from a person" : "relayed by the agent"}
                    </div>
                  </td>
                  <td className="td text-right">
                    <When at={f.at} />
                  </td>
                </tr>
              ))}
            </tbody>
          </TableCard>
        )}
      </Section>
    </>
  );
}

function AdoptionTab({ a }: { a: Analytics }) {
  const ad = a.adoption;
  const h = a.headline;
  const q = pivot(ad.per_day, (d) => d.day, (d) => d.client, (d) => d.questions);
  return (
    <>
      <div className="card overflow-hidden">
        <StatGrid>
          <StatCell label="Questions" tone="info" value={(h.questions.value ?? 0).toLocaleString()} secondary={change(h.questions, "count", a.days)} />
          <StatCell label="People" value={(h.people.value ?? 0).toLocaleString()} secondary={change(h.people, "count", a.days)} />
          <StatCell label="New" tone="good" value={ad.new_people.toLocaleString()} info="People whose first session was in this period." />
          <StatCell label="Returning" value={ad.returning_people.toLocaleString()} info="People who had used Satchel before this period." />
        </StatGrid>
      </div>
      <Section title="Questions per day, by client" icon={ICONS.layers}>
        <Bars rows={q.rows} keys={q.keys} x="x" label="Questions per day by client" />
      </Section>
      <Section title="People per week" icon={ICONS.user}>
        <Bars rows={ad.people_per_week.map((w) => ({ x: w.week, people: w.people }))} keys={["people"]} x="x" label="Active people per week" />
      </Section>
      <Section title="People" icon={ICONS.user}>
        <TableCard>
          <thead className="border-b border-line">
            <tr>
              <th className="th w-full">Person</th>
              <th className="th text-right">Questions</th>
              <th className="th hidden text-right sm:table-cell">Feedback</th>
              <th className="th text-right">Last active</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {ad.people.map((p) => (
              <tr key={p.user_id}>
                <td className="td">
                  <Link className="hover:underline" to={`/sessions?user=${p.user_id}`}>
                    {p.label}
                  </Link>{" "}
                  {p.new && <span className="badge badge-good">new</span>}
                </td>
                <td className="td text-right tabular-nums">{p.questions}</td>
                <td className="td hidden text-right sm:table-cell">
                  <Feedback up={p.up} down={p.down} />
                </td>
                <td className="td text-right">
                  <When at={p.last_active} />
                </td>
              </tr>
            ))}
          </tbody>
        </TableCard>
      </Section>
    </>
  );
}

function NameCounts({ rows, empty, mono = true }: { rows: { name: string; count: number }[]; empty: string; mono?: boolean }) {
  if (rows.length === 0) return <Empty>{empty}</Empty>;
  const max = Math.max(1, ...rows.map((r) => r.count));
  return (
    <TableCard>
      <tbody className="divide-y divide-line">
        {rows.map((r) => (
          <tr key={r.name}>
            <td className={`td w-full [overflow-wrap:anywhere] ${mono ? "font-mono" : ""}`}>{r.name}</td>
            <td className="td hidden w-28 sm:table-cell">
              <Meter value={(r.count / max) * 100} label={`${r.name} share`} />
            </td>
            <td className="td text-right tabular-nums">{r.count}</td>
          </tr>
        ))}
      </tbody>
    </TableCard>
  );
}

function GroundingTab({ a }: { a: Analytics }) {
  const g = a.grounding;
  const share = (n: number) => (g.answered ? pct(n / g.answered) : "–");
  return (
    <>
      <div className="card overflow-hidden">
        <StatGrid>
          <StatCell label="Governed metric" tone="good" value={share(g.governed)} secondary={`${g.governed} of ${g.answered} answers`} info="Answers that used query_metrics, the governed definitions." />
          <StatCell label="Raw SQL only" tone="warn" value={share(g.sql_only)} secondary={`${g.sql_only} answers`} info="Answers that ran run_sql but no governed metric." />
          <StatCell label="Read context" value={share(g.with_context)} secondary={`${g.with_context} answers`} info="Answers whose session read the context pack or searched context before answering." />
          <StatCell label="Gaps flagged" tone="warn" glyph="triangle" value={g.gaps_flagged.toLocaleString()} info="Times an agent said the context did not cover what it needed. They become suggestions in the Inbox." to="/inbox" />
        </StatGrid>
      </div>
      <div className="grid gap-9 xl:grid-cols-2">
        <Section title="Metrics queried" icon={ICONS.pulse}>
          <NameCounts rows={g.top_metrics} empty="No governed metric was queried in this period." />
        </Section>
        <Section title="Tables in answers" icon={ICONS.database}>
          <NameCounts rows={g.top_tables} empty="No answer named a table in this period." />
        </Section>
      </div>
      <Section title={`Never served (${g.unserved_count})`} icon={ICONS.book}>
        {g.unserved.length === 0 ? (
          <Empty>Every published item was served at least once in this period.</Empty>
        ) : (
          <TableCard
            footer={
              g.unserved_count > g.unserved.length && (
                <p className="border-t border-line px-3 py-2.5 text-xs text-muted">
                  {g.unserved_count - g.unserved.length} more. Items no agent needs may be stale, or worded so search can't find them.
                </p>
              )
            }
          >
            <tbody className="divide-y divide-line">
              {g.unserved.map((i) => (
                <tr key={i.key}>
                  <td className="td w-full">
                    <ItemLink itemKey={i.key} />
                  </td>
                  <td className="td">
                    <span className="badge font-mono">{i.type}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </TableCard>
        )}
      </Section>
    </>
  );
}

function PerformanceTab({ a }: { a: Analytics }) {
  const p = a.performance;
  const calls = p.tools.reduce((n, t) => n + t.calls, 0);
  const errors = p.tools.reduce((n, t) => n + t.errors, 0);
  return (
    <>
      <div className="card overflow-hidden">
        <StatGrid>
          <StatCell label="p95 latency" glyph="ring" value={ms(a.headline.p95_latency_ms.value)} secondary={change(a.headline.p95_latency_ms, "count", a.days)} />
          <StatCell label="Tool calls" value={calls.toLocaleString()} />
          <StatCell label="Errors" tone="bad" glyph="triangle" value={errors.toLocaleString()} secondary={calls ? `${pct(errors / calls)} of calls` : undefined} />
          <StatCell label="Warehouse queries" value={p.connections.reduce((n, c) => n + c.queries, 0).toLocaleString()} />
        </StatGrid>
      </div>
      <Section title="p95 latency per day (ms)" icon={ICONS.clock}>
        <Bars rows={p.p95_per_day.map((d) => ({ x: d.day, p95: d.value ?? 0 }))} keys={["p95"]} x="x" label="95th percentile tool latency per day" />
      </Section>
      <Section title="Tools" icon={ICONS.bolt}>
        <TableCard>
          <thead className="border-b border-line">
            <tr>
              <th className="th w-full">Tool</th>
              <th className="th text-right">Calls</th>
              <th className="th text-right">Errors</th>
              <th className="th hidden text-right sm:table-cell">p50</th>
              <th className="th text-right">p95</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {p.tools.map((t) => (
              <tr key={t.tool}>
                <td className="td font-mono [overflow-wrap:anywhere]">{t.tool}</td>
                <td className="td text-right tabular-nums">{t.calls}</td>
                <td className={`td text-right tabular-nums ${t.errors ? "text-bad" : "text-muted"}`}>{t.errors}</td>
                <td className="td hidden text-right font-mono text-muted sm:table-cell">{ms(t.p50_ms)}</td>
                <td className="td text-right font-mono">{ms(t.p95_ms)}</td>
              </tr>
            ))}
          </tbody>
        </TableCard>
      </Section>
      <div className="grid gap-9 xl:grid-cols-2">
        <Section title="Warehouse queries" icon={ICONS.database}>
          {p.connections.length === 0 ? (
            <Empty>No warehouse queries in this period.</Empty>
          ) : (
            <TableCard>
              <thead className="border-b border-line">
                <tr>
                  <th className="th w-full">Connection</th>
                  <th className="th text-right">Queries</th>
                  <th className="th text-right">Errors</th>
                  <th className="th hidden text-right sm:table-cell">Truncated</th>
                  <th className="th hidden text-right sm:table-cell">Avg rows</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {p.connections.map((c) => (
                  <tr key={c.connection}>
                    <td className="td font-mono">{c.connection}</td>
                    <td className="td text-right tabular-nums">{c.queries}</td>
                    <td className={`td text-right tabular-nums ${c.errors ? "text-bad" : "text-muted"}`}>{c.errors}</td>
                    <td className="td hidden text-right tabular-nums sm:table-cell">{c.truncated}</td>
                    <td className="td hidden text-right tabular-nums sm:table-cell">{c.avg_rows == null ? "–" : Math.round(c.avg_rows)}</td>
                  </tr>
                ))}
              </tbody>
            </TableCard>
          )}
        </Section>
        <Section title="Slowest calls" icon={ICONS.clock}>
          {p.slowest.length === 0 ? (
            <Empty>No calls in this period.</Empty>
          ) : (
            <TableCard>
              <tbody className="divide-y divide-line">
                {p.slowest.map((c) => (
                  <tr key={`${c.session_id}${c.at}${c.tool}`}>
                    <td className="td w-full font-mono">
                      <SessionLink id={c.session_id}>{c.tool}</SessionLink>
                    </td>
                    <td className="td text-right font-mono">{ms(c.latency_ms)}</td>
                    <td className="td text-right">
                      <When at={c.at} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </TableCard>
          )}
        </Section>
      </div>
    </>
  );
}

function AdherenceTab({ a }: { a: Analytics }) {
  const ad = a.adherence;
  const label = Object.fromEntries(ad.rules.map((r) => [r.rule, r.label]));
  return (
    <>
      <p className="max-w-3xl text-sm text-muted">
        Measured from the tool calls behind each question, against the workflow Satchel gives every agent. Whether answers follow your own{" "}
        <Link className="text-accent hover:underline" to="/agent">
          Agent instructions
        </Link>{" "}
        (tone, caveats, which metrics to prefer) is not measured yet.
      </p>
      <Section title="Rules" icon={ICONS.check}>
        <TableCard>
          <thead className="border-b border-line">
            <tr>
              <th className="th w-full">Rule</th>
              <th className="th hidden sm:table-cell" />
              <th className="th text-right">Followed</th>
              <th className="th text-right">Of</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {ad.rules.map((r) => (
              <tr key={r.rule}>
                <td className="td">{r.label}</td>
                <td className="td hidden w-32 sm:table-cell">{r.applies > 0 && <Meter value={(r.followed / r.applies) * 100} label={`${r.label}: share followed`} tone={r.followed < r.applies ? "warn" : "accent"} />}</td>
                <td className="td text-right tabular-nums">{r.applies ? pct(r.followed / r.applies) : "–"}</td>
                <td className="td text-right tabular-nums text-muted">{r.applies}</td>
              </tr>
            ))}
          </tbody>
        </TableCard>
      </Section>
      <Section title="By client" icon={ICONS.layers}>
        {ad.by_client.length === 0 ? (
          <Empty>No questions a rule applies to in this period.</Empty>
        ) : (
          <TableCard>
            <thead className="border-b border-line">
              <tr>
                <th className="th w-full">Client</th>
                <th className="th text-right">Questions</th>
                <th className="th text-right">Followed every rule</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {ad.by_client.map((c) => (
                <tr key={c.client}>
                  <td className="td font-mono">{c.client}</td>
                  <td className="td text-right tabular-nums">{c.questions}</td>
                  <td className="td text-right tabular-nums">{pct(c.followed / c.questions)}</td>
                </tr>
              ))}
            </tbody>
          </TableCard>
        )}
      </Section>
      <Section title="Recent misses" icon={ICONS.alert}>
        {ad.recent.length === 0 ? (
          <Empty>Every question in this period followed the workflow.</Empty>
        ) : (
          <TableCard>
            <tbody className="divide-y divide-line">
              {ad.recent.map((b) => (
                <tr key={`${b.session_id}-${b.seq}`}>
                  <td className="td w-full">
                    <SessionLink id={b.session_id}>{b.question || "Question hidden"}</SessionLink>
                    <ul className="mt-1 space-y-0.5 text-xs text-muted">
                      {b.rules.map((r) => (
                        <li key={r}>Missed: {label[r] ?? r}</li>
                      ))}
                    </ul>
                  </td>
                  <td className="td whitespace-nowrap text-right font-mono text-xs text-muted">{b.client}</td>
                  <td className="td text-right">
                    <When at={b.at} />
                  </td>
                </tr>
              ))}
            </tbody>
          </TableCard>
        )}
      </Section>
    </>
  );
}
