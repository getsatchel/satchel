import { useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";
import { endpoints } from "@/api/endpoints";
import type { SourceOut, SyncReportOut } from "@/api/types";
import { useToast } from "@/components/Toast";
import { Chevron, Crumb, DescList, ErrorBox, ICONS, Loading, Modal, Notice, PageHeader, SectionHeading, StatusDot } from "@/components/ui";
import { SourceForm, UploadSync } from "@/pages/SourceForm";
import { AdapterTags, problemsOf, SyncStatus, useSourceSync, warningsOf, type Warning } from "@/pages/Sources";
import { WebhookPanel } from "@/pages/SourceWebhook";
import { errMsg, fullTime, timeAgo, timeUntil } from "@/lib/format";

/**
 * One source on its own page, top to bottom by how often you need it: what needs attention, the
 * last sync, overlaps with other sources, setup, what starts a sync, and diagnostics (closed).
 */
export function SourceDetail() {
  const { name = "" } = useParams();
  const q = useQuery({ queryKey: ["sources"], queryFn: endpoints.listSources });
  const me = useQuery({ queryKey: ["me"], queryFn: endpoints.me, retry: false });
  if (q.isPending) return <Loading />;
  if (q.isError) return <ErrorBox error={q.error} />;
  const s = q.data.find((x) => x.name === name);
  if (!s) {
    return (
      <>
        <Crumb to="/sources" label="Sources" rest={name} />
        <Notice tone="warn">There is no source named {name} in this workspace. It may have been removed or renamed.</Notice>
      </>
    );
  }
  return <SourcePage s={s} admin={me.data?.role === "admin"} />;
}

function SourcePage({ s, admin }: { s: SourceOut; admin: boolean }) {
  const qc = useQueryClient();
  const toast = useToast();
  const nav = useNavigate();
  const kinds = useQuery({ queryKey: ["source-kinds"], queryFn: endpoints.listSourceKinds, enabled: admin });
  const llm = useQuery({ queryKey: ["llm-connections"], queryFn: endpoints.listLlmConnections, retry: false, enabled: admin });
  const [editing, setEditing] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [fresh, setFresh] = useState<SyncReportOut | null>(null);
  const sync = useSourceSync(setFresh);
  const remove = useMutation({
    mutationFn: () => endpoints.deleteSource(s.id),
    onSuccess: (r) => {
      toast("ok", `Removed ${s.name}; ${r.items_detached} item${r.items_detached === 1 ? "" : "s"} deprecated`);
      void qc.invalidateQueries({ queryKey: ["sources"] });
      void qc.invalidateQueries({ queryKey: ["items"] });
      void qc.invalidateQueries({ queryKey: ["changes"] });
      nav("/sources");
    },
    onError: (e) => toast("err", errMsg(e)),
  });

  // The sync just run here, else the one saved on the source (same shape once it has counts).
  const saved = s.last_report as Partial<SyncReportOut>;
  const report = fresh ?? (saved.counts ? (saved as SyncReportOut) : null);
  const problems = problemsOf(s);
  const overlaps = overlapsOf(s, fresh);
  // With no structured overlaps in the report, show its overlap warnings as warnings.
  const warnings = overlaps.length ? problems : warningsOf(s);
  const counts = countsOf(s);
  const rep = s.last_report as Automation;
  const attention = Boolean(s.last_error || report?.gate_message || (report && report.held.length > 0) || warnings.length);
  const hasFiles = (s.adapter?.file_inputs ?? []).length > 0;

  return (
    <>
      <Crumb to="/sources" label="Sources" rest={s.name} />
      <PageHeader
        title={s.name}
        sub={
          <span className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <SyncStatus s={s} />
            {s.last_synced_at && <span title={fullTime(s.last_synced_at)}>synced {timeAgo(s.last_synced_at)}</span>}
            <AdapterTags s={s} />
            {s.publish === "preview" && (
              <span className="badge badge-warn" title="Syncs create a preview release; publish it yourself">
                preview only
              </span>
            )}
          </span>
        }
        actions={
          <>
            {hasFiles && (
              <button type="button" className="btn" onClick={() => setUploading(true)}>
                Upload and sync
              </button>
            )}
            <button type="button" className="btn btn-primary" disabled={sync.isPending} onClick={() => sync.mutate(s.id)}>
              {sync.isPending ? "Syncing..." : "Sync now"}
            </button>
          </>
        }
      />

      {attention && (
        <section className="mb-9 space-y-3" aria-label="Needs attention">
          <SectionHeading icon={ICONS.alert} title="Needs attention" count={warnings.length || undefined} />
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
          {warnings.length > 0 && (
            <div className="card p-4 text-sm">
              <Warnings warnings={warnings} />
            </div>
          )}
        </section>
      )}

      <section className="mb-9" aria-label="Last sync">
        <SectionHeading icon={ICONS.pulse} title="Last sync" />
        <div className="card p-4 text-sm">
          <DescList
            labelWidth="7rem"
            rows={[
              [
                "When",
                s.last_synced_at ? (
                  <span title={fullTime(s.last_synced_at)}>
                    {timeAgo(s.last_synced_at)}
                    {rep.trigger && <span className="text-muted"> ({rep.trigger})</span>}
                  </span>
                ) : (
                  <span className="text-muted">never</span>
                ),
              ],
              ["Changed", report ? changedLine(report.counts) : null],
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
                s.sync_interval_hours ? (
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
        </div>
      </section>

      {overlaps.length > 0 && (
        <section className="mb-9" aria-label="Also described elsewhere">
          <SectionHeading icon={ICONS.link} title="Also described elsewhere" count={overlaps.length} />
          <div className="card p-4 text-sm">
            <Overlaps overlaps={overlaps} />
          </div>
        </section>
      )}

      <section className="mb-9" aria-label="Setup">
        <SectionHeading
          icon={ICONS.layers}
          title="Setup"
          actions={
            admin && (
              <>
                <button type="button" className="btn btn-sm" disabled={!kinds.data} onClick={() => setEditing(true)}>
                  Edit
                </button>
                <button type="button" className="btn btn-sm btn-ghost text-bad" onClick={() => setRemoving(true)}>
                  Remove
                </button>
              </>
            )
          }
        />
        <div className="card p-4 text-sm">
          <Setup s={s} />
          {!admin && <p className="mt-3 text-xs text-muted">Admins can edit or remove the source.</p>}
        </div>
      </section>

      <section className="mb-9" aria-label="Automation">
        <SectionHeading icon={ICONS.clock} title="Automation" />
        <div className="card space-y-4 p-4 text-sm">
          <AutomationRows s={s} admin={admin} />
        </div>
      </section>

      <Diagnostics s={s} />

      {editing && kinds.data && <SourceForm kinds={kinds.data} editing={s} encryption={llm.data?.encryption_configured ?? true} onClose={() => setEditing(false)} />}
      {uploading && (
        <UploadSync
          source={s}
          onClose={() => setUploading(false)}
          onDone={(r) => {
            setUploading(false);
            sync.onDone(r);
          }}
        />
      )}
      <Modal
        open={removing}
        onClose={() => setRemoving(false)}
        title={`Remove ${s.name}?`}
        footer={
          <>
            <button type="button" className="btn" onClick={() => setRemoving(false)}>
              Cancel
            </button>
            <button type="button" className="btn btn-danger" disabled={remove.isPending} onClick={() => remove.mutate()}>
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
function AutomationRows({ s, admin }: { s: SourceOut; admin: boolean }) {
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

/** The deepest layer: identifiers, versions and the full last sync report, for debugging. Closed by default. */
function Diagnostics({ s }: { s: SourceOut }) {
  const [open, setOpen] = useState(false);
  const toast = useToast();
  const raw = JSON.stringify(s.last_report ?? {}, null, 2);
  const last = s.last_report as { git_commit?: string | null; dbt_version?: string | null };
  return (
    <section aria-label="Diagnostics" className="border-t border-line pt-4">
      <button type="button" className="flex items-center gap-1.5 text-xs text-muted hover:text-ink" aria-expanded={open} onClick={() => setOpen(!open)}>
        <Chevron open={open} />
        Diagnostics: IDs, commit and the raw sync report
      </button>
      {open && (
        <div className="mt-3 space-y-4">
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
        </div>
      )}
    </section>
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

/** Tables and models another source also describes, and which one agents are served. */
function Overlaps({ overlaps }: { overlaps: Overlap[] }) {
  return (
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
  );
}

/** Warnings from the last sync, grouped by kind. */
function Warnings({ warnings }: { warnings: Warning[] }) {
  return (
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
  );
}
