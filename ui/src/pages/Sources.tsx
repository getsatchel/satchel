import { useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { endpoints } from "@/api/endpoints";
import type { SourceOut, SyncReportOut } from "@/api/types";
import { useToast } from "@/components/Toast";
import { CardGrid, Chevron, DescList, EmptyState, ErrorBox, ICONS, Icon, Loading, Modal, Notice, PageHeader, SectionHeading, StatCell, StatGrid, StatusDot, TableCard } from "@/components/ui";
import { SourceForm, UploadSync } from "@/pages/SourceForm";
import { WebhookPanel } from "@/pages/SourceWebhook";
import { errMsg, fullTime, timeAgo, timeUntil } from "@/lib/format";

interface Warning {
  code: string;
  message: string;
  key?: string | null;
}

/** Warnings stored in the last sync report (the report is free-form JSON from the server). */
function warningsOf(s: SourceOut): Warning[] {
  const w = (s.last_report as { warnings?: unknown }).warnings;
  return Array.isArray(w) ? (w as Warning[]) : [];
}

type Overlap =
  | { type: "asset_doc"; asset: string; primary: { source: string; key: string }; also_described_by: { source: string; key: string }[]; this_source_is_primary: boolean }
  | { type: "semantic_model"; key: string; shadowed: boolean; reason: string };

/** Overlaps from the latest sync response, else the stored last report (both free-form JSON). */
function overlapsOf(s: SourceOut, report: SyncReportOut | null): Overlap[] {
  const o = report?.overlaps ?? (s.last_report as { overlaps?: unknown }).overlaps;
  return Array.isArray(o) ? (o as Overlap[]) : [];
}

function countsOf(s: SourceOut): { label: string; n: number }[] {
  return Object.entries(s.item_counts)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([label, n]) => ({ label: label.replace("_", " "), n }));
}

interface Automation {
  trigger?: string;
  checked?: { at: string; trigger: string; commit: string };
  failures?: number;
}

/** Schedule and what started the last sync (trigger and checked live in the free-form report). */
function automationRows(s: SourceOut): [string, ReactNode][] {
  const rep = s.last_report as Automation;
  const n = s.sync_interval_hours;
  return [
    ["Schedule", n ? `every ${n === 1 ? "hour" : `${n} hours`}` : <span className="text-muted">off (sync on request)</span>],
    [
      "Next sync",
      s.next_sync_at ? (
        <span title={fullTime(s.next_sync_at)}>
          {timeUntil(s.next_sync_at)}
          {s.last_status === "error" && rep.failures ? (
            <span className="text-muted">
              {" "}
              (retry after {rep.failures} failed {rep.failures === 1 ? "sync" : "syncs in a row"})
            </span>
          ) : null}
        </span>
      ) : null,
    ],
    ["Push webhook", s.webhook_url ? "set up" : null],
    ["Last trigger", rep.trigger ?? null],
    [
      "Last check",
      rep.checked ? (
        <span title={fullTime(rep.checked.at)}>
          {timeAgo(rep.checked.at)} ({rep.checked.trigger}): no new commit since <code className="text-xs">{rep.checked.commit.slice(0, 10)}</code>
        </span>
      ) : null,
    ],
  ];
}

function SyncStatus({ s }: { s: SourceOut }) {
  if (s.last_status === "ok") return <StatusDot tone="good">synced</StatusDot>;
  if (s.last_status === "error") return <StatusDot tone="bad">failed</StatusDot>;
  return <StatusDot tone="muted">never synced</StatusDot>;
}

/** Adapter facts: kind, version, maturity, and whether it ships with satchel or comes from a package. */
function AdapterTags({ s }: { s: SourceOut }) {
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
      {a.version && <span className="mono text-xs text-muted">v{a.version}</span>}
      {a.maturity && <span className={`badge ${a.maturity === "stable" ? "badge-good" : a.maturity === "beta" ? "" : "badge-warn"}`}>{a.maturity}</span>}
      <span className="badge" title={a.builtin ? "Ships with Satchel" : `Installed from ${a.package} ${a.package_version ?? ""}`}>
        {a.builtin ? "built-in" : "external"}
      </span>
    </span>
  );
}

