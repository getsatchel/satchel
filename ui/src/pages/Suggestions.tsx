import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { endpoints } from "@/api/endpoints";
import type { EvidenceSession, SuggestionChange, SuggestionDetail, SuggestionSummary } from "@/api/types";
import { CodeDiff, Pre } from "@/components/Code";
import { ItemEditor, metaToText, parseMeta, type EditorValue } from "@/components/ItemEditor";
import { useToast } from "@/components/Toast";
import { EmptyState, ErrorBox, GitBadge, ICONS, ItemLink, ListDetail, ListRow, Loading, Modal, Notice, PanelSection, Segmented, SectionHeading, StatusDot, StatusLine, Tabs, TypeBadge, useWide } from "@/components/ui";
import { errMsg, fullTime, isYamlType, timeAgo } from "@/lib/format";

const SOURCE_LABEL: Record<string, string> = {
  signals: "Gap",
  hygiene: "Hygiene",
  golden: "Golden query",
};

const OP_LABEL: Record<string, string> = {
  create_item: "Add new item",
  update_item: "Update item",
  deprecate_item: "Deprecate item",
  update_asset_doc: "Update asset doc",
  endorse_asset: "Endorse asset",
  promote_golden_query: "Promote to golden query",
};

type Status = "open" | "completed" | "dismissed";
type Sort = "evidence" | "recent";

function KindBadge({ source }: { source: string }) {
  return <span className="badge">{SOURCE_LABEL[source] ?? source}</span>;
}

function invalidate(qc: ReturnType<typeof useQueryClient>, id?: string) {
  void qc.invalidateQueries({ queryKey: ["suggestions"] });
  if (id) void qc.invalidateQueries({ queryKey: ["suggestion", id] });
  void qc.invalidateQueries({ queryKey: ["items"] });
  void qc.invalidateQueries({ queryKey: ["changes"] });
}

export function SuggestionsView({ isAdmin }: { isAdmin: boolean }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [status, setStatus] = useState<Status>("open");
  const [sort, setSort] = useState<Sort>("evidence");
  const [sel, setSel] = useState<string | null>(null);
  const [skipLlm, setSkipLlm] = useState(false);
  const q = useQuery({
    queryKey: ["suggestions", status, sort],
    queryFn: () => endpoints.listSuggestions({ status, sort }),
  });
  const run = useMutation({
    mutationFn: () => endpoints.runCuration(!skipLlm),
    onSuccess: (r) => {
      toast(
        "ok",
        `Curation finished: ${r.suggestions_created} new, ${r.suggestions_extended} extended, ${r.drafted} drafted` +
          (r.llm === "none" ? " (no LLM configured, drafting skipped)" : ""),
      );
      if (r.errors.length) toast("err", r.errors.join("; "));
      invalidate(qc);
    },
    onError: (e) => toast("err", errMsg(e)),
  });
  const list = q.data?.suggestions ?? [];
  const wide = useWide();
  const current = list.find((s) => s.id === sel) ?? (wide ? list[0] : undefined);

  return (
    <>
      <div className="mb-3 flex flex-wrap items-center gap-2 sm:gap-3">
        <Segmented
          label="Status"
          options={[
            { id: "open" as Status, label: "Open" },
            { id: "completed" as Status, label: "Completed" },
            { id: "dismissed" as Status, label: "Dismissed" },
          ]}
          value={status}
          onChange={setStatus}
        />
        <Segmented
          label="Sort"
          options={[
            { id: "evidence" as Sort, label: "Most evidence" },
            { id: "recent" as Sort, label: "Newest" },
          ]}
          value={sort}
          onChange={setSort}
        />
        {isAdmin && (
          <div className="ml-auto flex items-center gap-2">
            <label className="flex items-center gap-1.5 text-xs text-muted">
              <input type="checkbox" checked={skipLlm} onChange={(e) => setSkipLlm(e.target.checked)} />
              Skip LLM
            </label>
            <button className="btn" disabled={run.isPending} onClick={() => run.mutate()}>
              {run.isPending ? "Running..." : "Run curation now"}
            </button>
          </div>
        )}
      </div>
      <SectionHeading icon={ICONS.inbox} title="Suggestions" count={q.data ? list.length : undefined} />
      {q.isPending && <Loading />}
      {q.isError && <ErrorBox error={q.error} />}
      {q.data && list.length === 0 && (
        <EmptyState title={status === "open" ? "No open suggestions" : `No ${status} suggestions`}>
          Satchel clusters the gaps agents flag (missing definitions, SQL errors, user corrections) and checks the context itself
          for stale, dead, duplicate or conflicting items. A cluster becomes a suggestion once at least 2 users in 2 sessions
          ran into it. {isAdmin ? "Use Run curation now to process new signals immediately." : "Curation runs nightly."}
        </EmptyState>
      )}
      {list.length > 0 && (
        <ListDetail
          hasDetail={current != null}
          onBack={() => setSel(null)}
          backLabel="All suggestions"
          list={
            <ul className="card max-h-[calc(100vh-12rem)] divide-y divide-line overflow-auto lg:sticky lg:top-5" aria-label="Suggestions">
              {list.map((s) => (
                <SuggestionRow key={s.id} s={s} active={s.id === current?.id} onClick={() => setSel(s.id)} />
              ))}
            </ul>
          }
          detail={current ? <SuggestionPanel key={current.id} id={current.id} onClosed={() => setSel(null)} /> : null}
        />
      )}
    </>
  );
}

