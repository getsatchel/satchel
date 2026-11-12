import { useState, type ReactNode } from "react";
import { useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { endpoints } from "@/api/endpoints";
import type { SessionDetail, ToolCall } from "@/api/types";
import { CodeEditor, Pre } from "@/components/Code";
import { useToast } from "@/components/Toast";
import { Chevron, Crumb, ErrorBox, Icon, ICONS, ItemLink, Loading, Notice, PageHeader, PillButton, SectionHeading, StatusDot } from "@/components/ui";
import { errMsg, fullTime, ms, pretty, timeAgo } from "@/lib/format";

export function SessionInspector() {
  const { id = "" } = useParams();
  const q = useQuery({ queryKey: ["session", id], queryFn: () => endpoints.getSession(id) });
  const me = useQuery({ queryKey: ["me"], queryFn: endpoints.me });
  if (q.isPending) return <Loading />;
  if (q.isError) return <ErrorBox error={q.error} />;
  return <Inspector s={q.data} isAdmin={me.data?.role === "admin"} />;
}

function Inspector({ s, isAdmin }: { s: SessionDetail; isAdmin: boolean }) {
  const qc = useQueryClient();
  const toast = useToast();
  const mark = useMutation({
    mutationFn: () => endpoints.setSensitive(s.id, !s.sensitive),
    onSuccess: () => {
      toast("ok", s.sensitive ? "Session is no longer marked sensitive" : "Session marked sensitive");
      void qc.invalidateQueries({ queryKey: ["session", s.id] });
      void qc.invalidateQueries({ queryKey: ["sessions"] });
    },
    onError: (e) => toast("err", errMsg(e)),
  });
  const t0 = s.timeline[0] ? new Date(s.timeline[0].created_at).getTime() : 0;
  const errors = s.timeline.filter((c) => c.has_error).length;
  const [open, setOpen] = useState<Set<number>>(() => new Set());
  const allOpen = s.timeline.length > 0 && open.size === s.timeline.length;
  const toggle = (id: number) =>
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  return (
    <>
      <Crumb to="/sessions" label="Sessions" rest="Session" />
      <PageHeader
        title={s.question ?? "(no question recorded)"}
        sub={
          <span className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <span>{s.client ?? "unknown client"}</span>
            <span>{s.user_label ?? "unknown user"}</span>
            <span title={fullTime(s.started_at)}>{timeAgo(s.started_at)}</span>
            <StatusDot tone={s.status === "answered" ? "good" : s.status === "error" ? "bad" : "muted"}>{s.status}</StatusDot>
            {s.release_seq != null && <span className="mono">release #{s.release_seq}</span>}
            {s.sensitive && <span className="badge badge-warn">sensitive</span>}
          </span>
        }
        actions={
          isAdmin ? (
            <button className="btn" disabled={mark.isPending} onClick={() => mark.mutate()}>
              {s.sensitive ? "Unmark sensitive" : "Mark sensitive"}
            </button>
          ) : undefined
        }
      />
      {s.redaction !== "none" && (
        <div className="mb-4">
          <Notice tone="warn">
            {s.redaction === "metadata_only"
              ? "This session is marked sensitive. Only metadata is visible to you: tool names, timings and the items served. Arguments, results and comments are hidden."
              : "Arguments and results are shown redacted: long values are truncated and literals in them are masked. Admins and the session's own user can see the full evidence."}
          </Notice>
        </div>
      )}

      <Answer s={s} />

      <div className="grid items-start gap-9 lg:grid-cols-[minmax(0,1fr)_320px]">
        <section className="min-w-0" aria-label="Tool call timeline">
          <SectionHeading
            icon={ICONS.list}
            title="Timeline"
            count={s.timeline.length}
            actions={
              s.timeline.length > 0 && (
                <>
                  {errors > 0 && <StatusDot tone="bad">{errors === 1 ? "1 error" : `${errors} errors`}</StatusDot>}
                  <button type="button" className="btn btn-ghost text-xs" onClick={() => setOpen(allOpen ? new Set() : new Set(s.timeline.map((c) => c.id)))}>
                    {allOpen ? "Collapse all" : "Expand all"}
                  </button>
                </>
              )
            }
          />
          {s.timeline.length === 0 ? (
            <p className="text-sm text-muted">No tool calls were recorded.</p>
          ) : (
            <ol className="card divide-y divide-line overflow-hidden">
              {s.timeline.map((c) => (
                <Call
                  key={c.id}
                  c={c}
                  offsetMs={new Date(c.created_at).getTime() - t0}
                  question={s.question}
                  redaction={s.redaction}
                  ratings={s.feedback.filter((f) => f.tool_call_id === c.id && f.source === "human")}
                  open={open.has(c.id)}
                  onToggle={() => toggle(c.id)}
                />
              ))}
            </ol>
          )}
        </section>

        <aside className="space-y-9 lg:sticky lg:top-5">
          <section aria-label="Signals">
            <SectionHeading icon={ICONS.alert} title="Signals" count={s.signals.length} />
            {s.signals.length === 0 ? (
              <p className="text-sm text-muted">None. Signals are recorded when an agent flags a gap or Satchel detects a SQL error, retry storm or unendorsed table.</p>
            ) : (
              <ul className="space-y-2">
                {s.signals.map((g) => (
                  <li key={g.id} className="card p-3 text-sm">
                    <div className="flex items-center gap-2">
                      <span className="badge badge-warn">{g.kind}</span>
                      <span className="text-xs text-muted">{g.source}</span>
                    </div>
                    {g.item_keys.length > 0 && <SignalItems keys={g.item_keys} />}
                    {g.payload && Object.keys(g.payload).length > 0 && (
                      <details className="mt-1">
                        <summary className="cursor-pointer text-xs text-muted">Payload</summary>
                        <Pre>{pretty(g.payload)}</Pre>
                      </details>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </section>
          <section aria-label="Feedback">
            <SectionHeading icon={ICONS.thumbUp} title="Feedback" count={s.feedback.length} />
            {s.feedback.length === 0 ? (
              <p className="text-sm text-muted">No thumbs yet. Users rate results with the buttons in the result widget, or the agent relays them with record_user_feedback.</p>
            ) : (
              <ul className="space-y-2">
                {s.feedback.map((f) => (
                  <li key={f.id} className="card p-3 text-sm">
                    <span aria-label={f.rating > 0 ? "positive" : "negative"} className={`inline-block align-[-2px] ${f.rating > 0 ? "text-good" : "text-bad"}`}>
                      <Icon d={f.rating > 0 ? ICONS.thumbUp : ICONS.thumbDown} size={14} />
                    </span>{" "}
                    {f.source === "human" && <span className="badge badge-good">human via widget</span>}{" "}
                    <FeedbackText comment={f.comment} />
                    <div className="text-xs text-muted">{timeAgo(f.created_at)}</div>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </aside>
      </div>
    </>
  );
}

/** The items a signal is about: the first few, the rest on request. */
function SignalItems({ keys }: { keys: string[] }) {
  const [all, setAll] = useState(false);
  const shown = all ? keys : keys.slice(0, 3);
  return (
    <div className="mt-1 space-y-0.5">
      {shown.map((k) => (
        <div key={k} className="[overflow-wrap:anywhere]">
          <ItemLink itemKey={k} />
        </div>
      ))}
      {keys.length > 3 && (
        <button type="button" className="text-xs text-muted hover:text-ink" onClick={() => setAll(!all)}>
          {all ? "Show fewer" : `+${keys.length - 3} more`}
        </button>
      )}
    </div>
  );
}

const REASON_LABELS: Record<string, string> = {
  wrong_number: "Wrong number",
  wrong_definition: "Wrong definition",
  missing_filter: "Missing a filter",
  not_what_i_asked: "Not what I asked",
};

/** The widget stores reasons as a "[slug]" prefix on the comment; show them as badges. */
function FeedbackText({ comment }: { comment: string | null }) {
  if (!comment) return <span className="text-muted">no comment</span>;
  const m = /^((?:\[[a-z_]+\])+)\s*([\s\S]*)$/.exec(comment);
  if (!m) return <>{comment}</>;
  const slugs = (m[1] ?? "").slice(1, -1).split("][");
  const note = m[2] ?? "";
  return (
    <>
      {slugs.map((r) => (
        <span key={r} className="badge badge-bad mr-1">{REASON_LABELS[r] ?? r}</span>
      ))}
      {note || <span className="text-muted">no comment</span>}
    </>
  );
}

type Confidence = "high" | "medium" | "low";
const CONFIDENCE_TONE: Record<Confidence, "good" | "muted" | "warn"> = { high: "good", medium: "muted", low: "warn" };

/** What the agent concluded, from its report_answer call: the first thing to read. */
function Answer({ s }: { s: SessionDetail }) {
  const report = [...s.timeline].reverse().find((c) => c.tool === "report_answer");
  const a = report?.args ?? null;
  const summary = typeof a?.["answer_summary"] === "string" ? (a["answer_summary"] as string) : null;
  const confidence = typeof a?.["confidence"] === "string" ? (a["confidence"] as Confidence) : null;
  const caveats = Array.isArray(a?.["caveats"]) ? (a["caveats"] as unknown[]).filter((x): x is string => typeof x === "string") : [];
  const used = [a?.["metrics_used"], a?.["tables_used"]].flatMap((v) => (Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : []));

  let body: ReactNode;
  if (!report) {
    body = (
      <p className="text-sm text-muted">
        {s.status === "error"
          ? "The session ended in an error before the agent reported an answer. The failing call is marked in the timeline."
          : "No answer was reported: the agent never called report_answer, so the session is still open or was abandoned."}
      </p>
    );
  } else if (a === null) {
    body = <p className="text-sm text-muted">The agent reported an answer. Its text is hidden ({s.redaction === "metadata_only" ? "sensitive session" : "redacted"}).</p>;
  } else {
    body = (
      <>
        <div className="flex flex-wrap items-start gap-x-4 gap-y-2">
          <p className="min-w-0 flex-1 text-sm leading-relaxed text-ink">{summary ?? <span className="text-muted">The agent reported an answer without a summary.</span>}</p>
          {confidence && CONFIDENCE_TONE[confidence] && <StatusDot tone={CONFIDENCE_TONE[confidence]}>{confidence} confidence</StatusDot>}
        </div>
        {caveats.length > 0 && (
          <div className="mt-3">
            <div className="label-caps mb-1">Caveats</div>
            <ul className="list-disc space-y-0.5 pl-5 text-sm text-ink">
              {caveats.map((c) => (
                <li key={c}>{c}</li>
              ))}
            </ul>
          </div>
        )}
        {used.length > 0 && (
          <p className="mt-3 text-xs text-muted">
            Used <span className="font-mono">{used.join(", ")}</span>
          </p>
        )}
      </>
    );
  }
  return (
    <section className="mb-9" aria-label="Answer">
      <SectionHeading icon={ICONS.check} title="Answer" />
      <div className="card p-4">{body}</div>
    </section>
  );
}

const GIST_KEYS = ["sql", "query", "question", "metric", "metrics", "table", "item_key", "key", "name"];

/** One line that says what a call was about: its SQL, query, table or metric. */
function gist(c: ToolCall, question: string | null): string | null {
  if (c.tool === "report_answer" || !c.args) return null;
  for (const k of GIST_KEYS) {
    const v = c.args[k];
    const text = typeof v === "string" ? v : Array.isArray(v) && v.every((x) => typeof x === "string") ? v.join(", ") : null;
    if (!text || (k === "question" && text === question)) continue;
    return text.replace(/\s+/g, " ").trim();
  }
  return null;
}

function Call({
  c,
  offsetMs,
  question,
  redaction,
  ratings,
  open,
  onToggle,
}: {
  c: ToolCall;
  offsetMs: number;
  question: string | null;
  redaction: SessionDetail["redaction"];
  ratings: SessionDetail["feedback"];
  open: boolean;
  onToggle: () => void;
}) {
  const sql = c.args && typeof c.args["sql"] === "string" ? (c.args["sql"] as string) : null;
  const [all, setAll] = useState(false);
  const served = all ? c.served : c.served.slice(0, 6);
  // The SQL has its own editor; the session id and the session's question are already on the page.
  const rest = c.args
    ? Object.fromEntries(
        Object.entries(c.args).filter(([k, v]) => !(sql && k === "sql") && k !== "session_id" && !(k === "question" && v === question)),
      )
    : null;
  const line = gist(c, question);
  const bodyId = `call-${c.id}`;
  return (
    <li className="min-w-0">
      <button
        type="button"
        className="flex w-full min-w-0 items-center gap-3 px-4 py-2.5 text-left hover:bg-sunken/70"
        aria-expanded={open}
        aria-controls={bodyId}
        onClick={onToggle}
      >
        <Chevron open={open} />
        <span className={`h-2 w-2 shrink-0 rounded-full ${c.has_error ? "bg-bad" : "bg-accent"}`} aria-hidden />
        <span className="shrink-0 font-mono text-sm font-semibold">{c.tool}</span>
        <span className="min-w-0 flex-1 truncate font-mono text-xs text-muted" title={line ?? undefined}>
          {line}
        </span>
        {c.served.length > 0 && (
          <span className="hidden shrink-0 whitespace-nowrap text-xs text-muted sm:inline">
            {c.served.length} {c.served.length === 1 ? "item" : "items"}
          </span>
        )}
        {c.has_error && <span className="badge badge-bad">error</span>}
        {ratings.map((f) => (
          <span key={f.id} className={`badge ${f.rating > 0 ? "badge-good" : "badge-bad"}`} title={`Rated by a person${f.client ? ` in ${f.client}` : ""}${f.comment ? `: ${f.comment}` : ""}`}>
            <Icon d={f.rating > 0 ? ICONS.thumbUp : ICONS.thumbDown} size={11} strokeWidth={2} />
            <span className="sr-only">{f.rating > 0 ? "rated up" : "rated down"}</span>
          </span>
        ))}
        <span className="shrink-0 whitespace-nowrap font-mono text-xs tabular-nums text-muted" title={`${ms(offsetMs)} after the first call; took ${ms(c.latency_ms)}`}>
          +{ms(offsetMs)} · {ms(c.latency_ms)}
        </span>
      </button>
      {c.error && !open && (
        <p className="truncate px-4 pb-2.5 pl-[3.25rem] font-mono text-xs text-bad" title={c.error}>
          {c.error}
        </p>
      )}
      {open && (
        <div id={bodyId} className="space-y-3 border-t border-line bg-sunken/40 px-4 py-4 sm:pl-[3.25rem]">
          {c.error && (
            <p role="alert" className="rounded border border-bad/30 bg-bad-soft px-2.5 py-1.5 font-mono text-xs text-bad [overflow-wrap:anywhere]">
              {c.error}
            </p>
          )}
          {c.args === null ? (
            <p className="text-xs text-muted">Arguments hidden ({redaction === "metadata_only" ? "sensitive session" : "redacted"}).</p>
          ) : (
            <>
              {sql && (
                <div>
                  <div className="label-caps mb-1">SQL</div>
                  <CodeEditor label="SQL" language="sql" value={sql} readOnly height={Math.min(240, 56 + sql.split("\n").length * 19)} />
                </div>
              )}
              {rest && Object.keys(rest).length > 0 && (
                <div>
                  <div className="label-caps mb-1">Arguments</div>
                  <Pre>{pretty(rest)}</Pre>
                </div>
              )}
            </>
          )}
          {c.served.length > 0 && (
            <div>
              <div className="label-caps mb-1">Context served · {c.served.length}</div>
              <table className="w-full text-sm">
                <thead className="border-b border-line">
                  <tr>
                    <th className="th w-10 !px-0">Rank</th>
                    <th className="th !px-2">Item</th>
                    <th className="th hidden w-16 text-right sm:table-cell">Score</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-line">
                  {served.map((v) => (
                    <tr key={`${v.item_key}@${v.seq}`}>
                      <td className="py-1 align-top font-mono text-muted">{v.rank}</td>
                      <td className="py-1 [overflow-wrap:anywhere]">
                        <ItemLink itemKey={v.item_key} seq={v.seq} />
                      </td>
                      <td className="hidden py-1 text-right font-mono text-muted sm:table-cell">{v.score != null ? v.score.toFixed(3) : "-"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {c.served.length > 6 && (
                <div className="mt-2 flex justify-center">
                  <PillButton onClick={() => setAll(!all)} chevron={false}>
                    {all ? "Show fewer" : `Show all ${c.served.length}`}
                  </PillButton>
                </div>
              )}
            </div>
          )}
          {Object.keys(c.result_meta).length > 0 && (
            <details>
              <summary className="cursor-pointer text-xs text-muted">Result metadata</summary>
              <Pre>{pretty(c.result_meta)}</Pre>
            </details>
          )}
        </div>
      )}
    </li>
  );
}
