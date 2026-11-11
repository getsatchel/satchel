import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { endpoints } from "@/api/endpoints";
import type { ChangeOut, EvalGate, PackOut, PreviewOut, SearchHit } from "@/api/types";
import { CaseMoves, DeltaTable } from "@/components/EvalBits";
import { Chevron, EmptyState, ErrorBox, GitBadge, ICONS, ItemKey, ItemLink, KeyLines, Loading, Modal, Notice, PageHeader, SectionHeading, SourceBadge, StatusDot, StatusLine, TypeBadge, type Tone } from "@/components/ui";
import { DiffStat, UnifiedDiff } from "@/components/UnifiedDiff";
import { Pre } from "@/components/Code";
import { useToast } from "@/components/Toast";
import { errMsg } from "@/lib/format";

export function Changes() {
  const [sp, setSp] = useSearchParams();
  const previewId = sp.get("preview");
  const q = useQuery({ queryKey: ["changes"], queryFn: endpoints.listChanges });
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [note, setNote] = useState("");
  const [open, setOpen] = useState<Set<string>>(new Set());
  const qc = useQueryClient();
  const toast = useToast();
  const changes = q.data?.changes ?? [];
  const all = changes.length > 0 && changes.every((c) => picked.has(c.key));

  const toggle = (set: Set<string>, key: string) => {
    const n = new Set(set);
    if (n.has(key)) n.delete(key);
    else n.add(key);
    return n;
  };

  const create = useMutation({
    mutationFn: () => endpoints.createPreview([...picked], note.trim() || null),
    onSuccess: (p) => {
      toast("ok", `Preview release #${p.seq} created`);
      void qc.invalidateQueries({ queryKey: ["releases"] });
      setSp({ preview: p.id });
    },
    onError: (e) => toast("err", errMsg(e)),
  });

  return (
    <>
      <PageHeader
        title="Changes"
        sub="Unpublished edits. Pick the ones to ship, create a preview, test it against real questions, then publish."
      />
      {q.isPending && <Loading />}
      {q.isError && <ErrorBox error={q.error} />}
      {q.data && changes.length === 0 && !previewId && (
        <EmptyState title="No unpublished changes">
          Edit an item in the Library, create a new one, or accept an agent proposal from the Inbox. Drafts show up here with their diffs.
        </EmptyState>
      )}
      {changes.length > 0 && (
        <section className="mb-9" aria-labelledby="chg-h">
        <SectionHeading id="chg-h" icon={ICONS.edit} title="Unpublished changes" count={changes.length} />
        <div className="card overflow-hidden">
          <div className="flex items-center gap-3 border-b border-line px-4 py-2.5 sm:px-5">
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={all}
                onChange={() => setPicked(all ? new Set() : new Set(changes.map((c) => c.key)))}
              />
              Select all ({changes.length})
            </label>
            <span className="ml-auto font-mono text-xs text-muted">{picked.size} selected</span>
          </div>
          <ul className="divide-y divide-line">
            {changes.map((c) => (
              <ChangeRow
                key={c.key}
                c={c}
                checked={picked.has(c.key)}
                expanded={open.has(c.key)}
                onCheck={() => setPicked((s) => toggle(s, c.key))}
                onExpand={() => setOpen((s) => toggle(s, c.key))}
              />
            ))}
          </ul>
          <div className="flex flex-wrap items-center gap-2 border-t border-line px-4 py-3 sm:px-5">
            <input
              className="input w-full sm:w-auto sm:min-w-64 sm:flex-1"
              placeholder="Release note (optional): what changed and why"
              aria-label="Release note"
              value={note}
              onChange={(e) => setNote(e.target.value)}
            />
            <button className="btn btn-primary" disabled={picked.size === 0 || create.isPending} onClick={() => create.mutate()}>
              Create preview{picked.size ? ` (${picked.size})` : ""}
            </button>
          </div>
        </div>
        </section>
      )}
      {previewId && <PreviewPanel id={previewId} onClose={() => setSp({})} />}
    </>
  );
}

