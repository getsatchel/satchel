import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { endpoints } from "@/api/endpoints";
import type { SourceOut, SyncReportOut } from "@/api/types";
import { useToast } from "@/components/Toast";
import { EmptyState, ErrorBox, ICONS, Icon, Loading, PageHeader, SectionHeading, StatCell, StatGrid, StatusDot, TableCard } from "@/components/ui";
import { pageIcon } from "@/components/nav";
import { SourceForm } from "@/pages/SourceForm";
import { errMsg, fullTime, timeAgo } from "@/lib/format";

export interface Warning {
  code: string;
  message: string;
  key?: string | null;
}

/** Warnings stored in the last sync report (the report is free-form JSON from the server). */
export function warningsOf(s: SourceOut): Warning[] {
  const w = (s.last_report as { warnings?: unknown }).warnings;
  return Array.isArray(w) ? (w as Warning[]) : [];
}

/** Overlap warnings (another source describes the same table or model) are shown as overlaps,
 * not as problems with the sync. */
const OVERLAP_CODES = new Set(["also_described_by", "semantic_shadowed"]);
export const problemsOf = (s: SourceOut) => warningsOf(s).filter((w) => !OVERLAP_CODES.has(w.code));
const overlapCountOf = (s: SourceOut) => warningsOf(s).length - problemsOf(s).length;

export function SyncStatus({ s }: { s: SourceOut }) {
  if (s.last_status === "ok") return <StatusDot tone="good">synced</StatusDot>;
  if (s.last_status === "error") return <StatusDot tone="bad">failed</StatusDot>;
  return <StatusDot tone="muted">never synced</StatusDot>;
}

/** The row's adapter line: kind and version. Flags appear only when they say something: an
 * adapter from a package, or one still experimental. The rest is in the detail. */
export function AdapterTags({ s }: { s: SourceOut }) {
  const a = s.adapter;
  if (!a) return <span className="badge badge-bad">adapter not installed</span>;
  if (!a.available) {
    return (
      <span className="block text-xs text-muted">
        <span className="badge font-mono">{a.kind}</span> unavailable{a.missing?.length ? `: missing ${a.missing.join(", ")}` : ""}
        {a.install_hint && <span className="block font-mono [overflow-wrap:anywhere]">{a.install_hint}</span>}
      </span>
    );
  }
  return (
    <span className="flex flex-wrap items-center gap-1.5">
      <span className="badge font-mono">{a.kind}</span>
      {a.version && <span className="font-mono text-xs text-muted">v{a.version}</span>}
      {a.maturity && a.maturity !== "stable" && a.maturity !== "beta" && <span className="badge badge-warn">{a.maturity}</span>}
      {!a.builtin && (
        <span className="badge" title={`Installed from ${a.package} ${a.package_version ?? ""}`}>
          external
        </span>
      )}
    </span>
  );
}


/** Sync a source now: toasts the result and refreshes what a sync changes. ``onDone`` does the
 * same for a sync started elsewhere (an upload). */
export function useSourceSync(onReport?: (r: SyncReportOut) => void) {
  const qc = useQueryClient();
  const toast = useToast();
  const onDone = (report: SyncReportOut) => {
    onReport?.(report);
    const c = report.counts;
    toast("ok", `Synced ${report.source}: ${c.created} new, ${c.updated} updated, ${c.deprecated} retired`);
    for (const key of ["sources", "items", "changes", "releases", "jobs"]) void qc.invalidateQueries({ queryKey: [key] });
  };
  const m = useMutation({
    mutationFn: (id: string) => endpoints.syncSource(id),
    onSuccess: onDone,
    onError: (e) => {
      toast("err", errMsg(e));
      void qc.invalidateQueries({ queryKey: ["sources"] });
    },
  });
  return { ...m, onDone };
}

