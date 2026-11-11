import { useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { endpoints } from "@/api/endpoints";
import type { EvalCase, EvalRun, EvalSuite } from "@/api/types";
import { CaseMoves, DeltaTable, MeasureChips, OutcomeBadge } from "@/components/EvalBits";
import { useToast } from "@/components/Toast";
import { Chevron, Crumb, EmptyState, ErrorBox, GitBadge, ICONS, KeyLines, Loading, Modal, Notice, PageHeader, SectionHeading, StatCell, StatGrid, TableCard, Tabs } from "@/components/ui";
import { errMsg, timeAgo } from "@/lib/format";

type Tab = "suites" | "runs";
const RANK = { explorer: 0, analyst: 1, curator: 2, admin: 3 } as const;

function useRole(): number {
  const me = useQuery({ queryKey: ["me"], queryFn: endpoints.me, staleTime: 60_000 });
  return (RANK as Record<string, number>)[me.data?.role ?? "explorer"] ?? 0;
}

export function Evals() {
  const [sp, setSp] = useSearchParams();
  const tab = (sp.get("tab") as Tab | null) ?? "suites";
  return (
    <>
      <PageHeader
        title="Evals"
        sub="Suites of questions with graders. Run them against the published release or a preview to see whether a context change helps or hurts before it ships."
      />
      <Tabs
        tabs={[
          { id: "suites" as Tab, label: "Suites" },
          { id: "runs" as Tab, label: "Run history" },
        ]}
        value={tab}
        onChange={(t) => setSp({ tab: t })}
      />
      {tab === "suites" ? <Suites /> : <Runs />}
    </>
  );
}

// ---- suites ---------------------------------------------------------------------------------

function Suites() {
  const q = useQuery({ queryKey: ["eval-suites"], queryFn: endpoints.listEvalSuites });
  const role = useRole();
  const [view, setView] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [running, setRunning] = useState<EvalSuite | null>(null);
  return (
    <>
      <SectionHeading
        icon={ICONS.flask}
        title="Suites"
        count={q.data?.length}
        actions={
          role >= RANK.curator ? (
            <button className="btn btn-sm" onClick={() => setEditing(true)}>
              New suite
            </button>
          ) : undefined
        }
      />
      {q.isPending && <Loading />}
      {q.isError && <ErrorBox error={q.error} />}
      {q.data && q.data.length === 0 && (
        <EmptyState title="No eval suites yet">
          Add <code>evals/*.yaml</code> to your context repo and run <code>satchel sync import</code>, or paste a suite with New suite.
        </EmptyState>
      )}
      {q.data && q.data.length > 0 && (
        <TableCard>
          <thead className="border-b border-line">
            <tr>
              <th className="th w-full">Suite</th>
              <th className="th hidden text-right sm:table-cell">Cases</th>
              <th className="th hidden xl:table-cell">Measures</th>
              <th className="th hidden md:table-cell">Last run</th>
              <th className="th">
                <span className="sr-only">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {q.data.map((s) => (
              <tr key={s.key} className="cursor-pointer hover:bg-sunken/70" onClick={() => setView(s.key)}>
                <td className="td">
                  <button className="block w-full text-left text-sm hover:text-accent" onClick={() => setView(s.key)} title={`View ${s.key}`}>
                    <KeyLines itemKey={s.key} />
                  </button>
                  <div className="mt-1 flex flex-wrap items-center gap-1.5 text-xs text-muted">
                    <span className="mono">v{s.version_seq}</span>
                    <span>{s.name}</span>
                    {s.managed_by === "git" && <GitBadge />}
                    {s.required_by_gate && <span className="badge badge-accent" title="Evals must pass before a preview can be published">publish gate</span>}
                    {!s.needs_agent && <span className="badge">no LLM</span>}
                  </div>
                  <div className="mt-0.5 text-xs text-muted sm:hidden">
                    {s.cases} cases{s.last_run_at ? ` · last run ${timeAgo(s.last_run_at)}` : ""}
                  </div>
                </td>
                <td className="td hidden text-right tabular-nums sm:table-cell">{s.cases}</td>
                <td className="td hidden text-xs xl:table-cell">
                  <span className="flex flex-wrap gap-1">
                    {s.measures.map((m) => (
                      <span key={m} className="badge font-mono">
                        {m}
                      </span>
                    ))}
                  </span>
                </td>
                <td className="td hidden whitespace-nowrap text-muted md:table-cell">{s.last_run_at ? timeAgo(s.last_run_at) : "never"}</td>
                <td className="td whitespace-nowrap text-right">
                  {role >= RANK.analyst && (
                    <button
                      className="btn btn-sm"
                      onClick={(e) => {
                        e.stopPropagation();
                        setRunning(s);
                      }}
                    >
                      Run
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </TableCard>
      )}
      <SuiteViewer suiteKey={view} onClose={() => setView(null)} canEdit={role >= RANK.curator} />
      <SuiteEditor open={editing} onClose={() => setEditing(false)} />
      {running && <RunDialog suite={running} onClose={() => setRunning(null)} />}
    </>
  );
}

function SuiteViewer(props: { suiteKey: string | null; onClose: () => void; canEdit: boolean }) {
  const q = useQuery({
    queryKey: ["eval-suite", props.suiteKey],
    queryFn: () => endpoints.getEvalSuite(props.suiteKey ?? ""),
    enabled: props.suiteKey !== null,
  });
  return (
    <Modal open={props.suiteKey !== null} onClose={props.onClose} title={`Suite ${props.suiteKey ?? ""}`} wide>
      {q.isPending && <Loading />}
      {q.isError && <ErrorBox error={q.error} />}
      {q.data && (
        <>
          <p className="mb-2 text-xs text-muted">
            Version {q.data.version_seq} of {q.data.versions.length}.{" "}
            {q.data.managed_by === "git" ? "Managed by git: change it in the context repo." : props.canEdit ? "Edit by saving a new version from New suite." : ""}
          </p>
          <pre className="max-h-[60vh] overflow-auto rounded border border-line bg-sunken p-3 font-mono text-xs">{q.data.yaml}</pre>
        </>
      )}
    </Modal>
  );
}

const TEMPLATE = `id: my-suite
name: My suite
cases:
  - id: example
    prompt: What was revenue last month?
    rubrics:
      - { grader: context_used, items: [glossary/business-terms.yaml#revenue], top_n: 5, measure: retrieval }
`;

function SuiteEditor({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [text, setText] = useState(TEMPLATE);
  const qc = useQueryClient();
  const toast = useToast();
  const save = useMutation({
    mutationFn: () => endpoints.saveEvalSuite(text),
    onSuccess: (s) => {
      toast("ok", `Saved ${s.key} v${s.version_seq}`);
      void qc.invalidateQueries({ queryKey: ["eval-suites"] });
      onClose();
    },
    onError: (e) => toast("err", errMsg(e)),
  });
  return (
    <Modal
      open={open}
      onClose={onClose}
      title="New eval suite"
      wide
      footer={
        <>
          <button className="btn" onClick={onClose}>
            Cancel
          </button>
          <button className="btn btn-primary" disabled={save.isPending} onClick={() => save.mutate()}>
            Save
          </button>
        </>
      }
    >
      <textarea className="input mono w-full" rows={18} value={text} onChange={(e) => setText(e.target.value)} aria-label="Suite YAML" spellCheck={false} />
    </Modal>
  );
}

function RunDialog({ suite, onClose }: { suite: EvalSuite; onClose: () => void }) {
  const rel = useQuery({ queryKey: ["releases", "published"], queryFn: () => endpoints.listReleases("published") });
  const prev = useQuery({ queryKey: ["releases", "preview"], queryFn: () => endpoints.listReleases("preview") });
  const [release, setRelease] = useState("");
  const [model, setModel] = useState("");
  const nav = useNavigate();
  const toast = useToast();
  const qc = useQueryClient();
  const head = rel.data?.releases[0];
  const chosen = release || head?.id || "";
  const start = useMutation({
    mutationFn: () => endpoints.startEvalRun(suite.key, chosen, model.trim()),
    onSuccess: (r) => {
      toast("ok", "Run started");
      void qc.invalidateQueries({ queryKey: ["eval-runs"] });
      nav(`/evals/runs/${r.id}`);
    },
    onError: (e) => toast("err", errMsg(e)),
  });
  return (
    <Modal
      open
      onClose={onClose}
      title={`Run ${suite.key}`}
      footer={
        <>
          <button className="btn" onClick={onClose}>
            Cancel
          </button>
          <button className="btn btn-primary" disabled={!chosen || start.isPending} onClick={() => start.mutate()}>
            Start run
          </button>
        </>
      }
    >
      <label className="mb-3 block">
        <span className="label">Release</span>
        <select className="input w-full" value={chosen} onChange={(e) => setRelease(e.target.value)}>
          {rel.data?.releases.map((r) => (
            <option key={r.id} value={r.id}>
              #{r.seq} published{r.is_current ? " (current)" : ""}
            </option>
          ))}
          {prev.data?.releases.map((r) => (
            <option key={r.id} value={r.id}>
              #{r.seq} preview
            </option>
          ))}
        </select>
      </label>
      {suite.needs_agent ? (
        <label className="block">
          <span className="label">Model override (optional, e.g. claude-haiku-4-5)</span>
          <input className="input w-full" value={model} onChange={(e) => setModel(e.target.value)} />
        </label>
      ) : (
        <Notice>This suite only checks retrieval, so it runs without an LLM.</Notice>
      )}
    </Modal>
  );
}

// ---- runs -----------------------------------------------------------------------------------

function Runs() {
  const q = useQuery({
    queryKey: ["eval-runs"],
    queryFn: () => endpoints.listEvalRuns({}),
    refetchInterval: (query) => (query.state.data?.some((r) => r.status === "running") ? 2000 : false),
  });
  const [picked, setPicked] = useState<string[]>([]);
  const nav = useNavigate();
  const toggle = (id: string) => setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p, id].slice(-2)));
  if (q.isPending) return <Loading />;
  if (q.isError) return <ErrorBox error={q.error} />;
  if (q.data.length === 0) return <EmptyState title="No runs yet">Run a suite from the Suites tab.</EmptyState>;
  return (
    <>
      <SectionHeading
        icon={ICONS.clock}
        title="Run history"
        count={q.data.length}
        actions={
          <button className="btn btn-sm" disabled={picked.length !== 2} onClick={() => nav(`/evals/compare?a=${picked[0]}&b=${picked[1]}`)}>
            Compare
          </button>
        }
      />
      <p className="mb-3 text-xs text-muted">Pick two completed runs (baseline first) to compare.</p>
      <TableCard>
        <thead className="border-b border-line">
          <tr>
            <th className="th w-px">
              <span className="sr-only">Pick to compare</span>
            </th>
            <th className="th w-full">Suite</th>
            <th className="th">Status</th>
            <th className="th hidden sm:table-cell">Cases</th>
            <th className="th hidden xl:table-cell">Measures</th>
            <th className="th hidden lg:table-cell">Model</th>
            <th className="th">Started</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-line">
          {q.data.map((r) => (
            <RunRow key={r.id} r={r} checked={picked.includes(r.id)} onCheck={() => toggle(r.id)} onOpen={() => nav(`/evals/runs/${r.id}`)} />
          ))}
        </tbody>
      </TableCard>
    </>
  );
}

function RunRow({ r, checked, onCheck, onOpen }: { r: EvalRun; checked: boolean; onCheck: () => void; onOpen: () => void }) {
  return (
    <tr className={`cursor-pointer ${checked ? "bg-accent-soft/70 shadow-[inset_2px_0_0_rgb(var(--accent))]" : "hover:bg-sunken/70"}`} onClick={onOpen}>
      <td className="td" onClick={(e) => e.stopPropagation()}>
        <input
          type="checkbox"
          aria-label={`Select run of ${r.suite_key}`}
          title={r.status === "completed" ? "Pick two runs to compare" : "Only completed runs can be compared"}
          checked={checked}
          onChange={onCheck}
          disabled={r.status !== "completed"}
        />
      </td>
      <td className="td">
        <Link to={`/evals/runs/${r.id}`} className="block hover:text-accent" onClick={(e) => e.stopPropagation()}>
          <KeyLines itemKey={r.suite_key} />
        </Link>
        <div className="mt-0.5 text-xs text-muted">
          <span className="mono">v{r.suite_version}</span> on #{r.release_seq} {r.release_kind}
          <span className="sm:hidden">
            {" "}
            · {r.cases_pass}/{r.cases_total} pass
          </span>
        </div>
      </td>
      <td className="td">
        <OutcomeBadge outcome={r.status} />
      </td>
      <td className="td hidden whitespace-nowrap tabular-nums sm:table-cell">
        {r.cases_pass}/{r.cases_total} pass
        {r.cases_fail ? <span className="text-bad"> · {r.cases_fail} fail</span> : null}
        {r.cases_error ? <span className="text-bad"> · {r.cases_error} error</span> : null}
        {r.cases_skipped ? <span className="text-muted"> · {r.cases_skipped} skipped</span> : null}
      </td>
      <td className="td hidden max-w-[26rem] xl:table-cell">
        <MeasureChips measures={r.measures} />
      </td>
      <td className="td hidden whitespace-nowrap text-muted lg:table-cell">{r.model ?? "no LLM"}</td>
      <td className="td whitespace-nowrap text-muted">{timeAgo(r.started_at)}</td>
    </tr>
  );
}

export function EvalRunPage() {
  const { id = "" } = useParams();
  const q = useQuery({
    queryKey: ["eval-run", id],
    queryFn: () => endpoints.getEvalRun(id),
    refetchInterval: (query) => (query.state.data?.status === "running" ? 2000 : false),
  });
  if (q.isPending) return <Loading />;
  if (q.isError) return <ErrorBox error={q.error} />;
  const r = q.data;
  return (
    <>
      <Crumb to="/evals?tab=runs" label="Evals" rest="Run history" />
      <PageHeader
        title={`Run of ${r.suite_key} v${r.suite_version}`}
        sub={`On release #${r.release_seq} (${r.release_kind}) · ${r.model ?? "no LLM"} · ${r.tokens.input ?? 0} in / ${r.tokens.output ?? 0} out tokens`}
        actions={
          <Link className="btn" to="/evals?tab=runs">
            All runs
          </Link>
        }
      />
      <section aria-labelledby="run-sum" className="mb-9">
        <SectionHeading id="run-sum" icon={ICONS.pulse} title="Summary" actions={<OutcomeBadge outcome={r.status} />} />
        <div className="card overflow-hidden">
          <StatGrid>
            <StatCell label="Passed" tone="good" value={`${r.cases_pass}/${r.cases_total}`} info="Cases that passed all their graders." />
            <StatCell label="Failed" tone="bad" glyph="triangle" value={r.cases_fail} secondary={r.cases_error ? `${r.cases_error} errors` : undefined} info="Cases where a grader failed, plus cases that errored." />
            <StatCell label="Skipped" tone="muted" glyph="ring" value={r.cases_skipped} info="Cases that did not run, for example agent-driven cases without an LLM." />
            <StatCell label="Tokens" tone="info" glyph="diamond" value={(r.tokens.input ?? 0) + (r.tokens.output ?? 0)} secondary={`${r.tokens.input ?? 0} in · ${r.tokens.output ?? 0} out`} info="Model tokens this run used." />
          </StatGrid>
          <div className="flex flex-wrap items-center gap-3 border-t border-line px-4 py-3 sm:px-5">
            <span className="label-caps">Measures</span>
            <MeasureChips measures={r.measures} />
          </div>
        </div>
        <div className="mt-3 space-y-3">
          {r.error && <Notice tone="warn">{r.error}</Notice>}
          {r.status === "running" && <Loading label="Running" />}
          {!r.llm && r.cases_skipped > 0 && (
            <Notice tone="warn">No LLM was available, so agent-driven cases were skipped. Set ANTHROPIC_API_KEY or SATCHEL_CHAT_PROVIDER.</Notice>
          )}
        </div>
      </section>
      <SectionHeading icon={ICONS.list} title="Cases" count={r.cases.length} />
      <div className="space-y-3">
        {r.cases.map((c) => (
          <CaseCard key={c.case_key} c={c} />
        ))}
      </div>
    </>
  );
}

function CaseCard({ c }: { c: EvalCase }) {
  const [open, setOpen] = useState(c.outcome !== "pass");
  return (
    <section className="card overflow-hidden" aria-label={`Case ${c.case_key}`}>
      <button className="flex w-full flex-wrap items-center gap-2 px-4 py-3 text-left" onClick={() => setOpen(!open)} aria-expanded={open}>
        <Chevron open={open} />
        <OutcomeBadge outcome={c.outcome} />
        <span className="break-all font-mono text-sm">{c.case_key}</span>
        {c.tags.map((t) => (
          <span key={t} className="badge">
            {t}
          </span>
        ))}
        <span className="w-full min-w-0 truncate pl-5 text-xs text-muted sm:ml-2 sm:w-auto sm:flex-1 sm:pl-0">{c.prompt}</span>
      </button>
      {open && (
        <div className="space-y-3 border-t border-line p-4">
          {c.attempts.map((a) => (
            <div key={a.attempt}>
              <div className="mb-1 flex flex-wrap items-center gap-2 text-xs text-muted">
                <span>Attempt {a.attempt}</span>
                <OutcomeBadge outcome={a.outcome} />
                {a.session_id && (
                  <Link className="text-accent hover:underline" to={`/sessions/${a.session_id}`}>
                    session inspector
                  </Link>
                )}
                {a.cost["steps"] != null && <span>{String(a.cost["steps"])} steps</span>}
              </div>
              {a.answer && <p className="mb-2 whitespace-pre-wrap rounded border border-line bg-sunken px-3 py-2 text-sm">{a.answer}</p>}
              <ul className="space-y-1">
                {a.grades.map((g, i) => (
                  <li key={i} className="flex flex-wrap items-baseline gap-2 text-sm">
                    <OutcomeBadge outcome={g.outcome} />
                    <span className="font-mono text-xs">{g.grader}</span>
                    <span className="text-xs text-muted">
                      {g.measure}
                      {g.warn_only ? " · warn only" : ""}
                    </span>
                    <span className="min-w-0 flex-1 text-xs">{g.reasoning}</span>
                  </li>
                ))}
              </ul>
            </div>
          ))}
          {c.attempts.length === 0 && <p className="text-sm text-muted">Not run yet.</p>}
        </div>
      )}
    </section>
  );
}

export function EvalComparePage() {
  const [sp] = useSearchParams();
  const a = sp.get("a") ?? "";
  const b = sp.get("b") ?? "";
  const q = useQuery({ queryKey: ["eval-compare", a, b], queryFn: () => endpoints.compareEvalRuns(a, b), enabled: Boolean(a && b) });
  if (!a || !b)
    return (
      <EmptyState
        title="Pick two runs"
        action={
          <Link className="btn" to="/evals?tab=runs">
            Open run history
          </Link>
        }
      >
        Tick two completed runs in Run history (baseline first), then choose Compare.
      </EmptyState>
    );
  if (q.isPending) return <Loading />;
  if (q.isError) return <ErrorBox error={q.error} />;
  const d = q.data;
  const label = (r: EvalRun) => `${r.suite_key} on #${r.release_seq} ${r.release_kind}`;
  return (
    <>
      <Crumb to="/evals?tab=runs" label="Evals" rest="Run history" />
      <PageHeader
        title="Compare runs"
        sub={`${label(d.a as EvalRun)} → ${label(d.b as EvalRun)}`}
        actions={
          <Link className="btn" to="/evals?tab=runs">
            All runs
          </Link>
        }
      />
      <SectionHeading icon={ICONS.layers} title="Measures" count={d.measures.length} />
      <section className="card overflow-x-auto p-4">
        <DeltaTable rows={d.measures} aLabel="Baseline" bLabel="Candidate" />
        <CaseMoves failing={d.newly_failing} passing={d.newly_passing} />
        {(d.only_in_a.length > 0 || d.only_in_b.length > 0) && (
          <p className="mt-2 text-xs text-muted">
            Only in baseline: {d.only_in_a.join(", ") || "none"} · only in candidate: {d.only_in_b.join(", ") || "none"}
          </p>
        )}
      </section>
    </>
  );
}