function ChangeRow(props: {
  c: ChangeOut;
  checked: boolean;
  expanded: boolean;
  onCheck: () => void;
  onExpand: () => void;
}) {
  const { c } = props;
  const tone: Tone = c.kind === "create" ? "good" : c.kind === "deprecate" ? "bad" : "warn";
  return (
    <li>
      <div className={`flex items-start gap-3 px-4 py-3 sm:px-5 ${props.checked ? "bg-accent-soft/70 shadow-[inset_2px_0_0_rgb(var(--accent))]" : "hover:bg-sunken/70"}`}>
        <input className="mt-1.5" type="checkbox" aria-label={`Select ${c.key}`} checked={props.checked} onChange={props.onCheck} />
        <button
          className="flex min-w-0 flex-1 flex-wrap items-center gap-2 text-left"
          onClick={props.onExpand}
          aria-expanded={props.expanded}
        >
          <Chevron open={props.expanded} />
          <span className="min-w-0 flex-1 text-sm" title={c.key}>
            <KeyLines itemKey={c.key} />
          </span>
          <span className="hidden sm:inline-flex">
            <TypeBadge type={c.type} />
          </span>
          <StatusLine tone={tone} state={c.kind} />
          {c.managed_by === "git" && <GitBadge />}
          {c.managed_by === "source" && <SourceBadge />}
        </button>
        <span className="hidden self-center whitespace-nowrap font-mono text-xs text-muted md:inline">
          {c.from_seq != null ? `v${c.from_seq}` : "new"} → {c.to_seq != null ? `v${c.to_seq}` : "removed"}
        </span>
        <span className="self-center">
          <DiffStat add={c.diff.additions} del={c.diff.deletions} />
        </span>
      </div>
      {props.expanded && (
        <div className="px-3 pb-3 pl-10">
          <UnifiedDiff text={c.diff.unified} empty={c.diff.meta_changed ? "Only the metadata changed." : "No textual change."} />
        </div>
      )}
    </li>
  );
}

