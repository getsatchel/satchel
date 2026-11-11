import type { CaseMove, MeasureDelta, MeasureStat } from "@/api/types";
import { StatusDot, type Tone } from "@/components/ui";

export function pct(v: number | null | undefined): string {
  return v == null ? "n/a" : `${Math.round(v * 100)}%`;
}

const OUTCOME_TONE: Record<string, Tone> = {
  pass: "good",
  completed: "good",
  fail: "bad",
  error: "bad",
  failed: "bad",
  running: "warn",
  skipped: "muted",
};

/** The state of a case, attempt, grade or run: a dot plus the outcome word. */
export function OutcomeBadge({ outcome }: { outcome: string }) {
  return <StatusDot tone={OUTCOME_TONE[outcome] ?? "muted"}>{outcome}</StatusDot>;
}

/** Pass rate per measure for one run. */
export function MeasureChips({ measures }: { measures: MeasureStat[] }) {
  if (measures.length === 0) return <span className="text-xs text-muted">no measures</span>;
  return (
    <span className="flex flex-wrap gap-1.5">
      {measures.map((m) => (
        <span key={m.measure} className={`badge whitespace-nowrap tabular-nums ${m.pass_rate != null && m.pass_rate < 1 ? "badge-warn" : ""}`} title={`pass ${m.passed} · fail ${m.failed} · error ${m.errored} · skipped ${m.skipped}`}>
          {m.measure} {pct(m.pass_rate)}
        </span>
      ))}
    </span>
  );
}

/** A -> B pass rate per measure with the delta; regressed rows are highlighted. */
export function DeltaTable(props: { rows: MeasureDelta[]; aLabel: string; bLabel: string; regressed?: Set<string> }) {
  if (props.rows.length === 0) return <p className="text-sm text-muted">No measures to compare.</p>;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead className="border-b border-line">
          <tr>
            <th className="th">Measure</th>
            <th className="th">{props.aLabel}</th>
            <th className="th">{props.bLabel}</th>
            <th className="th">Change</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-line">
          {props.rows.map((r) => {
            const bad = props.regressed?.has(r.measure) || (r.delta != null && r.delta < 0);
            return (
              <tr key={r.measure}>
                <td className="td font-mono [overflow-wrap:anywhere]">{r.measure}</td>
                <td className="td tabular-nums">{pct(r.a)}</td>
                <td className="td tabular-nums">{pct(r.b)}</td>
                <td className={`td whitespace-nowrap tabular-nums ${bad ? "text-bad" : r.delta != null && r.delta > 0 ? "text-good" : "text-muted"}`}>
                  {r.delta == null ? "n/a" : `${r.delta > 0 ? "+" : ""}${Math.round(r.delta * 100)} pts`}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function CaseMoves(props: { failing: CaseMove[]; passing: CaseMove[] }) {
  if (props.failing.length === 0 && props.passing.length === 0) return null;
  return (
    <div className="mt-3 space-y-1 text-sm">
      {props.failing.map((c) => (
        <div key={`f${c.case}`} className="flex flex-wrap items-center gap-x-2">
          <span className="badge badge-bad">newly failing</span>
          <span className="break-all font-mono">{c.case}</span>
        </div>
      ))}
      {props.passing.map((c) => (
        <div key={`p${c.case}`} className="flex flex-wrap items-center gap-x-2">
          <span className="badge badge-good">newly passing</span>
          <span className="break-all font-mono">{c.case}</span>
        </div>
      ))}
    </div>
  );
}
