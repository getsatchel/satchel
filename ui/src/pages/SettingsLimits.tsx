import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { endpoints } from "@/api/endpoints";
import type { DeploymentGroup, LimitRow, LimitValue } from "@/api/types";
import { useToast } from "@/components/Toast";
import { ErrorBox, Loading, SectionHeading, StatusDot } from "@/components/ui";
import { errMsg } from "@/lib/format";

/** Draft per limit: "" = use the deployment default, "forever" = no limit, else digits. */
type Draft = Record<string, string>;

function draftOf(rows: LimitRow[]): Draft {
  return Object.fromEntries(
    rows.map((r) => [r.name, !r.overridden ? "" : r.workspace == null ? "forever" : String(r.workspace)]),
  );
}

function fmt(v: number | null, r: LimitRow): string {
  if (v == null) return r.null_label ?? "No limit";
  return `${v.toLocaleString()} ${r.unit}`;
}

function problem(r: LimitRow, d: string): string | null {
  if (d === "" || d === "forever") return null;
  if (!/^\d+$/.test(d)) return "Whole numbers only.";
  const n = Number(d);
  if (n < r.min || n > r.max) return `Between ${r.min.toLocaleString()} and ${r.max.toLocaleString()}.`;
  return null;
}

function LimitField(props: { r: LimitRow; value: string; onChange: (v: string) => void; admin: boolean }) {
  const { r, value, onChange, admin } = props;
  const id = `limit-${r.name}`;
  const err = problem(r, value);
  const mode = value === "" ? "default" : value === "forever" ? "forever" : "custom";
  return (
    <div className="grid gap-x-6 gap-y-2 px-4 py-4 sm:grid-cols-[minmax(0,1fr)_17rem] sm:px-5">
      <div className="min-w-0">
        <label htmlFor={id} className="text-sm font-medium text-ink">{r.label}</label>
        <p className="mt-0.5 text-xs text-muted">{r.help}</p>
        <p className="mt-1.5 text-xs text-muted">
          Deployment default: <span className="text-ink/80">{fmt(r.deployment ?? null, r)}</span>
          <span className="mx-1.5 text-muted/50">·</span>
          <span className="font-mono">{r.env}</span>
        </p>
      </div>
      <div className="min-w-0">
        {admin ? (
          <>
            <select
              className="input w-full"
              aria-label={`${r.label}: source`}
              value={mode}
              onChange={(e) => {
                const m = e.target.value;
                onChange(m === "default" ? "" : m === "forever" ? "forever" : String(r.value ?? r.deployment ?? (r.nullable ? Math.max(r.min, 90) : r.min)));
              }}
            >
              <option value="default">Use deployment default</option>
              {r.nullable && <option value="forever">{r.null_label ?? "No limit"}</option>}
              <option value="custom">{r.nullable ? "Delete after…" : "Custom value"}</option>
            </select>
            {mode === "custom" && (
              <div className="mt-2 flex items-center gap-2">
                <input
                  id={id}
                  className="input mono w-full tabular-nums"
                  inputMode="numeric"
                  value={value}
                  onChange={(e) => onChange(e.target.value.trim())}
                  aria-invalid={Boolean(err)}
                  aria-describedby={err ? `${id}-err` : undefined}
                />
                <span className="shrink-0 text-sm text-muted">{r.unit}</span>
              </div>
            )}
            {err && <p id={`${id}-err`} className="mt-1 text-xs text-bad">{err}</p>}
          </>
        ) : (
          <p id={id} className="text-sm tabular-nums sm:pt-0.5 sm:text-right">
            {fmt(r.value ?? null, r)}
            {r.overridden && <span className="badge ml-2">workspace</span>}
          </p>
        )}
      </div>
    </div>
  );
}