/** "2 changes" until a curator decides one; then "1 of 2 left", and "All reviewed" at the end. */
function reviewState(s: SuggestionSummary): string {
  if (!s.drafted) return "No draft yet";
  if (s.pending_changes === 0) return "All reviewed";
  if (s.pending_changes === s.change_count) return `${s.change_count} change${s.change_count === 1 ? "" : "s"}`;
  return `${s.pending_changes} of ${s.change_count} left`;
}

function SuggestionRow({ s, active, onClick }: { s: SuggestionSummary; active: boolean; onClick: () => void }) {
  return (
    <ListRow active={active} onClick={onClick}>
      <StatusLine
        tone={s.drafted ? (s.pending_changes > 0 ? "warn" : "good") : "muted"}
        state={reviewState(s)}
        time={timeAgo(s.updated_at)}
        title={fullTime(s.updated_at)}
      />
      <div className="mt-1 line-clamp-2 text-sm font-medium">{s.title}</div>
      <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 font-mono text-xs text-muted/80">
        <KindBadge source={s.source} />
        <span>
          {s.evidence_count} signal{s.evidence_count === 1 ? "" : "s"}
          {s.distinct_users > 0 && ` · ${s.distinct_users} user${s.distinct_users === 1 ? "" : "s"}`}
          {s.distinct_sessions > 0 && ` · ${s.distinct_sessions} session${s.distinct_sessions === 1 ? "" : "s"}`}
        </span>
      </div>
    </ListRow>
  );
}