export function Sources() {
  const q = useQuery({ queryKey: ["sources"], queryFn: endpoints.listSources });
  const me = useQuery({ queryKey: ["me"], queryFn: endpoints.me, retry: false });
  const kinds = useQuery({ queryKey: ["source-kinds"], queryFn: endpoints.listSourceKinds });
  const llm = useQuery({ queryKey: ["llm-connections"], queryFn: endpoints.listLlmConnections, retry: false });
  const admin = me.data?.role === "admin";
  const [form, setForm] = useState<{ editing: SourceOut | null } | null>(null);
  const [uploading, setUploading] = useState<SourceOut | null>(null);
  const [removing, setRemoving] = useState<SourceOut | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [last, setLast] = useState<{ id: string; report: SyncReportOut } | null>(null);
  const qc = useQueryClient();
  const toast = useToast();

  const sync = useMutation({
    mutationFn: (id: string) => endpoints.syncSource(id),
    onSuccess: (report, id) => {
      setLast({ id, report });
      setOpen(id);
      const c = report.counts;
      toast("ok", `Synced ${report.source}: ${c.created} new, ${c.updated} updated, ${c.deprecated} retired`);
      void qc.invalidateQueries({ queryKey: ["sources"] });
      void qc.invalidateQueries({ queryKey: ["items"] });
      void qc.invalidateQueries({ queryKey: ["changes"] });
      void qc.invalidateQueries({ queryKey: ["releases"] });
    },
    onError: (e) => {
      toast("err", errMsg(e));
      void qc.invalidateQueries({ queryKey: ["sources"] });
    },
  });

  const remove = useMutation({
    mutationFn: (src: SourceOut) => endpoints.deleteSource(src.id),
    onSuccess: (r, src) => {
      toast("ok", `Removed ${src.name}; ${r.items_detached} item${r.items_detached === 1 ? "" : "s"} deprecated`);
      setRemoving(null);
      setOpen(null);
      void qc.invalidateQueries({ queryKey: ["sources"] });
      void qc.invalidateQueries({ queryKey: ["items"] });
      void qc.invalidateQueries({ queryKey: ["changes"] });
    },
    onError: (e) => toast("err", errMsg(e)),
  });

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
        <EmptyState title="No sources yet" action={addButton || undefined}>
          Add a dbt project, Cube model or SQLMesh project. Its artifacts can come from S3 or GCS, or be uploaded when you sync (CI can push them on every build).{" "}
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
              secondary={(() => { const w = rows.reduce((n, r) => n + warningsOf(r).length, 0); return `${w} warning${w === 1 ? "" : "s"}`; })()}
              info="Context items imported across all sources, and warnings from the last syncs."
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
                <SourceRow
                  key={s.id}
                  s={s}
                  warnings={warningsOf(s)}
                  isOpen={open === s.id}
                  total={Object.values(s.item_counts).reduce((a, b) => a + b, 0)}
                  syncing={sync.isPending && sync.variables === s.id}
                  onToggle={() => setOpen(open === s.id ? null : s.id)}
                  onSync={() => sync.mutate(s.id)}
                  report={last?.id === s.id ? last.report : null}
                  admin={admin}
                  onEdit={() => setForm({ editing: s })}
                  onUpload={() => setUploading(s)}
                  onRemove={() => setRemoving(s)}
                />
              ))}
            </tbody>
        </TableCard>
      )}
      {rows.length > 0 && (
        <p className="mt-3 max-w-3xl text-xs text-muted">
          CI can push fresh artifacts on every build:{" "}
          <code className="font-mono">
            dbt build && dbt docs generate && curl -X POST -H "Authorization: Bearer $SATCHEL_TOKEN" -F manifest=@target/manifest.json -F catalog=@target/catalog.json $SATCHEL_URL/v1/sources/&lt;id&gt;/sync
          </code>
        </p>
      )}
      {form && kinds.data && (
        <SourceForm kinds={kinds.data} editing={form.editing} encryption={llm.data?.encryption_configured ?? true} onClose={() => setForm(null)} />
      )}
      {uploading && (
        <UploadSync
          source={uploading}
          onClose={() => setUploading(null)}
          onDone={(report) => {
            const id = uploading.id;
            setUploading(null);
            setLast({ id, report });
            setOpen(id);
            const c = report.counts;
            toast("ok", `Synced ${report.source}: ${c.created} new, ${c.updated} updated, ${c.deprecated} retired`);
            void qc.invalidateQueries({ queryKey: ["sources"] });
            void qc.invalidateQueries({ queryKey: ["items"] });
            void qc.invalidateQueries({ queryKey: ["changes"] });
            void qc.invalidateQueries({ queryKey: ["releases"] });
          }}
        />
      )}
      <Modal
        open={removing !== null}
        onClose={() => setRemoving(null)}
        title={`Remove ${removing?.name ?? ""}?`}
        footer={
          <>
            <button type="button" className="btn" onClick={() => setRemoving(null)}>
              Cancel
            </button>
            <button type="button" className="btn btn-danger" disabled={remove.isPending} onClick={() => removing && remove.mutate(removing)}>
              {remove.isPending ? "Removing..." : "Remove"}
            </button>
          </>
        }
      >
        <p className="text-sm">
          Its items are deprecated and leave the next release; analysts' overlay notes and the history are kept, nothing is deleted. Add the source again to bring them back.
        </p>
      </Modal>
    </>
  );
}

