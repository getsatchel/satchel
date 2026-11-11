import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { endpoints } from "@/api/endpoints";
import type { GitTestOut, SourceKindOut, SourceOut, SyncReportOut } from "@/api/types";
import { useToast } from "@/components/Toast";
import { ErrorBox, InfoTip, Modal, Notice, Segmented, StatusDot } from "@/components/ui";
import { errMsg } from "@/lib/format";

const HOST_KEYS = new Set(["publish", "priority", "connection", "git_url", "git_ref", "git_username", "git_token", "sync_interval_hours", "webhook_secret", "dbt_project_dir", "dbt_skip_deps", "dbt_vars", "dbt_target_schema", "dbt_target_name"]);
/** "Sync automatically" choices, in hours (the server accepts 1-168). */
const INTERVALS: [number, string][] = [
  [1, "Every hour"],
  [3, "Every 3 hours"],
  [6, "Every 6 hours"],
  [12, "Every 12 hours"],
  [24, "Every day"],
  [168, "Every week"],
];
/** dbt from git: manifest.json built on the server (dbt parse) or a committed one. */
type DbtMode = "build" | "manifest";
/** Inputs dbt parse writes, so they are not asked for in build mode. */
const BUILT_INPUTS = new Set(["manifest", "semantic_manifest"]);

