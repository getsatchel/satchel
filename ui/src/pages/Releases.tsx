import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { endpoints } from "@/api/endpoints";
import type { ReleaseOut } from "@/api/types";
import { Chevron, EmptyState, ErrorBox, ICONS, ItemKey, ItemLink, Loading, Modal, PageHeader, SectionHeading, StatusLine, StatusDot } from "@/components/ui";
import { useToast } from "@/components/Toast";
import { errMsg, fullTime, timeAgo } from "@/lib/format";

export function Releases() {
  const q = useQuery({ queryKey: ["releases"], queryFn: () => endpoints.listReleases("published") });
  const me = useQuery({ queryKey: ["me"], queryFn: endpoints.me });
  const [target, setTarget] = useState<ReleaseOut | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const qc = useQueryClient();
  const toast = useToast();

  const rollback = useMutation({
    mutationFn: (id: string) => endpoints.rollback(id),
    onSuccess: (r) => {
      toast("ok", `Rolled back: new release #${r.seq} is live`);
      setTarget(null);
      void qc.invalidateQueries();
    },
    onError: (e) => {
      setTarget(null);
      toast("err", errMsg(e));
    },
  });

  const rels = q.data?.releases ?? [];
  return (
    <>
      <PageHeader title="Releases" sub="Every published version of the context. Rolling back publishes a new release that restores an older one; history is never rewritten." />
      {q.isPending && <Loading />}
      {q.isError && <ErrorBox error={q.error} />}
      {q.data && rels.length === 0 && (
        <EmptyState title="Nothing published yet">
          Run <code>satchel sync import &lt;folder&gt; --workspace &lt;slug&gt;</code> to import a context repo, or publish a preview from Changes. Either creates the first release.
        </EmptyState>
      )}
      {rels.length > 0 && (
        <section aria-labelledby="rel-h">
        <SectionHeading id="rel-h" icon={ICONS.tag} title="Published releases" count={rels.length} />
        <ol className="space-y-3" aria-label="Releases">
          {rels.map((r) => (
            <li key={r.id} className="card">
              <div className="flex flex-wrap items-center gap-3 px-4 py-3.5 sm:px-5">
                <span className="font-mono text-base font-semibold tabular-nums">#{r.seq}</span>
                <div className="min-w-[12rem] flex-1">
                  <StatusLine tone={r.is_current ? "good" : "muted"} state={r.is_current ? "Live" : "Superseded"} time={timeAgo(r.created_at)} title={fullTime(r.created_at)} />
                  <div className="mt-1 break-words text-sm">{r.note ?? <span className="text-muted">No note</span>}</div>
                  <div className="mt-0.5 font-mono text-xs text-muted/80">
                    {r.created_by ? (r.created_by === me.data?.user_id ? "you" : `user ${r.created_by.slice(0, 8)}`) : "system"} · {r.item_count} items
                  </div>
                </div>
                <button className="btn btn-pill" aria-expanded={open === r.id} onClick={() => setOpen(open === r.id ? null : r.id)}>
                  <Chevron open={open === r.id} />
                  Items changed
                </button>
                {!r.is_current && (
                  <button className="btn btn-sm btn-danger" onClick={() => setTarget(r)}>
                    Roll back to this
                  </button>
                )}
              </div>
              {open === r.id && <ReleaseChanges id={r.id} />}
            </li>
          ))}
        </ol>
        </section>
      )}
      <Modal
        open={target !== null}
        onClose={() => setTarget(null)}
        title={`Roll back to release #${target?.seq}?`}
        footer={
          <>
            <button className="btn" onClick={() => setTarget(null)}>
              Cancel
            </button>
            <button className="btn btn-danger" disabled={rollback.isPending} onClick={() => target && rollback.mutate(target.id)}>
              Roll back
            </button>
          </>
        }
      >
        <p className="text-sm">
          This publishes a new release that restores exactly what release #{target?.seq} contained. Agents are served that content from their next call. Unpublished drafts are kept.
        </p>
      </Modal>
    </>
  );
}

function ReleaseChanges({ id }: { id: string }) {
  const q = useQuery({ queryKey: ["release-changes", id], queryFn: () => endpoints.getPreview(id) });
  return (
    <div className="border-t border-line px-4 py-3 text-sm">
      {q.isPending && <Loading />}
      {q.isError && <ErrorBox error={q.error} />}
      {q.data && q.data.changed_keys.length + q.data.removed_keys.length === 0 && (
        <p className="text-muted">No items differ from the previous release.</p>
      )}
      {q.data && (
        <ul className="grid gap-x-6 gap-y-1.5" style={{ gridTemplateColumns: "repeat(auto-fill,minmax(min(20rem,100%),1fr))" }}>
          {q.data.changed_keys.map((k) => (
            <li key={k} className="flex min-w-0 items-start gap-2">
              <span className="mt-0.5 shrink-0">
                <StatusDot tone="warn">changed</StatusDot>
              </span>
              <ItemLink itemKey={k} />
            </li>
          ))}
          {q.data.removed_keys.map((k) => (
            <li key={k} className="flex min-w-0 items-start gap-2">
              <span className="mt-0.5 shrink-0">
                <StatusDot tone="bad">removed</StatusDot>
              </span>
              <span className="font-mono text-muted [overflow-wrap:anywhere]">
                <ItemKey itemKey={k} />
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
