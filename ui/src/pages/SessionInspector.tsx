import { useState } from "react";
import { useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { endpoints } from "@/api/endpoints";
import type { SessionDetail, ToolCall } from "@/api/types";
import { CodeEditor, Pre } from "@/components/Code";
import { useToast } from "@/components/Toast";
import { Crumb, ErrorBox, Icon, ICONS, ItemLink, Loading, Notice, PageHeader, PillButton, SectionHeading, StatCell, StatGrid, StatusDot } from "@/components/ui";
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

      <div className="card mb-9 overflow-hidden">
        <StatGrid>
          <StatCell label="Tool calls" glyph="ring" value={s.timeline.length} info="Tool calls the agent made in this session." />
          <StatCell label="Errors" tone="bad" glyph="triangle" value={s.timeline.filter((c) => c.has_error).length} info="Tool calls that returned an error." />
          <StatCell label="Signals" tone="warn" glyph="square" value={s.signals.length} info="Gaps, errors and doubts flagged in this session." />
          <StatCell label="Feedback" tone="good" value={`${s.feedback.filter((f) => f.rating > 0).length} / ${s.feedback.filter((f) => f.rating < 0).length}`} secondary="up / down" info="Thumbs recorded on this session's results." />
        </StatGrid>
      </div>

      <div className="grid items-start gap-9 lg:grid-cols-[minmax(0,1fr)_320px]">
        <section className="min-w-0" aria-label="Tool call timeline">
          <SectionHeading icon={ICONS.list} title="Timeline" count={s.timeline.length} />
          {s.timeline.length === 0 ? (
            <p className="text-sm text-muted">No tool calls were recorded.</p>
          ) : (
            <ol className="relative space-y-3 border-l border-line pl-5">
              {s.timeline.map((c) => (
                <Call
                  key={c.id}
                  c={c}
                  offsetMs={new Date(c.created_at).getTime() - t0}
                  redaction={s.redaction}
                  ratings={s.feedback.filter((f) => f.tool_call_id === c.id && f.source === "human")}
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
                    {g.item_keys.length > 0 && (
                      <div className="mt-1 flex flex-wrap gap-1.5">
                        {g.item_keys.map((k) => (
                          <ItemLink key={k} itemKey={k} />
                        ))}
                      </div>
                    )}
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

function Call({
  c,
  offsetMs,
  redaction,
  ratings,
}: {
  c: ToolCall;
  offsetMs: number;
  redaction: SessionDetail["redaction"];
  ratings: SessionDetail["feedback"];
}) {
  const sql = c.args && typeof c.args["sql"] === "string" ? (c.args["sql"] as string) : null;
  const [all, setAll] = useState(false);
  const served = all ? c.served : c.served.slice(0, 6);
  const rest = c.args ? Object.fromEntries(Object.entries(c.args).filter(([k]) => !(sql && k === "sql"))) : null;
  return (
    <li className="relative">
      <span
        className={`absolute -left-[25px] top-2 h-2.5 w-2.5 rounded-full border-2 border-bg ${c.has_error ? "bg-bad" : "bg-accent"}`}
        aria-hidden
      />
      <div className="card min-w-0">
        <div className="flex flex-wrap items-center gap-3 border-b border-line px-4 py-2.5">
          <span className="break-all font-mono text-sm font-semibold">{c.tool}</span>
          {c.has_error && <span className="badge badge-bad">error</span>}
          {ratings.map((f) => (
            <span key={f.id} className={`badge ${f.rating > 0 ? "badge-good" : "badge-bad"}`} title={f.comment ?? undefined}>
              <Icon d={f.rating > 0 ? ICONS.thumbUp : ICONS.thumbDown} size={11} strokeWidth={2} /> human via widget{f.client ? ` · ${f.client}` : ""}
            </span>
          ))}
          <span className="ml-auto whitespace-nowrap mono text-xs text-muted">
            +{ms(offsetMs)} · {ms(c.latency_ms)}
          </span>
        </div>
        <div className="space-y-3 p-4">
          {c.error && (
            <p role="alert" className="rounded border border-bad/30 bg-bad-soft px-2.5 py-1.5 font-mono text-xs text-bad">
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
          {Object.keys(c.result_meta).length > 0 && (
            <details>
              <summary className="cursor-pointer text-xs text-muted">Result metadata</summary>
              <Pre>{pretty(c.result_meta)}</Pre>
            </details>
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
        </div>
      </div>
    </li>
  );
}
