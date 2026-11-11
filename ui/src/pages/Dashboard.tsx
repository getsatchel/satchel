import { useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import { endpoints } from "@/api/endpoints";
import type { AssetNode, EvalRun, ObservabilitySummary } from "@/api/types";
import { HeaderActions } from "@/components/CommandPalette";
import {
  CardFooter,
  CardHeader,
  Dropdown,
  ErrorBox,
  Icon,
  ICONS,
  PageHeader,
  PillButton,
  SectionHeading,
  StatCell,
  StatGrid,
  StatusLine,
} from "@/components/ui";
import { fullTime, timeAgo } from "@/lib/format";

const DASH = "–";
const PERIODS = [7, 30, 90] as const;

function Skel({ w = "w-12" }: { w?: string }) {
  return <span className={`inline-block h-5 ${w} animate-pulse rounded bg-sunken align-middle`} aria-hidden />;
}

const fmt = (n: number | null | undefined) => (n == null || Number.isNaN(n) ? DASH : n.toLocaleString());
const pct = (num: number, den: number) => (den > 0 ? `${Math.round((num / den) * 100)}%` : DASH);

/** Value of a query for a stat: skeleton while loading, a dash on error or when absent. */
function val<T>(q: UseQueryResult<T>, pick: (d: T) => ReactNode): ReactNode {
  if (q.isPending) return <Skel />;
  if (q.isError || q.data === undefined) return DASH;
  return pick(q.data);
}
function sec<T>(q: UseQueryResult<T>, pick: (d: T) => ReactNode): ReactNode {
  if (q.isPending || q.isError || q.data === undefined) return null;
  return pick(q.data);
}

function coverage(nodes: AssetNode[]) {
  const c = { tables: 0, documented: 0, endorsed: 0, excluded: 0 };
  const walk = (n: AssetNode) => {
    if ((n.kind === "table" || n.kind === "view") && !n.dropped) {
      c.tables++;
      if (n.has_doc) c.documented++;
      if (n.endorsed) c.endorsed++;
      if (n.excluded_from_ai) c.excluded++;
    }
    n.children.forEach(walk);
  };
  nodes.forEach(walk);
  return c;
}

function latestFinished(runs: EvalRun[]): EvalRun | undefined {
  return runs
    .filter((r) => r.finished_at && r.status !== "running")
    .sort((a, b) => String(b.finished_at).localeCompare(String(a.finished_at)))[0];
}

function signalCount(s: ObservabilitySummary, kind: string) {
  return s.signals_by_kind.find((k) => k.kind === kind)?.count ?? 0;
}

export function Dashboard() {
  return (
    <>
      <PageHeader title="Dashboard" sub="What needs a curator, and how agents are using your context." actions={<HeaderActions />} />
      <div className="space-y-9">
        <NeedsReview />
        <AgentActivity />
        <ContextHealth />
      </div>
    </>
  );
}

function NeedsReview() {
  const sug = useQuery({
    queryKey: ["suggestions", "open", "evidence"],
    queryFn: () => endpoints.listSuggestions({ status: "open", sort: "evidence" }),
  });
  const props = useQuery({ queryKey: ["proposals"], queryFn: endpoints.listProposals });
  const changes = useQuery({ queryKey: ["changes"], queryFn: endpoints.listChanges });

  const rows = [
    ...(sug.data?.suggestions ?? []).map((s) => ({
      id: `s-${s.id}`,
      kind: "Suggestion",
      tone: "warn" as const,
      at: s.updated_at,
      title: s.title,
      detail: s.summary.split("\n")[0],
      meta: `${s.evidence_count} signals · ${s.pending_changes} pending change${s.pending_changes === 1 ? "" : "s"}`,
    })),
    ...(props.data?.proposals ?? []).map((p) => ({
      id: `p-${p.key}`,
      kind: "Agent proposal",
      tone: "info" as const,
      at: p.created_at,
      title: p.key,
      detail: p.rationale ?? p.session_question ?? "",
      meta: p.proposed_by_label ? `from ${p.proposed_by_label}` : "",
    })),
  ]
    .sort((a, b) => b.at.localeCompare(a.at))
    .slice(0, 3);

  const byKind = (k: string) => changes.data?.changes.filter((c) => c.kind === k).length ?? 0;
  const loadingList = sug.isPending || props.isPending;

  return (
    <section aria-labelledby="dash-review">
      <SectionHeading id="dash-review" icon={ICONS.inbox} title="Needs review" />
      <div className="grid items-start gap-4 lg:grid-cols-3">
        <div className="card overflow-hidden lg:col-span-2">
          <StatGrid cols={2}>
            <StatCell
              label="Suggestions"
              tone="warn"
              value={val(sug, (d) => fmt(d.total))}
              secondary={sec(sug, (d) => `${d.suggestions.reduce((n, s) => n + s.pending_changes, 0)} changes`)}
              info="Gaps that curation clustered from agent signals and context checks, with drafted fixes to accept or reject."
            />
            <StatCell
              label="Proposals"
              tone="info"
              glyph="diamond"
              value={val(props, (d) => fmt(d.proposals.length))}
              info="Edits agents proposed during sessions, for example after a user corrected an answer."
            />
          </StatGrid>
          {(sug.isError || props.isError) && (
            <div className="border-t border-line p-4">
              <ErrorBox error={sug.error ?? props.error} />
            </div>
          )}
          <ul className="divide-y divide-line border-t border-line" aria-label="Latest items to review" aria-busy={loadingList}>
            {loadingList &&
              [0, 1, 2].map((i) => (
                <li key={i} className="space-y-2 px-4 py-3.5 sm:px-5">
                  <Skel w="w-40" />
                  <Skel w="w-64 max-w-full" />
                </li>
              ))}
            {!loadingList && rows.length === 0 && !sug.isError && !props.isError && (
              <li className="px-4 py-6 text-center text-sm text-muted sm:px-5">Nothing waiting. New suggestions appear after curation runs on fresh agent signals.</li>
            )}
            {rows.map((r) => (
              <li key={r.id}>
                <Link to="/inbox" className="group flex items-start gap-3 px-4 py-3.5 hover:bg-sunken/60 sm:px-5">
                  <div className="min-w-0 flex-1">
                    <StatusLine tone={r.tone} state={r.kind} time={timeAgo(r.at)} title={fullTime(r.at)} />
                    <div className="mt-1 truncate text-sm font-medium [overflow-wrap:anywhere]">{r.title}</div>
                    {r.detail && <div className="truncate text-sm text-muted">{r.detail}</div>}
                    {r.meta && <div className="mt-0.5 font-mono text-xs text-muted/80">{r.meta}</div>}
                  </div>
                  <Icon d={ICONS.chevron} size={14} className="mt-1 text-muted/60 group-hover:text-ink" />
                </Link>
              </li>
            ))}
          </ul>
          <CardFooter>
            <PillButton to="/inbox">Open inbox</PillButton>
          </CardFooter>
        </div>

        <div className="card overflow-hidden">
          <StatGrid cols={2}>
            <div className="col-span-2">
              <StatCell
                label="Unpublished"
                tone="warn"
                glyph="square"
                value={val(changes, (d) => fmt(d.changes.length))}
                secondary={sec(changes, () => `${byKind("create")} new · ${byKind("update")} updated · ${byKind("deprecate")} deprecated`)}
                info="Drafts and edits that agents do not see yet. Preview them, run the eval gate, then publish a release."
              />
            </div>
          </StatGrid>
          {changes.isError && (
            <div className="border-t border-line p-4">
              <ErrorBox error={changes.error} />
            </div>
          )}
          <p className="border-t border-line px-4 py-3.5 text-sm text-muted sm:px-5">
            Agents keep reading the current release until you publish a preview.
          </p>
          <CardFooter>
            <PillButton to="/changes">Preview &amp; publish</PillButton>
          </CardFooter>
        </div>
      </div>
    </section>
  );
}

function AgentActivity() {
  const [days, setDays] = useState<(typeof PERIODS)[number]>(30);
  const q = useQuery({ queryKey: ["summary", days], queryFn: () => endpoints.summary(days), placeholderData: (p) => p });
  const s = (pick: (d: ObservabilitySummary) => ReactNode) => val(q, pick);
  const s2 = (pick: (d: ObservabilitySummary) => ReactNode) => sec(q, pick);
  return (
    <section aria-labelledby="dash-activity">
      <SectionHeading id="dash-activity" icon={ICONS.pulse} title="Agent activity" />
      <div className="card overflow-hidden">
        <CardHeader
          actions={
            <Dropdown
              label="Time range"
              menuClassName="btn btn-ghost -mr-2 font-mono text-2xs font-semibold uppercase tracking-[0.08em]"
              items={PERIODS.map((p) => ({ label: `Last ${p} days`, onSelect: () => setDays(p), hint: p === days ? "✓" : undefined }))}
              trigger={
                <>
                  Last {days} days
                  <Icon d={ICONS.down} size={13} strokeWidth={2.2} />
                </>
              }
            />
          }
        >
          How agents used your context.
        </CardHeader>
        {q.isError ? (
          <div className="p-4">
            <ErrorBox error={q.error} />
          </div>
        ) : (
          <StatGrid>
            <StatCell label="Sessions" tone="info" value={s((d) => fmt(d.total_sessions))} info="Analyses an agent ran against this workspace in the period." />
            <StatCell
              label="Tool calls"
              tone="muted"
              glyph="ring"
              value={s((d) => fmt(d.top_tools.reduce((n, t) => n + t.calls, 0)))}
              secondary={s2((d) => `${d.top_tools.length} tools`)}
              info="Calls agents made to Satchel tools such as search and context packs."
            />
            <StatCell
              label="Tool errors"
              tone="bad"
              glyph="triangle"
              value={s((d) => fmt(d.top_tools.reduce((n, t) => n + t.errors, 0)))}
              secondary={s2((d) => {
                const calls = d.top_tools.reduce((n, t) => n + t.calls, 0);
                return calls ? pct(d.top_tools.reduce((n, t) => n + t.errors, 0), calls) : null;
              })}
              info="Tool calls that returned an error, as a count and a share of all calls."
            />
            <StatCell
              label="Signals"
              tone="warn"
              glyph="square"
              value={s((d) => fmt(d.signals_by_kind.reduce((n, k) => n + k.count, 0)))}
              secondary={s2((d) => `${d.signals_by_kind.length} kinds`)}
              info="Everything agents and checks flagged: gaps, SQL errors, doubts, divergences. Curation turns these into suggestions."
            />
            <StatCell
              label="Gaps flagged"
              tone="warn"
              glyph="triangle"
              value={s((d) => fmt(signalCount(d, "gap_flagged")))}
              info="Times an agent said the context did not cover what it needed."
            />
            <StatCell
              label="User doubt"
              tone="accent"
              glyph="diamond"
              value={s((d) => fmt(signalCount(d, "user_doubt")))}
              info="Times a user questioned or corrected an agent's answer."
            />
            <StatCell
              label="Thumbs up"
              tone="good"
              value={s((d) => fmt(d.feedback.positive))}
              secondary={s2((d) => `${d.feedback.human_positive} human`)}
              info="Positive feedback from people and agents. The secondary number is the human share."
            />
            <StatCell
              label="Thumbs down"
              tone="bad"
              value={s((d) => fmt(d.feedback.negative))}
              secondary={s2((d) => `${d.feedback.human_negative} human`)}
              info="Negative feedback from people and agents. The secondary number is the human share."
            />
          </StatGrid>
        )}
        <CardFooter>
          <PillButton to="/sessions">View sessions</PillButton>
          <PillButton to="/observability">Observability</PillButton>
        </CardFooter>
      </div>
    </section>
  );
}

/** Item count for one state, from the list endpoint's total (limit 1 keeps it cheap). */
function useItemCount(state: string) {
  return useQuery({ queryKey: ["items", { state, limit: 1 }], queryFn: () => endpoints.listItems({ state, limit: 1 }) });
}

function ContextHealth() {
  const pub = useItemCount("published");
  const draft = useItemCount("draft");
  const proposed = useItemCount("proposed");
  const assets = useQuery({ queryKey: ["assets", "", ""], queryFn: () => endpoints.listAssets({ connection: "", q: "" }) });
  const releases = useQuery({ queryKey: ["releases"], queryFn: () => endpoints.listReleases("published") });
  const runs = useQuery({ queryKey: ["eval-runs"], queryFn: () => endpoints.listEvalRuns({}) });

  const cov = (pick: (c: ReturnType<typeof coverage>) => ReactNode) => val(assets, (d) => pick(coverage(d.assets)));
  const cov2 = (pick: (c: ReturnType<typeof coverage>) => ReactNode) => sec(assets, (d) => pick(coverage(d.assets)));
  const current = releases.data?.releases.find((r) => r.is_current) ?? releases.data?.releases[0];
  const run = runs.data ? latestFinished(runs.data) : undefined;
  const runDen = run ? run.cases_pass + run.cases_fail + run.cases_error : 0;
  const runRate = run && runDen ? run.cases_pass / runDen : null;
  const firstError = [pub, draft, proposed, assets, releases, runs].find((x) => x.isError)?.error;

  return (
    <section aria-labelledby="dash-health">
      <SectionHeading id="dash-health" icon={ICONS.heart} title="Context health" />
      <div className="card overflow-hidden">
        <CardHeader>Is the context agents read complete and trusted?</CardHeader>
        {firstError != null && (
          <div className="border-b border-line p-4">
            <ErrorBox error={firstError} />
          </div>
        )}
        <StatGrid>
          <StatCell
            label="Doc coverage"
            tone="good"
            glyph="ring"
            value={cov((c) => pct(c.documented, c.tables))}
            secondary={cov2((c) => `${c.documented}/${c.tables} tables`)}
            info="Share of live tables and views in the catalog that have a context doc."
          />
          <StatCell label="Endorsed" tone="good" glyph="square" value={cov((c) => fmt(c.endorsed))} info="Tables and views a curator marked as the trusted source." />
          <StatCell label="Excluded" tone="bad" glyph="square" value={cov((c) => fmt(c.excluded))} info="Tables and views hidden from AI agents." />
          <StatCell
            label="Release"
            tone="accent"
            glyph="diamond"
            value={val(releases, () => (current ? `v${current.seq}` : DASH))}
            secondary={sec(releases, () => (current ? timeAgo(current.created_at) : null))}
            info="The release agents read right now, and when it was published."
          />
          <StatCell
            label="Published"
            tone="good"
            value={val(pub, (d) => fmt(d.total))}
            info="Context items live in the current release."
          />
          <StatCell label="Drafts" tone="warn" value={val(draft, (d) => fmt(d.total))} info="Items with edits that are not published yet." />
          <StatCell label="Proposed" tone="info" value={val(proposed, (d) => fmt(d.total))} info="Items created from a proposal and waiting for a curator." />
          <StatCell
            label="Last eval"
            tone={runRate == null ? "muted" : runRate >= 0.9 ? "good" : runRate >= 0.7 ? "warn" : "bad"}
            glyph="triangle"
            value={val(runs, () => (runRate == null ? DASH : `${Math.round(runRate * 100)}%`))}
            secondary={sec(runs, () => (run ? `${run.suite_key.split("/").pop()} · ${timeAgo(run.finished_at)}` : null))}
            info="Pass rate of the most recent finished eval run (passed cases over passed, failed and errored)."
          />
        </StatGrid>
        <CardFooter>
          <PillButton to="/catalog">View catalog</PillButton>
          <PillButton to="/evals">View evals</PillButton>
        </CardFooter>
      </div>
    </section>
  );
}
