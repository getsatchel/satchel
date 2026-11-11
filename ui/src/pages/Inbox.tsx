import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { endpoints } from "@/api/endpoints";
import type { ProposalOut } from "@/api/types";
import { DescList, EmptyState, ErrorBox, GitBadge, ICONS, ItemKey, ItemLink, KeyLines, ListDetail, ListRow, Loading, Modal, Notice, PageHeader, PanelSection, SectionHeading, StatusLine, Tabs, TypeBadge, useWide } from "@/components/ui";
import { UnifiedDiff } from "@/components/UnifiedDiff";
import { ItemEditor, metaToText, parseMeta, type EditorValue } from "@/components/ItemEditor";
import { Markdown } from "@/components/Markdown";
import { Pre } from "@/components/Code";
import { useToast } from "@/components/Toast";
import { SuggestionsView } from "@/pages/Suggestions";
import { errMsg, fullTime, isYamlType, timeAgo } from "@/lib/format";

function bodyAsDiff(p: ProposalOut): string {
  if (p.diff) return p.diff.unified;
  return [`--- /dev/null`, `+++ ${p.key}@proposed`, ...p.body.split("\n").map((l) => `+${l}`)].join("\n");
}

export function Inbox() {
  const me = useQuery({ queryKey: ["me"], queryFn: endpoints.me, staleTime: 60_000 });
  const sug = useQuery({
    queryKey: ["suggestions", "open", "evidence"],
    queryFn: () => endpoints.listSuggestions({ status: "open", sort: "evidence" }),
  });
  const props = useQuery({ queryKey: ["proposals"], queryFn: endpoints.listProposals });
  const [tab, setTab] = useState<"suggestions" | "proposals" | null>(null);
  const nSug = sug.data?.total;
  const nProp = props.data?.proposals.length;
  // Default to whichever list has something to review; suggestions win ties.
  const active = tab ?? (nSug === 0 && (nProp ?? 0) > 0 ? "proposals" : "suggestions");
  return (
    <>
      <PageHeader
        title="Inbox"
        sub="Context changes waiting for review. Nothing here is live: accepting creates a draft that you still preview and publish."
      />
      <Tabs
        tabs={[
          { id: "suggestions", label: `Suggestions${nSug === undefined ? "" : ` (${nSug})`}` },
          { id: "proposals", label: `Agent proposals${nProp === undefined ? "" : ` (${nProp})`}` },
        ]}
        value={active}
        onChange={setTab}
      />
      {active === "suggestions" ? <SuggestionsView isAdmin={me.data?.role === "admin"} /> : <ProposalsView />}
    </>
  );
}

function ProposalsView() {
  const q = useQuery({ queryKey: ["proposals"], queryFn: endpoints.listProposals });
  const [sel, setSel] = useState<string | null>(null);
  const wide = useWide();
  const list = q.data?.proposals ?? [];
  const current = list.find((p) => p.key === sel) ?? (wide ? list[0] : undefined);

  return (
    <section aria-labelledby="proposals-h">
      <SectionHeading id="proposals-h" icon={ICONS.bolt} title="Agent proposals" count={q.data ? list.length : undefined} />
      {q.isPending && <Loading />}
      {q.isError && <ErrorBox error={q.error} />}
      {q.data && list.length === 0 && (
        <EmptyState title="Nothing to review">
          Agents propose context changes with the propose_context_change MCP tool, for example after a user corrects an answer.
          Proposals appear here with the session they came from. Connect an MCP client (docs/integrations/claude.md) to start receiving them.
        </EmptyState>
      )}
      {list.length > 0 && (
        <ListDetail
          hasDetail={current != null}
          onBack={() => setSel(null)}
          backLabel="All proposals"
          list={
            <ul className="card max-h-[calc(100vh-9rem)] divide-y divide-line overflow-auto lg:sticky lg:top-5" aria-label="Proposals">
              {list.map((p) => (
                <ListRow key={p.key} active={current?.key === p.key} onClick={() => setSel(p.key)}>
                  <StatusLine tone="info" state={p.op === "update_item" ? "Update" : "New item"} time={timeAgo(p.created_at)} title={fullTime(p.created_at)} />
                  <div className="mt-1 text-sm font-medium [overflow-wrap:anywhere]" title={p.key}>
                    <KeyLines itemKey={p.key} />
                  </div>
                  {p.rationale && <div className="line-clamp-2 text-sm text-muted">{p.rationale}</div>}
                  <div className="mt-1 flex flex-wrap items-center gap-1.5 font-mono text-xs text-muted/80">
                    <TypeBadge type={p.type} />
                    {p.proposed_by_label && <span>from {p.proposed_by_label}</span>}
                  </div>
                </ListRow>
              ))}
            </ul>
          }
          detail={current ? <ProposalPanel key={current.key} p={current} onResolved={() => setSel(null)} /> : null}
        />
      )}
    </section>
  );
}

