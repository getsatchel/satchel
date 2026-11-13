import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { endpoints } from "@/api/endpoints";
import type { JobOut } from "@/api/types";
import { EmptyState, ErrorBox, ICONS, Loading, PageHeader, SectionHeading, Segmented, StatusDot, TableCard, type Tone } from "@/components/ui";
import { useToast } from "@/components/Toast";
import { errMsg, fullTime, timeAgo, timeUntil } from "@/lib/format";

type Filter = "all" | JobOut["status"];

const KIND: Record<string, string> = {
  "source.sync": "Source sync",
  "connection.crawl": "Crawl",
  "curation.run": "Curation",
  "eval.run": "Eval run",
};

/** The state word and its tone. A queued job with an error is waiting to retry. */
function stateOf(j: JobOut): { tone: Tone; word: string } {
  if (j.status === "queued") return j.error ? { tone: "warn", word: "retrying" } : { tone: "muted", word: "queued" };
  if (j.status === "running") return { tone: "info", word: "running" };
  if (j.status === "done") return { tone: "good", word: "done" };
  return { tone: "bad", word: "failed" };
}

function when(j: JobOut): { text: string; title: string } {
  if (j.status === "queued" && j.error && j.run_after) return { text: `retry ${timeUntil(j.run_after)}`, title: fullTime(j.run_after) };
  if (j.finished_at) return { text: timeAgo(j.finished_at), title: `Finished ${fullTime(j.finished_at)}` };
  if (j.started_at) return { text: `started ${timeAgo(j.started_at)}`, title: fullTime(j.started_at) };
  return { text: timeAgo(j.created_at), title: `Queued ${fullTime(j.created_at)}` };
}

export function Jobs() {
  const [filter, setFilter] = useState<Filter>("all");
  const qc = useQueryClient();
  const toast = useToast();
  const q = useQuery({
    queryKey: ["jobs", filter],
    queryFn: () => endpoints.listJobs(filter === "all" ? undefined : filter),
    // Poll while something is still waiting or running.
    refetchInterval: (query) => (query.state.data?.some((j) => j.status === "queued" || j.status === "running") ? 5000 : false),
  });
  const retry = useMutation({
    mutationFn: (id: number) => endpoints.retryJob(id),
    onSuccess: (j) => {
      toast("ok", `Queued ${KIND[j.kind] ?? j.kind}${j.subject ? ` of ${j.subject}` : ""} again`);
      void qc.invalidateQueries({ queryKey: ["jobs"] });
    },
    onError: (e) => toast("err", errMsg(e)),
  });

  // Counts for the tabs come from the unfiltered list (the same query as the All tab).
  const everything = useQuery({ queryKey: ["jobs", "all"], queryFn: () => endpoints.listJobs(), enabled: filter !== "all" });
  const counted = filter === "all" ? q.data : everything.data;
  const rows = q.data ?? [];
  const [opened, setOpened] = useState<Set<number>>(new Set());
  const count = (s: JobOut["status"]) => counted?.filter((j) => j.status === s).length ?? 0;
  const tab = (id: JobOut["status"], label: string) => ({ id, label: count(id) ? `${label} (${count(id)})` : label });
  return (
    <>
      <PageHeader
        title="Jobs"
        sub="Background work: source syncs, crawls, curation passes and eval runs. The web server and satchel worker run them; a failed sync, crawl or curation pass can be retried. CLI and upload syncs run where they were started and are listed here once finished."
      />
      <div className="mb-4">
        <Segmented
          label="Status"
          value={filter}
          onChange={setFilter}
          options={[
            { id: "all", label: "All" },
            tab("queued", "Queued"),
            tab("running", "Running"),
            tab("failed", "Failed"),
            tab("done", "Done"),
          ]}
        />
      </div>
      {q.isPending && <Loading />}
      {q.isError && <ErrorBox error={q.error} />}
      {q.data && rows.length === 0 && (
        <EmptyState title={filter === "all" ? "No jobs yet" : `No ${filter} jobs`}>
          Jobs appear here when work is queued: Sync now on Sources, Crawl in Settings, Run curation, an eval run, a push webhook, or the schedules <code>satchel worker</code> runs.
        </EmptyState>
      )}
      {rows.length > 0 && <SectionHeading icon={ICONS.clock} title="Recent jobs" count={rows.length} />}
      {rows.length > 0 && (
        <TableCard>
          <thead className="border-b border-line">
            <tr>
              <th className="th w-full">Job</th>
              <th className="th">Status</th>
              <th className="th hidden md:table-cell">When</th>
              <th className="th">
                <span className="sr-only">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {rows.map((j) => {
              const st = stateOf(j);
              const w = when(j);
              return (
                <tr key={j.id} className="align-top">
                  <td className="td">
                    <div className="text-sm">
                      {KIND[j.kind] ?? <span className="font-mono">{j.kind}</span>}
                      {j.subject && <span className="font-mono text-muted"> · {j.subject}</span>}
                    </div>
                    <div className="mt-0.5 font-mono text-xs text-muted/80">
                      #{j.id}
                      {j.trigger && ` · ${j.trigger}`}
                      {j.attempts > 1 && ` · attempt ${j.attempts} of ${j.max_attempts}`}
                      <span className="md:hidden" title={w.title}> · {w.text}</span>
                    </div>
                    {j.error && (
                      <button
                        type="button"
                        className={`mt-1 max-w-2xl text-left font-mono text-xs ${j.status === "failed" ? "text-bad" : "text-muted"} ${opened.has(j.id) ? "block [overflow-wrap:anywhere]" : "line-clamp-1 break-all"}`}
                        aria-expanded={opened.has(j.id)}
                        title={opened.has(j.id) ? "Show less" : "Show the whole error"}
                        onClick={() =>
                          setOpened((prev) => {
                            const next = new Set(prev);
                            if (next.has(j.id)) next.delete(j.id);
                            else next.add(j.id);
                            return next;
                          })
                        }
                      >
                        {j.error}
                      </button>
                    )}
                  </td>
                  <td className="td whitespace-nowrap">
                    <StatusDot tone={st.tone}>{st.word}</StatusDot>
                  </td>
                  <td className="td hidden whitespace-nowrap text-muted md:table-cell" title={w.title}>
                    {w.text}
                  </td>
                  <td className="td whitespace-nowrap text-right">
                    {j.retryable && (
                      <button className="btn btn-sm" disabled={retry.isPending && retry.variables === j.id} onClick={() => retry.mutate(j.id)}>
                        Retry
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </TableCard>
      )}
    </>
  );
}