function SuggestionPanel({ id, onClosed }: { id: string; onClosed: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const q = useQuery({ queryKey: ["suggestion", id], queryFn: () => endpoints.getSuggestion(id) });
  const [tab, setTab] = useState<"changes" | "evidence">("changes");
  const [dismissing, setDismissing] = useState(false);
  const [reason, setReason] = useState("");

  const dismiss = useMutation({
    mutationFn: () => endpoints.dismissSuggestion(id, reason.trim()),
    onSuccess: () => {
      toast("ok", "Suggestion dismissed. Your reason will guide future drafts.");
      setDismissing(false);
      invalidate(qc, id);
      onClosed();
    },
    onError: (e) => toast("err", errMsg(e)),
  });
  const nav = useNavigate();
  const runEvals = useMutation({
    mutationFn: (keys: string[]) => endpoints.createPreview(keys, `From suggestion: ${q.data?.title ?? id}`),
    onSuccess: (p) => {
      toast("ok", `Preview #${p.seq} created; running evals`);
      void qc.invalidateQueries({ queryKey: ["releases"] });
      nav(`/changes?preview=${p.id}&evals=1`);
    },
    onError: (e) => toast("err", errMsg(e)),
  });
  const complete = useMutation({
    mutationFn: () => endpoints.completeSuggestion(id),
    onSuccess: () => {
      toast("ok", "Suggestion closed");
      invalidate(qc, id);
      onClosed();
    },
    onError: (e) => toast("err", errMsg(e)),
  });

  if (q.isPending) return <Loading />;
  if (q.isError) return <ErrorBox error={q.error} />;
  const s: SuggestionDetail = q.data;
  const open = s.status === "open";
  const acceptedKeys = s.changes.flatMap((c) =>
    c.status === "accepted" && typeof c.result["item_key"] === "string" ? [c.result["item_key"]] : [],
  );

  return (
    <section className="card min-w-0" aria-label={`Suggestion ${s.title}`}>
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-line px-4 py-3.5 sm:px-5">
        <div className="min-w-0">
          <h2 className="break-words text-base font-semibold">{s.title}</h2>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-muted">
            <KindBadge source={s.source} />
            <StatusDot tone={s.status === "open" ? "warn" : s.status === "dismissed" ? "muted" : "good"}>{s.status}</StatusDot>
            <span>
              {s.evidence_count} signals · {s.distinct_users} users · {s.distinct_sessions} sessions
            </span>
            {s.signal_kinds.map((k) => (
              <span key={k.kind} className="badge font-mono">
                {k.kind} {k.count}
              </span>
            ))}
          </div>
        </div>
        {(open || acceptedKeys.length > 0) && (
          <div className="flex flex-wrap gap-2">
            {acceptedKeys.length > 0 && (
              <button
                className="btn"
                disabled={runEvals.isPending}
                title="Creates a preview of the accepted drafts and runs the publish-gate evals"
                onClick={() => runEvals.mutate(acceptedKeys)}
              >
                Run evals on accepted changes
              </button>
            )}
          </div>
        )}
        {open && (
          <div className="flex flex-wrap gap-2">
            <button className="btn" disabled={complete.isPending} onClick={() => complete.mutate()}>
              Close
            </button>
            <button className="btn btn-danger" onClick={() => setDismissing(true)}>
              Dismiss
            </button>
          </div>
        )}
      </div>
      <PanelSection title="Summary">
      <p className="whitespace-pre-wrap text-sm">{s.summary}</p>
      {s.status === "dismissed" && s.dismiss_reason && <div className="mt-3"><Notice>Dismissed: {s.dismiss_reason}</Notice></div>}
      {!s.drafted && open && (
        <div className="mt-3">
          <Notice tone="warn">
            No changes have been drafted yet. Drafting needs a chat model (SATCHEL_CHAT_PROVIDER); the evidence below is still real.
            {s.draft_note ? ` ${s.draft_note}` : ""}
          </Notice>
        </div>
      )}
      {s.drafted && s.draft_note && (
        <p className="mt-3 text-xs text-muted">Drafting note: {s.draft_note}</p>
      )}
      </PanelSection>

      <PanelSection title="Review">
      <Tabs
        tabs={[
          { id: "changes", label: `Changes (${s.changes.length})` },
          { id: "evidence", label: `Evidence (${s.evidence.length} session${s.evidence.length === 1 ? "" : "s"})` },
        ]}
        value={tab}
        onChange={setTab}
      />
      {tab === "changes" ? (
        s.changes.length === 0 ? (
          <p className="py-4 text-sm text-muted">No proposed changes.</p>
        ) : (
          <div className="space-y-4">
            {s.changes.map((c) => (
              <ChangeCard key={c.id} suggestionId={s.id} c={c} open={open} />
            ))}
          </div>
        )
      ) : (
        <Evidence sessions={s.evidence} truncated={s.evidence_truncated} />
      )}
      </PanelSection>

      <Modal
        open={dismissing}
        onClose={() => setDismissing(false)}
        title="Dismiss suggestion"
        footer={
          <>
            <button className="btn" onClick={() => setDismissing(false)}>
              Cancel
            </button>
            <button className="btn btn-danger" disabled={!reason.trim() || dismiss.isPending} onClick={() => dismiss.mutate()}>
              Dismiss
            </button>
          </>
        }
      >
        <label className="block">
          <span className="label">Reason (required: it is fed back into future suggestion prompts)</span>
          <textarea className="input w-full" rows={3} value={reason} onChange={(e) => setReason(e.target.value)} autoFocus />
        </label>
      </Modal>
    </section>
  );
}

function ChangeCard({ suggestionId, c, open }: { suggestionId: string; c: SuggestionChange; open: boolean }) {
  const qc = useQueryClient();
  const toast = useToast();
  const itemType = c.item_type ?? "guide";
  const language = isYamlType(itemType) ? "yaml" : "markdown";
  const [editing, setEditing] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  const [key, setKey] = useState(c.proposed_key ?? "");
  const [ev, setEv] = useState<EditorValue>({ body: c.proposed_body, metaText: metaToText(c.proposed_meta) });
  const isNew = c.target_key === null;
  const gitTarget = c.target_managed_by === "git";
  const pending = c.status === "pending" && open;

  const accept = useMutation({
    mutationFn: (edited: boolean) => {
      const meta = parseMeta(ev.metaText);
      if (!meta.ok) throw new Error(meta.error);
      return endpoints.acceptSuggestionChange(suggestionId, c.id, {
        ...(edited ? { body: ev.body, meta: meta.value } : {}),
        ...(isNew && key.trim() && key.trim() !== c.proposed_key ? { key: key.trim() } : {}),
      });
    },
    onSuccess: (r) => {
      toast(
        "ok",
        r.edit_in_git
          ? (r.message ?? "Edit this in git")
          : r.item_key
            ? `Draft saved: ${r.item_key}. Review it under Changes.`
            : "Accepted",
      );
      setEditing(false);
      invalidate(qc, suggestionId);
    },
    onError: (e) => toast("err", errMsg(e)),
  });
  const reject = useMutation({
    mutationFn: () => endpoints.rejectSuggestionChange(suggestionId, c.id, reason.trim() || null),
    onSuccess: () => {
      setRejecting(false);
      invalidate(qc, suggestionId);
    },
    onError: (e) => toast("err", errMsg(e)),
  });

  const manual = c.result["manual"] === true;
  return (
    <div className="card" aria-label={`${OP_LABEL[c.op] ?? c.op} ${c.target_key ?? c.proposed_key ?? ""}`}>
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-line px-3 py-2">
        <div className="flex min-w-0 flex-wrap items-center gap-2 text-sm">
          <span className="font-medium">{OP_LABEL[c.op] ?? c.op}</span>
          {c.item_type && <TypeBadge type={c.item_type} />}
          {c.target_key ? <ItemLink itemKey={c.target_key} /> : <span className="mono text-xs">{c.proposed_key}</span>}
          {c.target_asset && <span className="text-xs text-muted">on {c.target_asset}</span>}
          {gitTarget && <GitBadge />}
          {c.status !== "pending" && <StatusDot tone={c.status === "rejected" ? "bad" : "good"}>{c.status}</StatusDot>}
        </div>
        {pending && (
          <div className="flex flex-wrap gap-2">
            <button className="btn btn-danger" onClick={() => setRejecting(true)}>
              Reject
            </button>
            <button className="btn" disabled={c.op === "deprecate_item"} onClick={() => setEditing(true)}>
              Edit &amp; accept
            </button>
            <button className="btn btn-primary" disabled={accept.isPending} onClick={() => accept.mutate(false)}>
              {gitTarget ? "Accept (edit in git)" : "Accept"}
            </button>
          </div>
        )}
      </div>
      <div className="space-y-3 p-3">
        {c.rationale && (
          <p className="text-sm">
            <span className="text-muted">Why: </span>
            {c.rationale}
          </p>
        )}
        {gitTarget && pending && (
          <Notice tone="warn">
            This item is managed in git. Accepting records the decision and shows the proposed content to copy; make the edit in the context repository.
          </Notice>
        )}
        {c.op === "deprecate_item" ? (
          <p className="text-sm text-muted">Accepting flags the item as deprecated; it leaves the live context when a release without it is published.</p>
        ) : (
          <CodeDiff
            label={`Diff for ${c.target_key ?? c.proposed_key ?? "new item"}`}
            original={c.current_body ?? ""}
            modified={c.proposed_body}
            language={language}
            height={Math.min(420, 90 + 19 * Math.max(c.proposed_body.split("\n").length, (c.current_body ?? "").split("\n").length))}
          />
        )}
        {c.status === "accepted" && typeof c.result["item_key"] === "string" && (
          <p className="text-sm">
            Draft saved as <ItemLink itemKey={c.result["item_key"]} />. Preview and publish it from <Link to="/changes" className="text-accent hover:underline">Changes</Link>.
          </p>
        )}
        {manual && (
          <>
            <Notice>{String(c.result["message"] ?? "Edit this in git.")}</Notice>
            {typeof c.result["proposed_body"] === "string" && <Pre>{c.result["proposed_body"]}</Pre>}
          </>
        )}
        {c.status === "rejected" && typeof c.result["reason"] === "string" && (
          <p className="text-xs text-muted">Rejected: {c.result["reason"]}</p>
        )}
      </div>

      <Modal
        wide
        open={editing}
        onClose={() => setEditing(false)}
        title={`Edit before accepting: ${c.target_key ?? c.proposed_key ?? "new item"}`}
        footer={
          <>
            <button className="btn" onClick={() => setEditing(false)}>
              Cancel
            </button>
            <button className="btn btn-primary" disabled={accept.isPending} onClick={() => accept.mutate(true)}>
              Accept edited version
            </button>
          </>
        }
      >
        {isNew && (
          <label className="mb-3 block">
            <span className="label">Key for the new item</span>
            <input className="input mono w-full" value={key} onChange={(e) => setKey(e.target.value)} />
          </label>
        )}
        <ItemEditor type={itemType} value={ev} onChange={setEv} />
      </Modal>
      <Modal
        open={rejecting}
        onClose={() => setRejecting(false)}
        title="Reject change"
        footer={
          <>
            <button className="btn" onClick={() => setRejecting(false)}>
              Cancel
            </button>
            <button className="btn btn-danger" disabled={reject.isPending} onClick={() => reject.mutate()}>
              Reject
            </button>
          </>
        }
      >
        <label className="block">
          <span className="label">Reason (optional, kept in the audit trail)</span>
          <textarea className="input w-full" rows={3} value={reason} onChange={(e) => setReason(e.target.value)} autoFocus />
        </label>
      </Modal>
    </div>
  );
}

function Evidence({ sessions, truncated }: { sessions: EvidenceSession[]; truncated: boolean }) {
  return (
    <div className="space-y-3">
      {sessions.map((e) => (
        <div key={e.session_id ?? "system"} className="card p-3">
          <div className="flex flex-wrap items-center gap-2 text-sm">
            {e.session_id ? (
              <Link to={`/sessions/${e.session_id}`} className="font-medium text-accent hover:underline">
                {e.question ? `"${e.question}"` : e.sensitive ? "Sensitive session" : "Open session"}
              </Link>
            ) : (
              <span className="font-medium">Context findings (no session)</span>
            )}
            {e.user_label && <span className="text-xs text-muted">{e.user_label}</span>}
            {e.client && <span className="badge">{e.client}</span>}
            {e.started_at && <span className="text-xs text-muted">{timeAgo(e.started_at)}</span>}
            {e.redaction !== "none" && (
              <span className="badge" title="Evidence is redacted for your role">
                {e.redaction === "metadata_only" ? "metadata only" : "redacted"}
              </span>
            )}
          </div>
          <ul className="mt-2 space-y-1">
            {e.signals.map((sg) => (
              <li key={sg.id} className="text-sm">
                <span className="badge mr-2 font-mono">{sg.kind}</span>
                {sg.message ?? <span className="text-muted">details withheld</span>}
              </li>
            ))}
          </ul>
        </div>
      ))}
      {truncated && <p className="text-xs text-muted">Showing the 50 most recent sessions.</p>}
    </div>
  );
}
