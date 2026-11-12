import { useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { endpoints } from "@/api/endpoints";
import type { SourceOut, SyncReportOut } from "@/api/types";
import { useToast } from "@/components/Toast";
import { Chevron, DescList, EmptyState, ErrorBox, ICONS, Icon, Loading, Modal, Notice, PageHeader, SectionHeading, StatCell, StatGrid, StatusDot, TableCard, Tabs } from "@/components/ui";
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

/** Overlap warnings (another source describes the same table or model) are shown as overlaps,
 * not as problems with the sync. */
const OVERLAP_CODES = new Set(["also_described_by", "semantic_shadowed"]);
const problemsOf = (s: SourceOut) => warningsOf(s).filter((w) => !OVERLAP_CODES.has(w.code));
const overlapCountOf = (s: SourceOut) => warningsOf(s).length - problemsOf(s).length;

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

/** The row's adapter line: kind and version. Flags appear only when they say something: an
 * adapter from a package, or one still experimental. The rest is in the detail. */
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

/** Adapter facts for the detail: version, maturity and where it comes from. */
function adapterLine(s: SourceOut): ReactNode {
  const a = s.adapter;
  if (!a) return <span className="text-bad">not installed</span>;
  const from = a.builtin ? "built into Satchel" : `${a.package}${a.package_version ? ` ${a.package_version}` : ""}`;
  return (
    <span>
      <span className="font-mono">
        {a.kind}
        {a.version ? ` v${a.version}` : ""}
      </span>
      <span className="text-muted">
        {a.maturity ? ` · ${a.maturity}` : ""} · {from}
      </span>
    </span>
  );
}

type Origin = "git" | "bucket" | "server" | "upload" | "built";

const ORIGIN: Record<Origin, string> = {
  git: "repository",
  bucket: "bucket",
  server: "server path",
  upload: "upload",
  built: "dbt parse",
};

/** Where each of the adapter's file inputs comes from on a sync. A path on the server can only be
 * set with the CLI (the UI accepts git paths, s3:// and gs://), so it is called out. */
function filesOf(s: SourceOut): { name: string; value: string | null; origin: Origin }[] {
  const cfg = s.config as Record<string, unknown>;
  const git = Boolean(cfg.git_url);
  const building = git && s.kind === "dbt" && Boolean(cfg.dbt_project_dir);
  return (s.adapter?.file_inputs ?? []).map((name) => {
    const raw = cfg[name];
    const value = typeof raw === "string" && raw ? raw : null;
    if (building && (name === "manifest" || name === "semantic_manifest")) return { name, value: null, origin: "built" };
    if (!value) return { name, value: null, origin: "upload" };
    if (git) return { name, value, origin: "git" };
    if (/^(s3|gs):\/\//.test(value)) return { name, value, origin: "bucket" };
    return { name, value, origin: "server" };
  });
}

/** A path that wraps at its slashes, with the folders dimmed. */
function PathText({ path }: { path: string }) {
  const i = path.lastIndexOf("/");
  const dir = i >= 0 ? path.slice(0, i + 1) : "";
  const base = path.slice(i + 1);
  return (
    <span className="block font-mono text-xs [overflow-wrap:anywhere]">
      <span className="text-muted">
        {dir.split("/").map((part, j, all) => (
          <span key={j}>
            {part}
            {j < all.length - 1 && (
              <>
                /<wbr />
              </>
            )}
          </span>
        ))}
      </span>
      {base}
    </span>
  );
}

function SourceFiles({ s }: { s: SourceOut }) {
  const files = filesOf(s);
  if (files.length === 0) return null;
  const required = new Set(s.adapter?.required_inputs ?? []);
  return (
    <ul className="space-y-1">
      {files.map((f) => (
        <li key={f.name} className="min-w-0">
          <span className="text-muted">{f.name.replace(/_/g, " ")}</span>{" "}
          <span className="badge" title={f.origin === "server" ? "A path on the Satchel server, set with the CLI. Change it with satchel source add." : undefined}>
            {ORIGIN[f.origin]}
          </span>
          {f.value && <PathText path={f.value} />}
          {!f.value && f.origin === "upload" && (
            <span className="block text-xs text-muted">{required.has(f.name) ? "uploaded on each sync (required)" : "not set"}</span>
          )}
        </li>
      ))}
    </ul>
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
                <SourceRow
                  key={s.id}
                  s={s}
                  warnings={problemsOf(s)}
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
  const overlaps = overlapCountOf(s);
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
              {(warnings.length > 0 || overlaps > 0) && (
                <span className="mt-1 block text-xs">
                  {warnings.length > 0 && (
                    <span className="text-warn">
                      {warnings.length} warning{warnings.length === 1 ? "" : "s"} in the last sync
                    </span>
                  )}
                  {warnings.length > 0 && overlaps > 0 && <span className="text-muted"> · </span>}
                  {overlaps > 0 && (
                    <span className="text-muted">
                      {overlaps} overlap{overlaps === 1 ? "" : "s"} with other sources
                    </span>
                  )}
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
            {/* w-0 min-w-full: fill the row without letting the tabs widen the table on a phone. */}
            <div className="w-0 min-w-full">
              <SourceDetail s={s} warnings={warnings} report={props.report} admin={props.admin} onEdit={props.onEdit} onUpload={props.onUpload} onRemove={props.onRemove} />
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

type DetailTab = "overview" | "issues" | "setup" | "automation" | "diagnostics";

/**
 * A source's detail, in tabs so each layer has its own place: Overview (what happened and what
 * needs you), Issues (warnings and overlaps, only when there are some), Setup, Automation
 * (schedule, push webhook, CI) and Diagnostics (identifiers and the raw report).
 */
function SourceDetail(props: {
  s: SourceOut;
  warnings: Warning[];
  report: SyncReportOut | null;
  admin: boolean;
  onEdit: () => void;
  onUpload: () => void;
  onRemove: () => void;
}) {
  const { s, report } = props;
  const overlaps = overlapsOf(s, report);
  // Overlap warnings are already listed under "Also described elsewhere".
  const warnings = overlaps.length ? props.warnings : warningsOf(s);
  const issues = warnings.length + overlaps.length;
  const hasFiles = (s.adapter?.file_inputs ?? []).length > 0;
  const [tab, setTab] = useState<DetailTab>("overview");
  const tabs: { id: DetailTab; label: string }[] = [
    { id: "overview", label: "Overview" },
    ...(issues > 0 ? [{ id: "issues" as const, label: `Issues (${issues})` }] : []),
    { id: "setup", label: "Setup" },
    { id: "automation", label: "Automation" },
    { id: "diagnostics", label: "Diagnostics" },
  ];
  const current = tabs.some((t) => t.id === tab) ? tab : "overview";
  return (
    <div className="text-sm">
      <div className="flex flex-wrap items-end justify-between gap-x-4">
        <div className="min-w-0 basis-full md:basis-0 md:flex-1">
          <Tabs tabs={tabs} value={current} onChange={setTab} />
        </div>
        <div className="mb-3 flex flex-wrap gap-2">
          {hasFiles && (
            <button type="button" className="btn btn-sm" onClick={props.onUpload}>
              Upload and sync
            </button>
          )}
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
      <div role="tabpanel" aria-label={tabs.find((t) => t.id === current)?.label} className="min-w-0 space-y-5 pt-1">
        {current === "overview" && <Overview s={s} report={report} admin={props.admin} />}
        {current === "issues" && <Issues overlaps={overlaps} warnings={warnings} />}
        {current === "setup" && <Setup s={s} />}
        {current === "automation" && <AutomationTab s={s} admin={props.admin} />}
        {current === "diagnostics" && <Diagnostics s={s} />}
      </div>
    </div>
  );
}

/** What happened in the last sync and anything that needs a person. */
function Overview(props: { s: SourceOut; report: SyncReportOut | null; admin: boolean }) {
  const { s } = props;
  // The sync just run here, else the one saved on the source (same shape once it has counts).
  const saved = s.last_report as Partial<SyncReportOut>;
  const report = props.report ?? (saved.counts ? (saved as SyncReportOut) : null);
  const counts = countsOf(s);
  const rep = s.last_report as Automation;
  const n = s.sync_interval_hours;
  return (
    <>
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
      <DescList
        labelWidth="7rem"
        rows={[
          [
            "Last sync",
            s.last_synced_at ? (
              <span title={fullTime(s.last_synced_at)}>
                {timeAgo(s.last_synced_at)}
                {rep.trigger && <span className="text-muted"> ({rep.trigger})</span>}
              </span>
            ) : (
              <span className="text-muted">never</span>
            ),
          ],
          [
            "Changed",
            report ? changedLine(report.counts) : null,
          ],
          ["Release", report && "release_kind" in report ? (report.release_kind ? `${report.release_kind} #${report.release_seq}${report.release_created ? " (new)" : " (unchanged)"}` : "no change") : null],
          [
            "Items",
            counts.length ? (
              <span>
                {counts.map((c) => `${c.n} ${c.label}`).join(", ")}{" "}
                <Link className="text-accent hover:underline" to="/library?managed_by=source">
                  in the Library
                </Link>
              </span>
            ) : (
              <span className="text-muted">none yet</span>
            ),
          ],
          [
            "Next sync",
            n ? (
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
              ) : null
            ) : (
              <span className="text-muted">on request (no schedule)</span>
            ),
          ],
        ]}
      />
      {!props.admin && <p className="text-xs text-muted">Admins can edit or remove the source.</p>}
    </>
  );
}

/** "2 created, 4 unchanged": only the counts that are not zero. */
function changedLine(c: SyncReportOut["counts"]): string {
  const parts = [
    [c.created, "created"],
    [c.updated, "updated"],
    [c.deprecated, "retired"],
    [c.unchanged, "unchanged"],
  ] as const;
  const shown = parts.filter(([n]) => (n ?? 0) > 0).map(([n, w]) => `${n} ${w}`);
  return shown.length ? shown.join(", ") : "nothing";
}

/** Overlaps with other sources, then warnings grouped by kind. */
function Issues({ overlaps, warnings }: { overlaps: Overlap[]; warnings: Warning[] }) {
  return (
    <>
      {overlaps.length > 0 && (
        <section aria-label="Also described elsewhere">
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
        </section>
      )}
      {warnings.length > 0 && (
        <section aria-label="Warnings from the last sync">
          <h3 className="label-caps mb-2">Warnings from the last sync ({warnings.length})</h3>
          <ul className="space-y-3">
            {groupWarnings(warnings).map((g) => (
              <li key={g.code} className="min-w-0">
                <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                  <span className="badge badge-warn">{g.code.replace(/_/g, " ")}</span>
                  {g.shared && <span className="min-w-0 break-words">{g.shared}</span>}
                </div>
                {g.shared ? (
                  <ul className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 font-mono text-xs text-muted" aria-label="Affected">
                    {g.subjects.map((x, i) => (
                      <li key={i} className="min-w-0 [overflow-wrap:anywhere]">
                        {x}
                      </li>
                    ))}
                  </ul>
                ) : (
                  <ul className="mt-1 space-y-0.5">
                    {g.items.map((w, i) => (
                      <li key={i} className="min-w-0 break-words">
                        {w.message}
                      </li>
                    ))}
                  </ul>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}
    </>
  );
}

/** Where the source reads from and how its syncs publish. */
function Setup({ s }: { s: SourceOut }) {
  const git = Boolean(s.config.git_url);
  const hasFiles = (s.adapter?.file_inputs ?? []).length > 0;
  return (
    <DescList
      labelWidth="7rem"
      rows={[
        ["Adapter", adapterLine(s)],
        ["Connection", s.connection ?? <span className="text-muted">none (tables not linked to a warehouse)</span>],
        [
          "Repository",
          git ? (
            <span className="font-mono text-xs [overflow-wrap:anywhere]">
              {String(s.config.git_url)}
              {s.config.git_ref ? <span className="text-muted"> @ {String(s.config.git_ref)}</span> : null}
            </span>
          ) : null,
        ],
        ["Files", hasFiles ? <SourceFiles s={s} /> : null],
        ["Priority", <span className="tabular-nums">{s.priority}</span>],
        ["Publishes", s.publish === "preview" ? "a preview release you publish yourself" : "a release on every sync"],
      ]}
    />
  );
}

/** What starts a sync: the schedule, a push webhook, or CI uploading artifacts. */
function AutomationTab({ s, admin }: { s: SourceOut; admin: boolean }) {
  const git = Boolean(s.config.git_url);
  const ci = !git && singleFileInputs(s).length > 0;
  return (
    <>
      {/* Admins see the webhook's state in its panel below. */}
      <DescList labelWidth="7rem" rows={automationRows(s).filter(([k]) => !(admin && k === "Push webhook"))} />
      {admin ? <WebhookPanel s={s} /> : !s.webhook_url && <p className="text-xs text-muted">Admins can set up a push webhook.</p>}
      {ci && <CiPush s={s} />}
    </>
  );
}

/** Warnings of one kind. When every message is "<subject>: <the same explanation>", the
 * explanation is shown once with the list of subjects, instead of one long line each. */
function groupWarnings(ws: Warning[]): { code: string; shared: string | null; subjects: string[]; items: Warning[] }[] {
  const by = new Map<string, Warning[]>();
  for (const w of ws) by.set(w.code, [...(by.get(w.code) ?? []), w]);
  return [...by.entries()].map(([code, items]) => {
    const parts = items.map((w) => {
      const i = w.message.indexOf(": ");
      return i > 0 ? { subject: w.key ?? w.message.slice(0, i).replace(/^(model|table|cube|view|metric) /, ""), rest: w.message.slice(i + 2) } : null;
    });
    const same = items.length > 1 && parts.every((x) => x && x.rest === parts[0]?.rest);
    const first = parts[0]?.rest ?? "";
    return {
      code,
      shared: same ? first.charAt(0).toUpperCase() + first.slice(1) : null,
      subjects: same ? parts.map((x) => x?.subject ?? "") : [],
      items,
    };
  });
}

/** The deepest layer: identifiers, versions and the full last sync report, for debugging. */
function Diagnostics({ s }: { s: SourceOut }) {
  const toast = useToast();
  const raw = JSON.stringify(s.last_report ?? {}, null, 2);
  const last = s.last_report as { git_commit?: string | null; dbt_version?: string | null };
  return (
    <>
      <DescList
        labelWidth="7rem"
        rows={[
          ["Source ID", <code className="text-xs [overflow-wrap:anywhere]">{s.id}</code>],
          ["Commit", last.git_commit ? <code className="text-xs [overflow-wrap:anywhere]">{String(last.git_commit)}</code> : null],
          ["Built with", last.dbt_version ? `dbt Core ${String(last.dbt_version)} (dbt parse on the server)` : null],
          ["Fingerprint", s.last_fingerprint ? <code className="text-xs [overflow-wrap:anywhere]">{s.last_fingerprint}</code> : null],
          ["Last sync", s.last_synced_at ? fullTime(s.last_synced_at) : null],
        ]}
      />
      <section aria-label="Last sync report">
        <div className="mb-1.5 flex items-center justify-between gap-2">
          <h3 className="label-caps">Last sync report</h3>
          <button
            type="button"
            className="btn btn-sm"
            onClick={() =>
              void navigator.clipboard.writeText(raw).then(
                () => toast("ok", "Report copied"),
                () => toast("err", "Copy failed: select the text and copy it"),
              )
            }
          >
            Copy
          </button>
        </div>
        <pre className="max-h-80 overflow-auto whitespace-pre-wrap rounded-md border border-line bg-sunken px-3 py-2 font-mono text-xs leading-relaxed [overflow-wrap:anywhere]">{raw}</pre>
      </section>
    </>
  );
}

const singleFileInputs = (s: SourceOut) => (s.adapter?.file_inputs ?? []).filter((n) => !(s.adapter?.folder_inputs ?? []).includes(n));

/** How CI pushes fresh artifacts to this source after a build. */
function CiPush({ s }: { s: SourceOut }) {
  const toast = useToast();
  const fields = singleFileInputs(s);
  const cmd = `curl -fsS -X POST -H "Authorization: Bearer $SATCHEL_TOKEN" \\\n${fields.map((n) => `  -F ${n}=@target/${n}.json \\\n`).join("")}  ${window.location.origin}/v1/sources/${s.id}/sync`;
  return (
    <section aria-label="Push from CI" className="space-y-2 border-t border-line pt-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="label-caps">Push from CI</h3>
        <button
          type="button"
          className="btn btn-sm"
          onClick={() =>
            void navigator.clipboard.writeText(cmd).then(
              () => toast("ok", "Command copied"),
              () => toast("err", "Copy failed: select the text and copy it"),
            )
          }
        >
          Copy
        </button>
      </div>
      <p className="max-w-3xl text-xs text-muted">After each build, CI can upload the artifacts and sync in one call. Use a token of a curator or above; files you leave out use the location saved on the source.</p>
      <pre className="whitespace-pre-wrap rounded-md border border-line bg-sunken px-3 py-2 font-mono text-xs leading-relaxed [overflow-wrap:anywhere]">{cmd}</pre>
    </section>
  );
}
