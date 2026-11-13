import { useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { endpoints } from "@/api/endpoints";
import { EmptyState, ErrorBox, Feedback, FilterSelect, ICONS, Loading, PageHeader, PillButton, SectionHeading, StatusDot, TableCard } from "@/components/ui";
import { pageIcon } from "@/components/nav";
import { timeAgo } from "@/lib/format";

export function Sessions() {
  const [sp, setSp] = useSearchParams();
  const nav = useNavigate();
  const client = sp.get("client") ?? "";
  const status = sp.get("status") ?? "";
  const [page, setPage] = useState(0);
  const LIMIT = 50;
  const q = useQuery({
    queryKey: ["sessions", client, status, page],
    queryFn: () => endpoints.listSessions({ client, status, limit: LIMIT, offset: page * LIMIT }),
    placeholderData: (p) => p,
  });
  const all = useQuery({ queryKey: ["sessions", "clients"], queryFn: () => endpoints.listSessions({ limit: 200 }) });
  const clients = [...new Set((all.data?.sessions ?? []).map((s) => s.client).filter((c): c is string => !!c))];

  const statuses = [...new Set((all.data?.sessions ?? []).map((s) => s.status))];

  function set(k: string, v: string) {
    setPage(0);
    setSp((prev) => {
      const n = new URLSearchParams(prev);
      if (v) n.set(k, v);
      else n.delete(k);
      return n;
    });
  }

  return (
    <>
      <PageHeader title="Sessions" sub="One row per analysis an agent ran against Satchel. Open one to replay the tool calls and the context it was served." />
      <SectionHeading icon={ICONS.pulse} title="Sessions" count={q.data?.total} actions={<PillButton to="/observability">Observability</PillButton>} />
      <div className="mb-3 grid grid-cols-2 items-center gap-2 sm:flex sm:flex-wrap">
        <FilterSelect label="Client" value={client} onChange={(v) => set("client", v)} options={clients} />
        <FilterSelect label="Status" value={status} onChange={(v) => set("status", v)} options={statuses} />
      </div>
      {q.isPending && <Loading />}
      {q.isError && <ErrorBox error={q.error} />}
      {q.data && q.data.sessions.length === 0 && (
        <EmptyState title="No sessions yet." muted="Connect an agent and ask it a data question." icon={pageIcon("/sessions")}>
          Each analysis from Claude or any other MCP client shows up here. Setup steps are in docs/integrations/claude.md.
        </EmptyState>
      )}
      {q.data && q.data.sessions.length > 0 && (
        <TableCard
          footer={
            <div className="flex items-center justify-between border-t border-line px-3 py-2 text-xs text-muted">
              <button className="btn btn-sm" disabled={page === 0} onClick={() => setPage((p) => p - 1)}>
                Previous
              </button>
              <span className="tabular-nums">
                {page * LIMIT + 1}-{Math.min((page + 1) * LIMIT, q.data.total)} of {q.data.total}
              </span>
              <button className="btn btn-sm" disabled={(page + 1) * LIMIT >= q.data.total} onClick={() => setPage((p) => p + 1)}>
                Next
              </button>
            </div>
          }
        >
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
            {q.data.sessions.map((s) => {
              const tone = s.status === "answered" ? "good" : s.status === "error" ? "bad" : "muted";
              return (
                <tr key={s.id} className="cursor-pointer hover:bg-sunken/70" onClick={() => nav(`/sessions/${s.id}`)}>
                  <td className="td min-w-[12rem]">
                    <Link to={`/sessions/${s.id}`} className="line-clamp-2 font-medium hover:text-accent" onClick={(e) => e.stopPropagation()}>
                      {s.question ?? <span className="font-normal text-muted">(no question recorded)</span>}
                    </Link>
                    <span className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted sm:hidden">
                      <StatusDot tone={tone}>{s.status}</StatusDot>
                      <span>{[s.client, s.user_label].filter(Boolean).join(" · ")}</span>
                    </span>
                    <span className="mt-0.5 hidden text-xs text-muted md:max-lg:block">{s.user_label}</span>
                    {s.sensitive && <span className="badge badge-warn mt-1">sensitive</span>}
                  </td>
                  <td className="td hidden whitespace-nowrap sm:table-cell">
                    <StatusDot tone={tone}>{s.status}</StatusDot>
                  </td>
                  <td className="td hidden whitespace-nowrap md:table-cell">{s.client ?? "-"}</td>
                  <td className="td hidden whitespace-nowrap text-muted lg:table-cell">{s.user_label ?? "-"}</td>
                  <td className="td hidden text-right tabular-nums sm:table-cell">{s.tool_call_count}</td>
                  <td className="td hidden text-right tabular-nums xl:table-cell">{s.signal_count}</td>
                  <td className="td hidden text-right md:table-cell">
                    <Feedback up={s.thumbs_up} down={s.thumbs_down} />
                  </td>
                  <td className="td whitespace-nowrap text-muted">{timeAgo(s.started_at)}</td>
                </tr>
              );
            })}
          </tbody>
        </TableCard>
      )}
    </>
  );
}