/** `name: value` lines <-> the dbt_vars map (numbers and booleans keep their type). */
function varsToText(v: unknown): string {
  if (!v || typeof v !== "object") return "";
  return Object.entries(v as Record<string, unknown>)
    .map(([k, x]) => `${k}: ${String(x)}`)
    .join("\n");
}
function textToVars(text: string): Record<string, string | number | boolean> | null {
  const out: Record<string, string | number | boolean> = {};
  for (const line of text.split("\n")) {
    const i = line.indexOf(":");
    if (!line.trim() || i < 1) continue;
    const raw = line.slice(i + 1).trim().replace(/^(['"])(.*)\1$/, "$2");
    out[line.slice(0, i).trim()] = raw === "true" ? true : raw === "false" ? false : raw !== "" && !Number.isNaN(Number(raw)) ? Number(raw) : raw;
  }
  return Object.keys(out).length ? out : null;
}
const SECRET_WORDS = ["token", "secret", "password", "api_key"];
const MASK = "***";

const isSecret = (k: string) => !k.endsWith("_env") && SECRET_WORDS.some((w) => k.toLowerCase().includes(w));
const human = (k: string) =>
  k
    .replace(/_/g, " ")
    .replace(/\burl\b/g, "URL")
    .replace(/^./, (c) => c.toUpperCase());
type Where = "git" | "bucket";

/** Adapter help for a file input, reworded for where the files come from. */
function fileHelp(desc: string, required: boolean, where: Where): string {
  const text = desc.replace(/^Path to /, where === "git" ? "Path in the repository to " : "Location of ").replace(/\s*\(required\)\s*$/i, "");
  if (!required) return text;
  return where === "git" ? `${text}. Required.` : `${text}. Required: set a location or upload it when you sync.`;
}

function filePlaceholder(name: string, folder: boolean, where: Where, kind: string): string {
  if (where === "git") {
    if (folder) return kind === "cube" ? "model" : "path/to/project (. for the root)";
    return kind === "dbt" ? `target/${name}.json` : `${name}.json`;
  }
  return folder ? "s3://bucket/path/to/folder/" : `s3://bucket/path/${name}.json`;
}

/** dbt first: the common case. */
const ORDER = ["dbt", "cube", "sqlmesh"];
const byOrder = (a: SourceKindOut, b: SourceKindOut) => (ORDER.indexOf(a.kind) + 1 || 99) - (ORDER.indexOf(b.kind) + 1 || 99) || a.kind.localeCompare(b.kind);

interface SchemaProp {
  description?: string;
  default?: unknown;
}

/** The adapter fields the UI can edit: everything but host keys and *_env (CLI only). */
function adapterFields(kind: SourceKindOut | undefined): { name: string; prop: SchemaProp; required: boolean }[] {
  const schema = (kind?.config_schema ?? {}) as { properties?: Record<string, SchemaProp>; required?: string[] };
  const req = new Set(schema.required ?? []);
  return Object.entries(schema.properties ?? {})
    .filter(([k]) => !HOST_KEYS.has(k) && !k.endsWith("_env"))
    .map(([name, prop]) => ({ name, prop, required: req.has(name) }));
}

export function SourceForm(props: { kinds: SourceKindOut[]; editing: SourceOut | null; encryption: boolean; onClose: () => void }) {
  const e = props.editing;
  const kinds = [...props.kinds].sort(byOrder);
  const usable = kinds.filter((k) => k.available);
  const [kind, setKind] = useState(e?.kind ?? usable[0]?.kind ?? "dbt");
  const spec = props.kinds.find((k) => k.kind === kind);
  const files = new Set(spec?.file_inputs ?? []);
  const folders = new Set(spec?.folder_inputs ?? []);
  const fields = adapterFields(spec);
  const fileFields = fields.filter((f) => files.has(f.name));
  const otherFields = fields.filter((f) => !files.has(f.name));
  const requiredFiles = new Set(spec?.required_inputs ?? []);
  const cfg = (e?.config ?? {}) as Record<string, unknown>;

  const [name, setName] = useState(e?.name ?? "");
  const [connection, setConnection] = useState(e?.connection ?? "");
  const [publish, setPublish] = useState<"auto" | "preview">(e?.publish === "preview" ? "preview" : "auto");
  const [priority, setPriority] = useState(String(e?.priority ?? 0));
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(fields.map((f) => [f.name, cfg[f.name] == null || cfg[f.name] === MASK ? "" : String(cfg[f.name])])),
  );
  const [cleared, setCleared] = useState<Record<string, boolean>>({});
  // Where the files come from: a git repository, or a bucket / upload. Cube and SQLMesh
  // projects live in git; a dbt manifest is a build artifact, so dbt defaults to a bucket.
  const [where, setWhere] = useState<Where>(cfg.git_url ? "git" : e ? "bucket" : kind === "dbt" ? "bucket" : "git");
  const [gitUrl, setGitUrl] = useState(String(cfg.git_url ?? ""));
  const [gitRef, setGitRef] = useState(String(cfg.git_ref ?? ""));
  const [gitUser, setGitUser] = useState(String(cfg.git_username ?? ""));
  const [gitToken, setGitToken] = useState("");
  const tokenStored = cfg.git_token === MASK;
  const [tokenCleared, setTokenCleared] = useState(false);
  const [gitResult, setGitResult] = useState<GitTestOut | null>(null);
  const dbtParse = props.kinds.find((k) => k.kind === "dbt")?.dbt_parse ?? null;
  const [dbtMode, setDbtMode] = useState<DbtMode>(cfg.dbt_project_dir ? "build" : cfg.manifest || dbtParse?.available === false ? "manifest" : "build");
  const [projectDir, setProjectDir] = useState(String(cfg.dbt_project_dir ?? ""));
  const [targetSchema, setTargetSchema] = useState(String(cfg.dbt_target_schema ?? ""));
  const [targetName, setTargetName] = useState(String(cfg.dbt_target_name ?? ""));
  const [skipDeps, setSkipDeps] = useState(Boolean(cfg.dbt_skip_deps));
  const [varsText, setVarsText] = useState(varsToText(cfg.dbt_vars));
  const building = kind === "dbt" && where === "git" && dbtMode === "build";
  const gitTest = useMutation({
    mutationFn: () =>
      endpoints.testGit({
        url: gitUrl.trim(),
        ref: gitRef.trim() || null,
        username: gitUser.trim() || null,
        ...(gitToken ? { token: gitToken } : tokenStored && !tokenCleared && e ? { source_id: e.id } : {}),
      }),
    onMutate: () => setGitResult(null),
    onSuccess: setGitResult,
    onError: (err) => setGitResult({ ok: false, commit: null, error: errMsg(err) }),
  });
  const [every, setEvery] = useState(String(e?.sync_interval_hours ?? ""));
  // A schedule reads the stored locations, so every required file needs one (or a default).
  // dbt build mode produces the manifests itself, so they need no stored location.
  const unstored = fields.filter((f) => requiredFiles.has(f.name) && !(building && BUILT_INPUTS.has(f.name)) && !values[f.name]?.trim() && f.prop.default == null).map((f) => f.name);
  const intervals = e?.sync_interval_hours && !INTERVALS.some(([h]) => h === e.sync_interval_hours) ? [...INTERVALS, [e.sync_interval_hours, `Every ${e.sync_interval_hours} hours`] as [number, string]] : INTERVALS;
  const [error, setError] = useState<string | null>(null);
  const conns = useQuery({ queryKey: ["connections"], queryFn: endpoints.listConnections });
  const qc = useQueryClient();
  const toast = useToast();

  const config = () => {
    const out: Record<string, unknown> = { publish, priority: Number(priority) || 0 };
    if (connection) out.connection = connection;
    if (every && unstored.length === 0) out.sync_interval_hours = Number(every);
    if (where === "git") {
      out.git_url = gitUrl.trim();
      if (gitRef.trim()) out.git_ref = gitRef.trim();
      if (gitUser.trim()) out.git_username = gitUser.trim();
      if (gitToken) out.git_token = gitToken;
      else if (tokenStored) out.git_token = tokenCleared ? "" : MASK;
    }
    if (building) {
      out.dbt_project_dir = projectDir.trim() || ".";
      out.dbt_target_schema = targetSchema.trim();
      if (targetName.trim()) out.dbt_target_name = targetName.trim();
      if (skipDeps) out.dbt_skip_deps = true;
      const vars = textToVars(varsText);
      if (vars) out.dbt_vars = vars;
    }
    for (const f of fields) {
      if (building && BUILT_INPUTS.has(f.name)) continue;
      const v = values[f.name]?.trim() ?? "";
      if (isSecret(f.name)) {
        if (v) out[f.name] = v;
        else if (cfg[f.name] === MASK) out[f.name] = cleared[f.name] ? "" : MASK;
      } else if (v) {
        out[f.name] = v;
      }
    }
    return out;
  };

  const save = useMutation({
    mutationFn: () => (e ? endpoints.updateSource(e.id, config()) : endpoints.createSource({ kind, name, config: config() })),
    onSuccess: (s) => {
      toast("ok", e ? `Saved ${s.name}` : `Added ${s.name}. Sync it to import its context.`);
      void qc.invalidateQueries({ queryKey: ["sources"] });
      props.onClose();
    },
    onError: (err) => setError(errMsg(err)),
  });

  const newSecret = fields.some((f) => isSecret(f.name) && values[f.name]) || (where === "git" && Boolean(gitToken));
  const blocked = newSecret && !props.encryption;

  return (
    <Modal
      open
      wide
      onClose={props.onClose}
      title={e ? `Edit ${e.name}` : "Add source"}
      footer={
        <>
          <button type="button" className="btn" onClick={props.onClose}>
            Cancel
          </button>
          <button type="submit" form="source-form" className="btn btn-primary" disabled={save.isPending || blocked || !spec?.available}>
            {save.isPending ? "Saving..." : "Save"}
          </button>
        </>
      }
    >
      <form
        id="source-form"
        className="space-y-6"
        onSubmit={(ev) => {
          ev.preventDefault();
          setError(null);
          save.mutate();
        }}
      >
        <fieldset className="grid gap-x-4 gap-y-4 sm:grid-cols-2">
          <legend className="label-caps mb-3">Source</legend>
          <label className="block">
            <span className="label">Kind</span>
            {e ? (
              <span className="flex h-9 items-center text-sm sm:h-8">{spec?.display_name ?? kind}</span>
            ) : (
              <select
                className="input w-full"
                value={kind}
                onChange={(ev) => {
                  const next = props.kinds.find((k) => k.kind === ev.target.value);
                  setKind(ev.target.value);
                  setValues(Object.fromEntries(adapterFields(next).map((f) => [f.name, ""])));
                  if (!gitUrl) setWhere(ev.target.value === "dbt" ? "bucket" : "git");
                }}
              >
                {kinds.map((k) => (
                  <option key={k.kind} value={k.kind} disabled={!k.available}>
                    {k.display_name ?? k.kind}
                    {k.available ? "" : " (not installed)"}
                  </option>
                ))}
              </select>
            )}
          </label>
          <label className="block">
            <span className="label">Name</span>
            <input className="input mono w-full" value={name} onChange={(ev) => setName(ev.target.value)} placeholder="jaffle" required disabled={Boolean(e)} autoFocus={!e} />
          </label>
          <label className="block sm:col-span-2">
            <span className="label">Warehouse connection</span>
            <select className="input w-full" value={connection} onChange={(ev) => setConnection(ev.target.value)}>
              <option value="">None</option>
              {(conns.data ?? []).map((c) => (
                <option key={c.id} value={c.name}>
                  {c.name}
                </option>
              ))}
            </select>
            <span className="mt-1 block text-xs text-muted">Links imported tables to the catalog and lets metrics be queried.</span>
          </label>
          <div>
            <span className="label flex items-center gap-1">
              Publishing
              <InfoTip text="Auto publishes a release on every sync. Preview creates a preview release you review and publish yourself." />
            </span>
            <Segmented
              label="Publishing"
              options={[
                { id: "auto", label: "Auto" },
                { id: "preview", label: "Preview" },
              ]}
              value={publish}
              onChange={setPublish}
            />
          </div>
          <label className="block">
            <span className="label flex items-center gap-1">
              Priority
              <InfoTip text="When two sources describe the same table, the lower number wins. Hand-written docs count as 0." />
            </span>
            <input className="input w-24 tabular-nums" inputMode="numeric" value={priority} onChange={(ev) => setPriority(ev.target.value)} />
          </label>
        </fieldset>

        {fileFields.length > 0 && (
          <section aria-label="Files" className="space-y-4 border-t border-line pt-5">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <h3 className="label-caps">Files</h3>
              <Segmented
                label="Where the files are"
                options={[
                  { id: "git", label: "Git repository" },
                  { id: "bucket", label: "Bucket or upload" },
                ]}
                value={where}
                onChange={(w) => {
                  setWhere(w);
                  setGitResult(null);
                }}
              />
            </div>
            {where === "git" ? (
              <>
                <p className="-mt-2 text-xs text-muted">
                  Each sync checks out the paths below from the repository (shallow, just those folders). Use a read-only token.
                </p>
                <div className="grid gap-x-4 gap-y-4 sm:grid-cols-[minmax(0,1fr)_12rem]">
                  <label className="block">
                    <span className="label">Repository URL</span>
                    <input
                      className="input mono w-full"
                      autoComplete="off"
                      spellCheck={false}
                      value={gitUrl}
                      onChange={(ev) => setGitUrl(ev.target.value)}
                      placeholder="https://github.com/acme/analytics.git"
                      required
                    />
                  </label>
                  <label className="block">
                    <span className="label">Branch, tag or commit</span>
                    <input className="input mono w-full" autoComplete="off" spellCheck={false} value={gitRef} onChange={(ev) => setGitRef(ev.target.value)} placeholder="default branch" />
                  </label>
                  <div>
                    <label className="block">
                      <span className="label">Access token (optional for public repositories)</span>
                      <input
                        className="input w-full"
                        type="password"
                        autoComplete="off"
                        value={gitToken}
                        disabled={tokenCleared}
                        onChange={(ev) => setGitToken(ev.target.value)}
                        placeholder={tokenStored ? "Stored. Leave empty to keep it." : "Fine-grained, read-only contents"}
                      />
                    </label>
                    <span className="mt-1 block text-xs text-muted">Encrypted at rest and never shown again.</span>
                    {tokenStored && (
                      <label className="mt-2 flex items-center gap-2 text-sm">
                        <input type="checkbox" checked={tokenCleared} onChange={(ev) => setTokenCleared(ev.target.checked)} />
                        Remove the stored token
                      </label>
                    )}
                  </div>
                  <label className="block">
                    <span className="label flex items-center gap-1">
                      Username
                      <InfoTip text="Sent with the token. GitHub and GitLab accept any value; Bitbucket needs your username." />
                    </span>
                    <input className="input mono w-full" autoComplete="off" spellCheck={false} value={gitUser} onChange={(ev) => setGitUser(ev.target.value)} placeholder="x-access-token" />
                  </label>
                </div>
                <div className="flex flex-wrap items-center gap-3">
                  <button type="button" className="btn btn-sm" disabled={!gitUrl.trim() || gitTest.isPending} onClick={() => gitTest.mutate()}>
                    {gitTest.isPending ? "Testing..." : "Test repository"}
                  </button>
                  {gitResult &&
                    (gitResult.ok ? (
                      <StatusDot tone="good">
                        reachable · <span className="normal-case">{gitResult.commit?.slice(0, 10)}</span>
                      </StatusDot>
                    ) : (
                      <span className="min-w-0 text-xs text-bad [overflow-wrap:anywhere]">{gitResult.error}</span>
                    ))}
                </div>
                {kind === "dbt" && (
                  <DbtBuildFields
                    mode={dbtMode}
                    setMode={setDbtMode}
                    status={dbtParse}
                    projectDir={projectDir}
                    setProjectDir={setProjectDir}
                    targetSchema={targetSchema}
                    setTargetSchema={setTargetSchema}
                    targetName={targetName}
                    setTargetName={setTargetName}
                    skipDeps={skipDeps}
                    setSkipDeps={setSkipDeps}
                    varsText={varsText}
                    setVarsText={setVarsText}
                  />
                )}
              </>
            ) : (
              <p className="-mt-2 text-xs text-muted">
                An <span className="font-mono">s3://</span> or <span className="font-mono">gs://</span> location the server reads on every sync, or leave it empty and upload the files when you sync (CI can do that on every build).
              </p>
            )}
            {fileFields.filter((f) => !(building && BUILT_INPUTS.has(f.name))).map((f) => (
              <label key={f.name} className="block">
                <span className="label">
                  {human(f.name)}
                  {where === "git" ? " path" : ""}
                  {requiredFiles.has(f.name) ? "" : " (optional)"}
                </span>
                <input
                  className="input mono w-full"
                  autoComplete="off"
                  spellCheck={false}
                  value={values[f.name] ?? ""}
                  onChange={(ev) => setValues({ ...values, [f.name]: ev.target.value })}
                  placeholder={filePlaceholder(f.name, folders.has(f.name), where, kind)}
                  required={where === "git" && requiredFiles.has(f.name)}
                />
                {f.prop.description && <span className="mt-1 block text-xs text-muted">{fileHelp(f.prop.description, requiredFiles.has(f.name), where)}</span>}
              </label>
            ))}
          </section>
        )}

        <section aria-label="Sync automatically" className="space-y-2 border-t border-line pt-5">
          <h3 className="label-caps">Sync automatically</h3>
          <label className="block">
            <span className="label flex items-center gap-1">
              Schedule
              <InfoTip text="The worker syncs the source when its last sync is older than this. A git source whose branch has no new commit is only checked, not re-imported. Push webhooks (on the source, after saving) sync on every push instead." />
            </span>
            <select className="input w-full sm:w-56" value={unstored.length ? "" : every} disabled={unstored.length > 0} onChange={(ev) => setEvery(ev.target.value)}>
              <option value="">Off (sync on request)</option>
              {intervals.map(([h, label]) => (
                <option key={h} value={String(h)}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          <span className="block text-xs text-muted">
            {unstored.length > 0
              ? `Needs a stored location for ${unstored.map(human).join(", ")}: uploaded files can only be synced by whoever uploads them (for example CI).`
              : "Runs in satchel worker, which checks every few minutes (SATCHEL_SOURCE_TICK_MIN)."}
          </span>
        </section>

        {otherFields.length > 0 && (
          <section aria-label="Options" className="grid gap-x-4 gap-y-4 border-t border-line pt-5 sm:grid-cols-2">
            <h3 className="label-caps sm:col-span-2">Options</h3>
            {otherFields.map((f) => {
              const secret = isSecret(f.name);
              const stored = secret && cfg[f.name] === MASK;
              return (
                <div key={f.name}>
                  <label className="block">
                    <span className="label">
                      {human(f.name)}
                      {f.required ? "" : " (optional)"}
                    </span>
                    <input
                      className={`input w-full ${secret ? "" : "mono"}`}
                      type={secret ? "password" : "text"}
                      autoComplete="off"
                      spellCheck={false}
                      value={values[f.name] ?? ""}
                      disabled={Boolean(cleared[f.name])}
                      onChange={(ev) => setValues({ ...values, [f.name]: ev.target.value })}
                      placeholder={stored ? "Stored. Leave empty to keep it." : f.prop.default != null ? String(f.prop.default) : ""}
                      required={f.required && !stored}
                    />
                  </label>
                  <span className="mt-1 block text-xs text-muted">{secret ? "Encrypted at rest and never shown again." : f.prop.description}</span>
                  {stored && (
                    <label className="mt-2 flex items-center gap-2 text-sm">
                      <input type="checkbox" checked={Boolean(cleared[f.name])} onChange={(ev) => setCleared({ ...cleared, [f.name]: ev.target.checked })} />
                      Remove the stored value
                    </label>
                  )}
                </div>
              );
            })}
          </section>
        )}

        {(blocked || error) && (
          <div className="space-y-3">
            {blocked && <Notice tone="warn">Secrets can't be saved: the server has no SATCHEL_ENCRYPTION_KEY.</Notice>}
            {error && <ErrorBox error={error} />}
          </div>
        )}
      </form>
    </Modal>
  );
}

/** dbt in Git repository mode: build manifest.json with dbt parse, or read a committed one. */
function DbtBuildFields(p: {
  mode: DbtMode;
  setMode: (m: DbtMode) => void;
  status: { available: boolean; message: string } | null;
  projectDir: string;
  setProjectDir: (v: string) => void;
  targetSchema: string;
  setTargetSchema: (v: string) => void;
  targetName: string;
  setTargetName: (v: string) => void;
  skipDeps: boolean;
  setSkipDeps: (v: boolean) => void;
  varsText: string;
  setVarsText: (v: string) => void;
}) {
  return (
    <div className="space-y-4 border-t border-line pt-4">
      <div>
        <span className="label">manifest.json</span>
        <Segmented
          label="How Satchel gets manifest.json"
          options={[
            { id: "build", label: "Build it from the dbt project (dbt parse)" },
            { id: "manifest", label: "Committed manifest path" },
          ]}
          value={p.mode}
          onChange={p.setMode}
        />
      </div>
      {p.mode === "manifest" ? (
        <p className="-mt-2 text-xs text-muted">Only works if the repository commits dbt&apos;s target/ folder. Otherwise build it with dbt parse, or choose Bucket or upload.</p>
      ) : (
        <>
          <p className="-mt-2 text-xs text-muted">
            Each sync runs <span className="font-mono">dbt deps</span> and <span className="font-mono">dbt parse</span> on the server, in a separate process without credentials. Parse does not connect to the warehouse, so there is
            no catalog.json: column types come from crawling the warehouse connection.
          </p>
          {p.status && !p.status.available && <Notice tone="warn">{p.status.message}</Notice>}
          <div className="grid gap-x-4 gap-y-4 sm:grid-cols-2">
            <label className="block">
              <span className="label">dbt project folder</span>
              <input className="input mono w-full" autoComplete="off" spellCheck={false} value={p.projectDir} onChange={(ev) => p.setProjectDir(ev.target.value)} placeholder=". for the repository root" />
              <span className="mt-1 block text-xs text-muted">The folder that holds dbt_project.yml.</span>
            </label>
            <label className="block">
              <span className="label flex items-center gap-1">
                Target schema
                <InfoTip text="target.schema of your production target in profiles.yml. dbt derives every model's schema from it, so Satchel can match the tables in the warehouse." />
              </span>
              <input className="input mono w-full" autoComplete="off" spellCheck={false} value={p.targetSchema} onChange={(ev) => p.setTargetSchema(ev.target.value)} placeholder="analytics" required />
            </label>
            <label className="block">
              <span className="label flex items-center gap-1">
                Target name (optional)
                <InfoTip text="target.name during parse, for projects that branch on it (for example in generate_schema_name)." />
              </span>
              <input className="input mono w-full" autoComplete="off" spellCheck={false} value={p.targetName} onChange={(ev) => p.setTargetName(ev.target.value)} placeholder="prod" />
            </label>
            <label className="block">
              <span className="label flex items-center gap-1">
                Variables (optional)
                <InfoTip text="Passed as dbt --vars, one name: value per line. Stored in plain text, so no secrets. env_var() sees no server variables: give env_var calls a default." />
              </span>
              <textarea className="input mono w-full" rows={2} spellCheck={false} value={p.varsText} onChange={(ev) => p.setVarsText(ev.target.value)} placeholder="start_date: 2024-01-01" />
            </label>
          </div>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={p.skipDeps} onChange={(ev) => p.setSkipDeps(ev.target.checked)} />
            Skip dbt deps (packages are committed in dbt_packages/; no network needed)
          </label>
        </>
      )}
    </div>
  );
}

export function UploadSync(props: { source: SourceOut; onDone: (r: SyncReportOut) => void; onClose: () => void }) {
  const a = props.source.adapter;
  const inputs = a?.file_inputs ?? [];
  const folders = new Set(a?.folder_inputs ?? []);
  const [picked, setPicked] = useState<Record<string, FileList | null>>({});
  const [error, setError] = useState<string | null>(null);
  const run = useMutation({
    mutationFn: () => {
      const form = new FormData();
      for (const [name, list] of Object.entries(picked)) {
        for (const f of Array.from(list ?? [])) form.append(name, f, f.name);
      }
      return endpoints.syncSourceWithFiles(props.source.id, form);
    },
    onSuccess: props.onDone,
    onError: (err) => setError(errMsg(err)),
  });
  const any = Object.values(picked).some((l) => l && l.length > 0);
  return (
    <Modal
      open
      onClose={props.onClose}
      title={`Upload and sync ${props.source.name}`}
      footer={
        <>
          <button type="button" className="btn" onClick={props.onClose}>
            Cancel
          </button>
          <button type="button" className="btn btn-primary" disabled={!any || run.isPending} onClick={() => run.mutate()}>
            {run.isPending ? "Syncing..." : "Upload and sync"}
          </button>
        </>
      }
    >
      <div className="space-y-4">
        <p className="text-sm text-muted">Files you upload are used for this sync only. Inputs you leave empty use the location saved on the source.</p>
        {inputs.map((name) => (
          <label key={name} className="block">
            <span className="label">
              {human(name)}
              {folders.has(name) ? " (select every file in the folder)" : ""}
            </span>
            <input className="block w-full text-sm" type="file" multiple={folders.has(name)} onChange={(ev) => setPicked({ ...picked, [name]: ev.target.files })} />
          </label>
        ))}
        {error && <ErrorBox error={error} />}
      </div>
    </Modal>
  );
}