export function Sources() {
  const q = useQuery({ queryKey: ["sources"], queryFn: endpoints.listSources });
  const me = useQuery({ queryKey: ["me"], queryFn: endpoints.me, retry: false });
  const kinds = useQuery({ queryKey: ["source-kinds"], queryFn: endpoints.listSourceKinds });
  const llm = useQuery({ queryKey: ["llm-connections"], queryFn: endpoints.listLlmConnections, retry: false });
  const admin = me.data?.role === "admin";
  const [form, setForm] = useState<{ editing: SourceOut | null } | null>(null);
  const sync = useSourceSync();

  const addButton = admin && kinds.data && (
    <button type="button" className="btn btn-primary" onClick={() => setForm({ editing: null })}>
      <Icon d={ICONS.plus} size={14} /> Add source
    </button>
  );

  const rows = q.data ?? [];
  return (
    <>
      <PageHeader
        title="Sources"
        sub="Tools whose metadata Satchel imports and keeps in sync, such as dbt. Their facts become ordinary context items. Analysts add notes in an overlay that a sync never overwrites."
        actions={rows.length > 0 ? addButton || undefined : undefined}
      />
      {q.isPending && <Loading />}
      {q.isError && <ErrorBox error={q.error} />}
      {q.data && rows.length === 0 && (
        <EmptyState title="No sources yet." muted="Add the dbt, Cube or SQLMesh project that describes your warehouse." icon={pageIcon("/sources")} action={addButton || undefined}>
          Its artifacts can come from S3 or GCS, or be uploaded when you sync (CI can push them on every build).{" "}
          {!admin && "An admin can add one. "}The CLI works too: <code className="font-mono text-xs">satchel source add</code>.
        </EmptyState>
      )}
      {rows.length > 0 && (
        <div className="card mb-9 overflow-hidden">
          <StatGrid>
            <StatCell label="Sources" tone="info" value={rows.length} info="Upstream tools Satchel imports metadata from." />
            <StatCell label="Synced" tone="good" value={rows.filter((r) => r.last_status === "ok").length} info="Sources whose last sync succeeded." />
            <StatCell label="Failed" tone="bad" glyph="triangle" value={rows.filter((r) => r.last_status === "error").length} info="Sources whose last sync failed." />
            <StatCell
              label="Items"
              tone="muted"
              glyph="square"
              value={rows.reduce((n, r) => n + Object.values(r.item_counts).reduce((a, b) => a + b, 0), 0).toLocaleString()}
              secondary={(() => { const w = rows.reduce((n, r) => n + problemsOf(r).length, 0); return `${w} warning${w === 1 ? "" : "s"}`; })()}
              info="Context items imported across all sources, and warnings from the last syncs (tables another source also describes are not counted)."
            />
          </StatGrid>
        </div>
      )}
      {rows.length > 0 && <SectionHeading icon={ICONS.link} title="Connected sources" count={rows.length} />}
      {rows.length > 0 && (
        <TableCard>
            <thead className="border-b border-line">
              <tr>
                <th className="th w-full">Source</th>
                <th className="th">Status</th>
                <th className="th hidden md:table-cell">Last sync</th>
                <th className="th hidden text-right lg:table-cell">Items</th>
                <th className="th hidden lg:table-cell">Publishes</th>
                <th className="th">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {rows.map((s) => (
                <SourceRow key={s.id} s={s} syncing={sync.isPending && sync.variables === s.id} onSync={() => sync.mutate(s.id)} />
              ))}
            </tbody>
        </TableCard>
      )}
      {form && kinds.data && (
        <SourceForm kinds={kinds.data} editing={form.editing} encryption={llm.data?.encryption_configured ?? true} onClose={() => setForm(null)} />
      )}
    </>
  );
}

/** One source: its name links to its page; the row says only whether it is healthy. */
function SourceRow(props: { s: SourceOut; syncing: boolean; onSync: () => void }) {
  const { s } = props;
  const warnings = problemsOf(s).length;
  const overlaps = overlapCountOf(s);
  const total = Object.values(s.item_counts).reduce((a, b) => a + b, 0);
  const to = `/sources/${encodeURIComponent(s.name)}`;
  return (
    <tr className="hover:bg-sunken/70">
      <td className="td">
        <Link to={to} className="block min-w-0">
          <span className="block break-words font-medium text-ink hover:text-accent">{s.name}</span>
          <span className="mt-1 block">
            <AdapterTags s={s} />
          </span>
          {(warnings > 0 || overlaps > 0) && (
            <span className="mt-1 block text-xs">
              {warnings > 0 && (
                <span className="text-warn">
                  {warnings} warning{warnings === 1 ? "" : "s"} in the last sync
                </span>
              )}
              {warnings > 0 && overlaps > 0 && <span className="text-muted"> · </span>}
              {overlaps > 0 && (
                <span className="text-muted">
                  {overlaps} overlap{overlaps === 1 ? "" : "s"} with other sources
                </span>
              )}
            </span>
          )}
        </Link>
      </td>
      <td className="td">
        <SyncStatus s={s} />
      </td>
      <td className="td hidden whitespace-nowrap text-muted md:table-cell" title={fullTime(s.last_synced_at)}>
        {timeAgo(s.last_synced_at)}
      </td>
      <td className="td hidden whitespace-nowrap text-right tabular-nums lg:table-cell">{total}</td>
      <td className="td hidden whitespace-nowrap lg:table-cell">
        {s.publish === "preview" && (
          <span className="badge badge-warn" title="Syncs create a preview release; publish it yourself">
            preview only
          </span>
        )}
      </td>
      <td className="td whitespace-nowrap text-right">
        <button className="btn btn-sm" disabled={props.syncing} onClick={props.onSync}>
          {props.syncing ? "Syncing..." : "Sync now"}
        </button>
      </td>
    </tr>
  );
}