function PreviewPanel({ id, onClose }: { id: string; onClose: () => void }) {
  const q = useQuery({ queryKey: ["preview", id], queryFn: () => endpoints.getPreview(id) });
  const [question, setQuestion] = useState("");
  const [asked, setAsked] = useState<string | null>(null);
  const [confirm, setConfirm] = useState(false);
  const [override, setOverride] = useState("");
  const [sp] = useSearchParams();
  const nav = useNavigate();
  const qc = useQueryClient();
  const toast = useToast();
  const me = useQuery({ queryKey: ["me"], queryFn: endpoints.me, staleTime: 60_000 });
  const isAdmin = me.data?.role === "admin";
  const canRun = me.data?.role === "admin" || me.data?.role === "curator" || me.data?.role === "analyst";
  const gate = useQuery({
    queryKey: ["gate", id],
    queryFn: () => endpoints.previewGate(id),
    refetchInterval: (query) => (query.state.data?.status === "running" ? 2000 : false),
  });
  const runEvals = useMutation({
    mutationFn: () => endpoints.runPreviewEvals(id),
    onSuccess: (g) => qc.setQueryData(["gate", id], g),
    onError: (e) => toast("err", errMsg(e)),
  });
  const autoRan = useRef(false);
  useEffect(() => {
    // "Run evals on accepted changes" lands here with ?evals=1
    if (sp.get("evals") === "1" && gate.data?.configured && canRun && !autoRan.current) {
      autoRan.current = true;
      runEvals.mutate();
    }
  }, [sp, gate.data?.configured, canRun, runEvals]);
  const blocked = gate.data != null && gate.data.status !== "none" && gate.data.status !== "pass";

  const publish = useMutation({
    mutationFn: () => endpoints.publish(id, blocked ? override.trim() : undefined),
    onSuccess: (r) => {
      toast("ok", `Published release #${r.seq}`);
      setConfirm(false);
      void qc.invalidateQueries();
      nav("/releases");
    },
    onError: (e) => {
      setConfirm(false);
      toast("err", errMsg(e));
    },
  });

  if (q.isPending) return <Loading label="Loading preview" />;
  if (q.isError) return <ErrorBox error={q.error} />;
  const p: PreviewOut = q.data;
  const published = p.kind === "published";

  return (
    <section aria-labelledby="prev-h">
    <SectionHeading id="prev-h" icon={ICONS.play} title={`Preview release #${p.seq}`} actions={published ? <StatusDot tone="good">published</StatusDot> : undefined} />
    <div className="card p-4 sm:p-5" aria-label="Preview">
      <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="break-words text-sm text-muted">
            {p.changed_keys.length} changed, {p.removed_keys.length} removed, {p.item_count} items in total
            {p.note ? ` · "${p.note}"` : ""}
          </p>
          <ul className="mt-2 grid gap-x-6 gap-y-1 text-sm" style={{ gridTemplateColumns: "repeat(auto-fill,minmax(min(18rem,100%),1fr))" }}>
            {p.changed_keys.map((k) => (
              <li key={k} className="min-w-0">
                <ItemLink itemKey={k} />
              </li>
            ))}
            {p.removed_keys.map((k) => (
              <li key={k} className="min-w-0 font-mono text-bad line-through [overflow-wrap:anywhere]" title="Removed in this release">
                <ItemKey itemKey={k} />
              </li>
            ))}
          </ul>
        </div>
        <div className="flex flex-wrap gap-2">
          <button className="btn" onClick={onClose}>
            Close
          </button>
          {!published && (
            <button
              className="btn btn-primary"
              disabled={blocked && !isAdmin}
              title={blocked && !isAdmin ? "Blocked by the eval gate: run evals, or ask an admin to override" : undefined}
              onClick={() => setConfirm(true)}
            >
              {blocked ? "Publish (override)" : "Publish"}
            </button>
          )}
        </div>
      </div>

      {!published && gate.data?.configured && (
        <GatePanel gate={gate.data} canRun={canRun} running={runEvals.isPending} onRun={() => runEvals.mutate()} />
      )}
      {!published && gate.data && !gate.data.configured && (
        <p className="mb-3 text-xs text-muted">
          No eval gate is configured (<code>publish.require_evals</code>), so this preview can be published without evals. You can still run suites from <Link to="/evals" className="text-accent hover:underline">Evals</Link>.
        </p>
      )}

      <form
        className="mb-3 flex flex-col gap-2 sm:flex-row"
        onSubmit={(e) => {
          e.preventDefault();
          if (question.trim()) setAsked(question.trim());
        }}
      >
        <input
          className="input min-w-0 flex-1"
          placeholder="e.g. what was revenue last month?"
          aria-label="Test question"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
        />
        <button className="btn" type="submit" disabled={!question.trim()}>
          Compare
        </button>
      </form>
      {asked ? (
        <Compare previewId={id} question={asked} />
      ) : (
        <p className="rounded border border-dashed border-line px-3 py-6 text-center text-sm text-muted">
          Test box: ask a question to see the context pack and search results from this preview next to the currently published release.
        </p>
      )}

      <Modal
        open={confirm}
        onClose={() => setConfirm(false)}
        title={`Publish release #${p.seq}?`}
        footer={
          <>
            <button className="btn" onClick={() => setConfirm(false)}>
              Cancel
            </button>
            <button
              className="btn btn-primary"
              disabled={publish.isPending || (blocked && !override.trim())}
              onClick={() => publish.mutate()}
            >
              {blocked ? "Override and publish" : "Publish"}
            </button>
          </>
        }
      >
        {blocked && (
          <div className="mb-3 space-y-2">
            <Notice tone="warn">The eval gate is {gate.data?.status}. As an admin you can publish anyway; the override and your reason are audited.</Notice>
            <label className="block">
              <span className="label">Override reason (required)</span>
              <textarea className="input w-full" rows={2} value={override} onChange={(e) => setOverride(e.target.value)} />
            </label>
          </div>
        )}
        <p className="text-sm">
          Agents will be served {p.changed_keys.length} changed and {p.removed_keys.length} removed item(s) from their next call. You can roll back from Releases.
        </p>
        {p.note && <p className="mt-2 text-sm text-muted">Note: {p.note}</p>}
      </Modal>
    </div>
    </section>
  );
}

const GATE_TEXT: Record<string, string> = {
  pass: "Gate passed: no required measure regressed.",
  fail: "Gate failed: a required measure regressed. Publishing is blocked.",
  pending: "Gate has no results for this preview yet. Run evals to continue.",
  running: "Evals are running…",
};