function SourceRow(props: {
  s: SourceOut;
  warnings: Warning[];
  isOpen: boolean;
  total: number;
  syncing: boolean;
  report: SyncReportOut | null;
  onToggle: () => void;
  onSync: () => void;
  admin: boolean;
  onEdit: () => void;
  onUpload: () => void;
  onRemove: () => void;
}) {
  const { s, warnings } = props;
  return (
    <>
      <tr className={props.isOpen ? "bg-accent-soft/70 shadow-[inset_2px_0_0_rgb(var(--accent))]" : "hover:bg-sunken/70"}>
        <td className="td">
          <button className="flex w-full items-start gap-2 text-left" aria-expanded={props.isOpen} onClick={props.onToggle}>
            <span className="mt-1.5">
              <Chevron open={props.isOpen} />
            </span>
            <span className="min-w-0">
              <span className="block break-words font-medium">{s.name}</span>
              <span className="mt-1 block">
                <AdapterTags s={s} />
              </span>
              {warnings.length > 0 && (
                <span className="mt-1 block text-xs text-warn">
                  {warnings.length} warning{warnings.length === 1 ? "" : "s"} in the last sync
                </span>
              )}
            </span>
          </button>
        </td>
        <td className="td">
          <SyncStatus s={s} />
        </td>
        <td className="td hidden whitespace-nowrap text-muted md:table-cell" title={fullTime(s.last_synced_at)}>
          {timeAgo(s.last_synced_at)}
        </td>
        <td className="td hidden whitespace-nowrap text-right tabular-nums lg:table-cell">{props.total}</td>
        <td className="td hidden whitespace-nowrap lg:table-cell">{s.publish === "preview" && <span className="badge badge-warn" title="Syncs create a preview release; publish it yourself">preview only</span>}</td>
        <td className="td whitespace-nowrap text-right">
          <button className="btn btn-sm" disabled={props.syncing} onClick={props.onSync}>
            {props.syncing ? "Syncing..." : "Sync now"}
          </button>
        </td>
      </tr>
      {props.isOpen && (
        <tr>
          <td colSpan={6} className="border-t border-line bg-sunken/20 px-4 py-3">
            <SourceDetail s={s} warnings={warnings} report={props.report} admin={props.admin} onEdit={props.onEdit} onUpload={props.onUpload} onRemove={props.onRemove} />
          </td>
        </tr>
      )}
    </>
  );
}