function ProposalPanel({ p, onResolved }: { p: ProposalOut; onResolved: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [tab, setTab] = useState<"diff" | "body">("diff");
  const [editing, setEditing] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  const [newKey, setNewKey] = useState(p.key);
  const [ev, setEv] = useState<EditorValue>({ body: p.body, metaText: metaToText(p.meta) });
  const isUpdate = p.op === "update_item";
  const gitTarget = p.target_managed_by === "git";

  const done = (msg: string) => {
    toast("ok", msg);
    void qc.invalidateQueries({ queryKey: ["proposals"] });
    void qc.invalidateQueries({ queryKey: ["items"] });
    void qc.invalidateQueries({ queryKey: ["changes"] });
    onResolved();
  };

  const accept = useMutation({
    mutationFn: () => {
      const meta = parseMeta(ev.metaText);
      if (!meta.ok) throw new Error(meta.error);
      const edited = ev.body !== p.body || ev.metaText !== metaToText(p.meta);
      return endpoints.acceptProposal(p.key, {
        key: !isUpdate && newKey.trim() && newKey.trim() !== p.key ? newKey.trim() : undefined,
        ...(edited ? { body: ev.body, meta: meta.value } : {}),
      });
    },
    onSuccess: (r) => done(`Accepted as a draft: ${r.item_key ?? p.key}`),
    onError: (e) => toast("err", errMsg(e)),
  });
  const reject = useMutation({
    mutationFn: () => endpoints.rejectProposal(p.key, reason.trim()),
    onSuccess: () => {
      setRejecting(false);
      done("Proposal rejected");
    },
    onError: (e) => toast("err", errMsg(e)),
  });

  return (
    <section className="card min-w-0" aria-label={`Proposal ${p.key}`}>
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-line px-4 py-3.5 sm:px-5">
        <div className="min-w-0">
          <h2 className="font-mono text-base font-semibold [overflow-wrap:anywhere]">
            <ItemKey itemKey={p.key} />
          </h2>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-muted">
            <TypeBadge type={p.type} />
            <span>{isUpdate ? "Proposes an update to" : "Proposes a new item"}</span>
            {p.target && <ItemLink itemKey={p.target} />}
            {gitTarget && <GitBadge />}
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          <button className="btn btn-danger" onClick={() => setRejecting(true)}>
            Reject
          </button>
          <button className="btn" disabled={gitTarget} onClick={() => setEditing(true)}>
            Edit &amp; accept
          </button>
          <button className="btn btn-primary" disabled={gitTarget || accept.isPending} onClick={() => accept.mutate()}>
            Accept
          </button>
        </div>
      </div>

      {gitTarget && (
        <div className="border-b border-line p-4">
          <Notice tone="warn">
            The target is managed in git, so this proposal cannot be applied here. Make the change in the context repository and reject this proposal.
          </Notice>
        </div>
      )}

      <PanelSection title="Origin">
        <DescList
          rows={[
            ["Proposed by", `${p.proposed_by_label ?? "an agent"} · ${timeAgo(p.created_at)}`],
            [
              "Session",
              p.session_id ? (
                <Link to={`/sessions/${p.session_id}`} className="text-accent hover:underline">
                  {p.session_question ? `"${p.session_question}"` : "Open session"}
                </Link>
              ) : (
                <span className="text-muted">none recorded</span>
              ),
            ],
            ["Rationale", p.rationale ?? <span className="text-muted">none given</span>],
          ]}
        />
      </PanelSection>

      <PanelSection title="Proposed change">
      <Tabs
        tabs={[
          { id: "diff", label: isUpdate ? "Diff against target" : "Proposed (new)" },
          { id: "body", label: "Proposed body" },
        ]}
        value={tab}
        onChange={setTab}
      />
      {tab === "diff" ? (
        <UnifiedDiff text={bodyAsDiff(p)} />
      ) : isYamlType(p.type) ? (
        <Pre>{p.body}</Pre>
      ) : (
        <div className="rounded border border-line p-3">
          <Markdown>{p.body}</Markdown>
        </div>
      )}
      {Object.keys(p.meta).length > 0 && (
        <details className="mt-3 text-sm">
          <summary className="cursor-pointer text-muted">Metadata</summary>
          <Pre>{JSON.stringify(p.meta, null, 2)}</Pre>
        </details>
      )}
      </PanelSection>

      <Modal
        wide
        open={editing}
        onClose={() => setEditing(false)}
        title={`Edit before accepting: ${p.key}`}
        footer={
          <>
            <button className="btn" onClick={() => setEditing(false)}>
              Cancel
            </button>
            <button
              className="btn btn-primary"
              disabled={accept.isPending}
              onClick={() => accept.mutate(undefined, { onSuccess: () => setEditing(false) })}
            >
              Accept edited version
            </button>
          </>
        }
      >
        {!isUpdate && (
          <label className="mb-3 block">
            <span className="label">Key for the new item</span>
            <input className="input mono w-full" value={newKey} onChange={(e) => setNewKey(e.target.value)} />
          </label>
        )}
        <ItemEditor type={p.type} value={ev} onChange={setEv} />
      </Modal>

      <Modal
        open={rejecting}
        onClose={() => setRejecting(false)}
        title="Reject proposal"
        footer={
          <>
            <button className="btn" onClick={() => setRejecting(false)}>
              Cancel
            </button>
            <button className="btn btn-danger" disabled={!reason.trim() || reject.isPending} onClick={() => reject.mutate()}>
              Reject
            </button>
          </>
        }
      >
        <label className="block">
          <span className="label">Reason (kept in the audit trail)</span>
          <textarea className="input w-full" rows={3} value={reason} onChange={(e) => setReason(e.target.value)} autoFocus />
        </label>
      </Modal>
    </section>
  );
}