function GatePanel(props: { gate: EvalGate; canRun: boolean; running: boolean; onRun: () => void }) {
  const { gate } = props;
  const tone: Tone = gate.status === "pass" ? "good" : gate.status === "fail" ? "bad" : "warn";
  return (
    <section className="card mb-4 overflow-hidden" aria-label="Eval gate">
      <div className="flex flex-wrap items-center gap-2 border-b border-line bg-sunken/50 px-4 py-2">
        <h3 className="label-caps">Evals</h3>
        <StatusDot tone={tone}>{gate.status}</StatusDot>
        <span className="min-w-0 basis-full text-xs text-muted sm:basis-auto">{GATE_TEXT[gate.status]}</span>
        {props.canRun && (
          <button className="btn ml-auto" disabled={props.running || gate.status === "running"} onClick={props.onRun}>
            {gate.status === "pending" ? "Run evals" : "Re-run evals"}
          </button>
        )}
      </div>
      <div className="space-y-3 p-4">
        {gate.suites.map((s) => (
          <div key={s.suite_key}>
            <div className="mb-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
              <span className="break-all font-mono">{s.suite_key}</span>
              <span className="mono text-xs text-muted">v{s.suite_version}</span>
              <StatusDot tone={s.status === "pass" ? "good" : s.status === "fail" ? "bad" : "warn"}>{s.status}</StatusDot>
              {s.message && <span className="text-xs text-muted">{s.message}</span>}
              {s.preview_run_id && (
                <Link className="text-xs text-accent hover:underline" to={`/evals/runs/${s.preview_run_id}`}>
                  run detail
                </Link>
              )}
            </div>
            {s.measures.length > 0 && (
              <DeltaTable
                rows={s.measures}
                aLabel="Published"
                bLabel="Preview"
                regressed={new Set(s.regressions.map((r) => r.measure))}
              />
            )}
            <CaseMoves failing={s.newly_failing} passing={s.newly_passing} />
          </div>
        ))}
      </div>
    </section>
  );
}

function Compare({ previewId, question }: { previewId: string; question: string }) {
  const prev = useQuery({
    queryKey: ["cmp", "preview", previewId, question],
    queryFn: async () => ({
      pack: await endpoints.previewPack(previewId, question),
      hits: await endpoints.previewSearch(previewId, question),
    }),
  });
  const pub = useQuery({
    queryKey: ["cmp", "published", previewId, question],
    queryFn: async () => ({ pack: await endpoints.pack(question), hits: await endpoints.search(question) }),
  });
  const tags = (r?: { pack: PackOut }) => new Set(r?.pack.entries.map((e) => e.tag));
  const prevTags = tags(prev.data);
  const pubTags = tags(pub.data);
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Column title="Preview" accent q={prev} other={pubTags} />
      <Column title="Published now" q={pub} other={prevTags} />
    </div>
  );
}

function Column(props: {
  title: string;
  accent?: boolean;
  q: { isPending: boolean; isError: boolean; error: unknown; data?: { pack: PackOut; hits: SearchHit[] } };
  other: Set<string>;
}) {
  const { q } = props;
  return (
    <div className={`card min-w-0 overflow-hidden ${props.accent ? "border-accent/50" : ""}`}>
      <h3 className="label-caps border-b border-line bg-sunken/50 px-4 py-2">{props.title}</h3>
      <div className="space-y-4 p-4">
        {q.isPending && <Loading />}
        {q.isError && <ErrorBox error={q.error} />}
        {q.data && (
          <>
            <div>
              <h4 className="label-caps mb-1.5">
                Context pack · {q.data.pack.used_tokens}/{q.data.pack.budget_tokens} tokens
              </h4>
              {q.data.pack.entries.length === 0 ? (
                <p className="text-sm text-muted">Nothing relevant was found.</p>
              ) : (
                <ul className="space-y-1">
                  {q.data.pack.entries.map((e) => (
                    <li key={e.tag} className="flex flex-wrap items-center gap-x-2 text-sm">
                      <span className="min-w-0">
                        <ItemLink itemKey={e.key} seq={e.seq} />
                      </span>
                      {!props.other.has(e.tag) && <span className="badge badge-accent">differs</span>}
                      <span className="ml-auto mono text-xs tabular-nums text-muted">
                        {e.score != null ? e.score.toFixed(3) : ""} · {e.tokens}t
                      </span>
                    </li>
                  ))}
                </ul>
              )}
              <details className="mt-2">
                <summary className="cursor-pointer text-xs text-muted">Pack text as the agent sees it</summary>
                <Pre>{q.data.pack.text}</Pre>
              </details>
            </div>
            <div>
              <h4 className="label-caps mb-1.5">Search results</h4>
              {q.data.hits.length === 0 ? (
                <p className="text-sm text-muted">No hits.</p>
              ) : (
                <ol className="space-y-2">
                  {q.data.hits.map((h) => (
                    <li key={h.tag} className="text-sm">
                      <div className="flex items-center gap-2">
                        <span className="min-w-0">
                          <ItemLink itemKey={h.key} seq={h.seq} />
                        </span>
                        <span className="ml-auto mono text-xs tabular-nums text-muted">{h.score.toFixed(3)}</span>
                      </div>
                      <p className="line-clamp-2 text-xs text-muted">{h.snippet}</p>
                    </li>
                  ))}
                </ol>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
