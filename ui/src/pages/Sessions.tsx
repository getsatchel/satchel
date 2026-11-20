import { useState, type ReactNode } from "react";
import { Link, Navigate, useNavigate, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { endpoints } from "@/api/endpoints";
import { EmptyState, ErrorBox, Feedback, FilterSelect, ICONS, Loading, PageHeader, PillButton, SectionHeading, Segmented, StatusDot, TableCard, type Tone } from "@/components/ui";
import { pageIcon } from "@/components/nav";
import { fullTime, timeAgo } from "@/lib/format";
import { useRole } from "@/lib/role";

type View = "questions" | "conversations";
const LIMIT = 50;

const statusTone = (status: string): Tone => (status === "answered" ? "good" : status === "error" ? "bad" : "muted");

/** What one table row shows, whether it is a question (a turn) or a whole conversation (a session). */
interface Row {
  key: string;
  to: string;
  question: string | null;
  /** "2 of 3" for a follow-up question, "3 questions" for a conversation; absent when there is only one. */
  note: string | null;
  status: string;
  client: string | null;
  user: string | null;
  sensitive: boolean;
  calls: number;
  signals: number;
  up: number;
  down: number;
  startedAt: string;
}

/** Sessions: every question agents were asked. Usage moved to Observability. */
export function Sessions() {
  const [sp] = useSearchParams();
  const { can } = useRole();
  const curator = can("curator");
  if (sp.get("tab") === "usage") {
    const q = new URLSearchParams(sp);
    q.delete("tab");
    const rest = q.toString();
    return <Navigate to={`/observability${rest ? `?${rest}` : ""}`} replace />;
  }
  const sub = curator
    ? "Every question an agent was asked through Satchel. Open a session to replay its tool calls and the context it was served."
    : can("analyst")
      ? "Questions people in this workspace asked their agents, and the answers. Open one to see the queries it ran and the context it used."
      : "Questions you asked your agent, and the answers. Open one to see the queries it ran and the context it used.";
  return (
    <>
      <PageHeader title="Sessions" sub={sub} />
      <SessionList />
    </>
  );
}

function SessionList() {
  const [sp, setSp] = useSearchParams();
  const client = sp.get("client") ?? "";
  const status = sp.get("status") ?? "";
  const view: View = sp.get("view") === "conversations" ? "conversations" : "questions";
  const { me, can } = useRole();
  // Explorers only ever see their own; analysts and up choose between everyone and themselves.
  const team = can("analyst");
  const mine = !team || sp.get("who") === "me";
  // ?user= (from Observability → Adoption): one person's questions.
  const person = team && !mine ? (sp.get("user") ?? undefined) : undefined;
  const user_id = team && mine ? (me.data?.user_id ?? undefined) : person;
  const [page, setPage] = useState(0);
  const paging = { client, status, user_id, limit: LIMIT, offset: page * LIMIT };
  const turns = useQuery({
    queryKey: ["turns", client, status, user_id ?? null, page],
    queryFn: () => endpoints.listTurns(paging),
    placeholderData: (p) => p,
    enabled: view === "questions",
  });
  const sessions = useQuery({
    queryKey: ["sessions", client, status, user_id, page],
    queryFn: () => endpoints.listSessions(paging),
    placeholderData: (p) => p,
    enabled: view === "conversations",
  });
  const all = useQuery({ queryKey: ["sessions", "clients"], queryFn: () => endpoints.listSessions({ limit: 200 }) });
  const clients = [...new Set((all.data?.sessions ?? []).map((s) => s.client).filter((c): c is string => !!c))];
  // A question is open or answered; a conversation can also have ended in an error.
  const statuses = view === "questions" ? ["active", "answered"] : [...new Set((all.data?.sessions ?? []).map((s) => s.status))];

  function set(k: string, v: string, drop: string[] = []) {
    setPage(0);
    setSp((prev) => {
      const n = new URLSearchParams(prev);
      if (v) n.set(k, v);
      else n.delete(k);
      for (const d of drop) n.delete(d);
      return n;
    });
  }

  const q = view === "questions" ? turns : sessions;
  const total = q.data?.total;
  const rows: Row[] | undefined =
    view === "questions"
      ? turns.data?.turns.map((t) => ({
          key: `t${t.id}`,
          to: t.turn_count > 1 ? `/sessions/${t.session_id}?turn=${t.seq}` : `/sessions/${t.session_id}`,
          question: t.question,
          note: t.turn_count > 1 ? `Question ${t.seq} of ${t.turn_count} in its conversation` : null,
          status: t.status,
          client: t.client,
          user: t.user_label,
          sensitive: t.sensitive,
          calls: t.tool_call_count,
          signals: t.signal_count,
          up: t.thumbs_up,
          down: t.thumbs_down,
          startedAt: t.started_at,
        }))
      : sessions.data?.sessions.map((s) => ({
          key: s.id,
          to: `/sessions/${s.id}`,
          question: s.question,
          note: s.turn_count > 1 ? `${s.turn_count} questions` : null,
          status: s.status,
          client: s.client,
          user: s.user_label,
          sensitive: s.sensitive,
          calls: s.tool_call_count,
          signals: s.signal_count,
          up: s.thumbs_up,
          down: s.thumbs_down,
          startedAt: s.started_at,
        }));

  return (
    <>
      <SectionHeading
        icon={ICONS.pulse}
        title={view === "questions" ? "Questions" : "Conversations"}
        count={total}
      />
      {person && (
        <p className="mb-3 text-sm text-muted">
          One person's questions.{" "}
          <button type="button" className="text-accent hover:underline" onClick={() => set("user", "")}>
            Show everyone
          </button>
        </p>
      )}
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <Segmented
          label="Group by"
          value={view}
          // The two views have different statuses, so a status filter does not carry over.
          onChange={(v) => set("view", v === "questions" ? "" : v, ["status"])}
          options={[
            { id: "questions", label: "Questions" },
            { id: "conversations", label: "Conversations" },
          ]}
        />
        {team && me.data?.user_id && (
          <Segmented
            label="Whose questions"
            value={mine ? "me" : "all"}
            onChange={(v) => set("who", v === "me" ? "me" : "")}
            options={[
              { id: "all", label: "Everyone" },
              { id: "me", label: "Mine" },
            ]}
          />
        )}
        <div className="grid w-full min-w-0 grid-cols-2 items-center gap-2 sm:flex sm:w-auto sm:flex-wrap">
          <FilterSelect label="Client" value={client} onChange={(v) => set("client", v)} options={clients} />
          <FilterSelect label="Status" value={status} onChange={(v) => set("status", v)} options={statuses} />
        </div>
      </div>
      {q.isPending && <Loading />}
      {q.isError && <ErrorBox error={q.error} />}
      {rows && rows.length === 0 && (
        <EmptyState
          title={mine ? "You haven't asked anything yet." : "No sessions yet."}
          muted="Connect an agent and ask it a data question."
          icon={pageIcon("/sessions")}
          action={<PillButton to="/dashboard">How to connect</PillButton>}
        >
          Each question asked from Claude or any other MCP client shows up here.
        </EmptyState>
      )}
      {rows && rows.length > 0 && (
        <SessionTable
          rows={rows}
          footer={
            <div className="flex items-center justify-between border-t border-line px-3 py-2 text-xs text-muted">
              <button className="btn btn-sm" disabled={page === 0} onClick={() => setPage((p) => p - 1)}>
                Previous
              </button>
              <span className="tabular-nums">
                {page * LIMIT + 1}-{Math.min((page + 1) * LIMIT, total ?? 0)} of {total}
              </span>
              <button className="btn btn-sm" disabled={(page + 1) * LIMIT >= (total ?? 0)} onClick={() => setPage((p) => p + 1)}>
                Next
              </button>
            </div>
          }
        />
      )}
    </>
  );
}

function SessionTable({ rows, footer }: { rows: Row[]; footer: ReactNode }) {
  const nav = useNavigate();
  return (
    <TableCard footer={footer}>
      <thead className="border-b border-line">
        <tr>
          <th className="th w-full">Question</th>
          <th className="th hidden sm:table-cell">Status</th>
          <th className="th hidden md:table-cell">Client</th>
          <th className="th hidden lg:table-cell">User</th>
          <th className="th hidden text-right sm:table-cell">Calls</th>
          <th className="th hidden text-right xl:table-cell">Signals</th>
          <th className="th hidden text-right md:table-cell">Feedback</th>
          <th className="th">Started</th>
        </tr>
      </thead>
      <tbody className="divide-y divide-line">
        {rows.map((r) => (
          <tr key={r.key} className="cursor-pointer hover:bg-sunken/70" onClick={() => nav(r.to)}>
            <td className="td min-w-[12rem]">
              <Link to={r.to} className="line-clamp-2 font-medium hover:text-accent" onClick={(e) => e.stopPropagation()}>
                {r.question ?? <span className="font-normal text-muted">(no question recorded)</span>}
              </Link>
              <span className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted sm:hidden">
                <StatusDot tone={statusTone(r.status)}>{r.status}</StatusDot>
                <span>{[r.client, r.user].filter(Boolean).join(" · ")}</span>
              </span>
              <span className="mt-0.5 hidden text-xs text-muted md:max-lg:block">{r.user}</span>
              {r.note && <span className="mt-0.5 block text-xs text-muted">{r.note}</span>}
              {r.sensitive && <span className="badge badge-warn mt-1">sensitive</span>}
            </td>
            <td className="td hidden whitespace-nowrap sm:table-cell">
              <StatusDot tone={statusTone(r.status)}>{r.status}</StatusDot>
            </td>
            <td className="td hidden whitespace-nowrap md:table-cell">{r.client ?? "-"}</td>
            <td className="td hidden whitespace-nowrap text-muted lg:table-cell">{r.user ?? "-"}</td>
            <td className="td hidden text-right tabular-nums sm:table-cell">{r.calls}</td>
            <td className="td hidden text-right tabular-nums xl:table-cell">{r.signals}</td>
            <td className="td hidden text-right md:table-cell">
              <Feedback up={r.up} down={r.down} />
            </td>
            <td className="td whitespace-nowrap text-muted" title={fullTime(r.startedAt)}>
              {timeAgo(r.startedAt)}
            </td>
          </tr>
        ))}
      </tbody>
    </TableCard>
  );
}