export function LimitsSection({ admin }: { admin: boolean }) {
  const qc = useQueryClient();
  const toast = useToast();
  const q = useQuery({ queryKey: ["limits"], queryFn: endpoints.getLimits });
  const [draft, setDraft] = useState<Draft>({});
  const base = useMemo(() => (q.data ? draftOf(q.data) : {}), [q.data]);
  useEffect(() => setDraft(base), [base]);

  const changed = q.data?.filter((r) => (draft[r.name] ?? "") !== (base[r.name] ?? "")) ?? [];
  const invalid = q.data?.some((r) => problem(r, draft[r.name] ?? "")) ?? false;
  const save = useMutation({
    mutationFn: () => {
      const values: Record<string, LimitValue> = {};
      for (const r of changed) {
        const d = draft[r.name] ?? "";
        values[r.name] = d === "" ? "default" : d === "forever" ? null : Number(d);
      }
      return endpoints.putLimits(values);
    },
    onSuccess: (rows) => {
      qc.setQueryData(["limits"], rows);
      toast("ok", "Limits saved");
    },
    onError: (e) => toast("err", errMsg(e)),
  });

  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  const rows = q.data ?? [];
  return (
    <section>
      <SectionHeading title="Limits and retention" />
      <p className="mb-4 max-w-2xl text-sm text-muted">
        The deployment's environment sets the defaults. Values set here apply to this workspace only. Changes take effect on the next query; retention runs with the worker's next pass.
      </p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (changed.length && !invalid) save.mutate();
        }}
      >
        <div className="card overflow-hidden">
          <div className="divide-y divide-line">
            {rows.map((r) => (
              <LimitField key={r.name} r={r} admin={admin} value={draft[r.name] ?? ""} onChange={(v) => setDraft((d) => ({ ...d, [r.name]: v }))} />
            ))}
          </div>
          {admin && (
              <div className="flex flex-wrap items-center justify-end gap-2 border-t border-line px-4 py-3 sm:px-5">
                {changed.length > 0 && (
                  <button type="button" className="btn btn-sm" onClick={() => setDraft(base)} disabled={save.isPending}>
                    Discard
                  </button>
                )}
                <button type="submit" className="btn btn-primary btn-sm" disabled={!changed.length || invalid || save.isPending}>
                  {save.isPending ? "Saving..." : changed.length ? `Save ${changed.length} change${changed.length > 1 ? "s" : ""}` : "Save"}
                </button>
              </div>
          )}
        </div>
      </form>
    </section>
  );
}

function GroupTable({ g }: { g: DeploymentGroup }) {
  return (
    <section>
      <SectionHeading title={g.title} />
      <div className="card overflow-hidden">
        <table className="w-full table-fixed text-sm">
          <colgroup>
            <col className="w-[38%] sm:w-[30%]" />
            <col />
            <col className="hidden w-[16rem] md:table-column" />
          </colgroup>
          <tbody>
            {g.rows.map((r) => (
              <tr key={r.label} className="border-t border-line first:border-t-0">
                <td className="td align-top text-muted">{r.label}</td>
                <td className="td align-top">
                  {r.secret ? (
                    r.value ? <StatusDot tone="good">set</StatusDot> : <StatusDot tone="muted">not set</StatusDot>
                  ) : r.value == null ? (
                    <span className="text-muted">–</span>
                  ) : (
                    <span className="font-mono text-xs [overflow-wrap:anywhere]">{r.value}</span>
                  )}
                  {r.source === "default" && r.value != null && !r.secret && <span className="ml-2 text-2xs text-muted">default</span>}
                </td>
                <td className="td hidden align-top font-mono text-2xs text-muted [overflow-wrap:anywhere] md:table-cell">{r.env ?? ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

export function DeploymentSection() {
  const q = useQuery({ queryKey: ["deployment"], queryFn: endpoints.getDeployment });
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  const d = q.data;
  if (!d) return null;
  return (
    <>
      <p className="max-w-2xl text-sm text-muted">
        Satchel <span className="font-mono text-ink/80">{d.version}</span>. This is the server's configuration, read-only: change it in the deployment's environment (the <span className="font-mono">SATCHEL_*</span> variables) and restart. Secrets show only whether they're set.
      </p>
      {d.groups.map((g) => (
        <GroupTable key={g.title} g={g} />
      ))}
    </>
  );
}