function SourceDetail(props: {
  s: SourceOut;
  warnings: Warning[];
  report: SyncReportOut | null;
  admin: boolean;
  onEdit: () => void;
  onUpload: () => void;
  onRemove: () => void;
}) {
  const { s, warnings, report } = props;
  const counts = countsOf(s);
  const overlaps = overlapsOf(s, report);
  return (
    <div className="space-y-3 text-sm">
      {s.last_error && <ErrorBox error={s.last_error} />}
      {report?.gate_message && <Notice tone="warn">Not published: {report.gate_message}.</Notice>}
      {report && report.held.length > 0 && (
        <Notice tone="warn">
          {report.held.length} item{report.held.length === 1 ? " has" : "s have"} an unpublished overlay draft, so the sync kept the published version. Publish the draft from{" "}
          <Link className="underline" to="/changes">
            Changes
          </Link>
          .
        </Notice>
      )}
      <CardGrid min="22rem" gap="gap-4">
        <DescList
          labelWidth="8rem"
          rows={[
            ["Connection", s.connection ?? <span className="text-muted">none (assets not linked to a warehouse)</span>],
            [
              "Repository",
              s.config.git_url ? (
                <span className="font-mono text-xs [overflow-wrap:anywhere]">
                  {String(s.config.git_url)}
                  {s.config.git_ref ? <span className="text-muted"> @ {String(s.config.git_ref)}</span> : null}
                </span>
              ) : null,
            ],
            [
              "Commit",
              (s.last_report as { git_commit?: string | null }).git_commit ? (
                <code className="text-xs">{String((s.last_report as { git_commit?: string }).git_commit).slice(0, 10)}</code>
              ) : null,
            ],
            [
              "Built with",
              (s.last_report as { dbt_version?: string | null }).dbt_version ? (
                <span>dbt Core {String((s.last_report as { dbt_version?: string }).dbt_version)} (dbt parse on the server)</span>
              ) : null,
            ],
            ["Priority", <span className="tabular-nums">{s.priority}</span>],
            ["Items", counts.length ? counts.map((c) => `${c.n} ${c.label}`).join(", ") : <span className="text-muted">none yet</span>],
            ["Last sync", s.last_synced_at ? fullTime(s.last_synced_at) : null],
            ...automationRows(s),
            ["Fingerprint", s.last_fingerprint ? <code className="break-all text-xs">{s.last_fingerprint.slice(0, 16)}</code> : null],
          ]}
        />
        {report && (
          <DescList
            labelWidth="8rem"
            rows={[
              ["This sync", `${report.counts.created} created, ${report.counts.updated} updated, ${report.counts.unchanged} unchanged, ${report.counts.deprecated} retired`],
              ["Release", report.release_kind ? `${report.release_kind} #${report.release_seq}${report.release_created ? " (new)" : " (unchanged)"}` : "no change"],
              [
                "Curation signals",
                Object.keys(report.signals).length
                  ? Object.entries(report.signals)
                      .map(([k, v]) => `${k.replace(/_/g, " ")} ${v}`)
                      .join(", ")
                  : null,
              ],
            ]}
          />
        )}
      </CardGrid>
      {overlaps.length > 0 && (
        <div>
          <h3 className="label-caps mb-2">Also described elsewhere ({overlaps.length})</h3>
          <ul className="space-y-1.5">
            {overlaps.map((o, i) =>
              o.type === "semantic_model" ? (
                <li key={i} className="min-w-0">
                  <span className="font-mono [overflow-wrap:anywhere]">{o.key}</span>{" "}
                  {o.shadowed && <span className="badge badge-warn">shadowed</span>}{" "}
                  <span className="text-muted">{o.reason}</span>
                </li>
              ) : (
                <li key={i} className="min-w-0">
                  <span className="font-mono [overflow-wrap:anywhere]">{o.asset}</span>{" "}
                  {o.this_source_is_primary ? <StatusDot tone="good">this source is primary</StatusDot> : <span className="badge badge-warn">not served</span>}
                  <span className="block text-xs text-muted">
                    {o.this_source_is_primary ? "Also described by" : `${o.primary.source} is primary; also`}{" "}
                    {o.also_described_by.map((r, j) => (
                      <span key={r.key}>
                        {j > 0 && ", "}
                        <Link className="text-accent hover:underline" to={`/library/item/${r.key.split("/").map(encodeURIComponent).join("/")}`}>
                          {r.source}
                        </Link>
                      </span>
                    ))}
                  </span>
                </li>
              ),
            )}
          </ul>
        </div>
      )}
      {warnings.length > 0 && (
        <div>
          <h3 className="label-caps mb-2">Warnings from the last sync</h3>
          <ul className="space-y-1">
            {warnings.map((w, i) => (
              <li key={i} className="flex flex-wrap gap-x-2">
                <span className="badge badge-warn">{w.code.replace(/_/g, " ")}</span>
                <span className="min-w-0 break-words">{w.message}</span>
                {w.key && <span className="font-mono text-xs text-muted">{w.key}</span>}
              </li>
            ))}
          </ul>
        </div>
      )}
      {props.admin && <WebhookPanel s={s} />}
      <p className="text-xs text-muted">
        Browse its items in the{" "}
        <Link className="text-accent hover:underline" to="/library?managed_by=source">
          Library
        </Link>
        .{!props.admin && " Admins can edit or remove the source here."}
      </p>
      <div className="flex flex-wrap gap-2">
        <button type="button" className="btn btn-sm" onClick={props.onUpload}>
          Upload and sync
        </button>
        {props.admin && (
          <>
            <button type="button" className="btn btn-sm" onClick={props.onEdit}>
              Edit
            </button>
            <button type="button" className="btn btn-sm btn-ghost text-bad" onClick={props.onRemove}>
              Remove
            </button>
          </>
        )}
      </div>
    </div>
  );
}
