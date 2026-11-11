import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { endpoints } from "@/api/endpoints";
import type { LlmConnection, LlmConnectionPatch, LlmDeployment, LlmTestOut, LlmTestRequest } from "@/api/types";
import { ApiError } from "@/api/client";
import { useToast } from "@/components/Toast";
import { DescList, EmptyState, ErrorBox, ICONS, Icon, InfoTip, Loading, Modal, Notice, PageHeader, SectionHeading, Segmented, StatusDot, TableCard, Tabs } from "@/components/ui";
import { ConnectionsSection } from "@/pages/SettingsConnections";
import { DeploymentSection, LimitsSection } from "@/pages/SettingsLimits";
import { errMsg, fullTime, timeAgo } from "@/lib/format";

type Provider = "anthropic" | "openai_compat";

const PROVIDERS: { id: Provider; label: string }[] = [
  { id: "anthropic", label: "Anthropic" },
  { id: "openai_compat", label: "OpenAI-compatible" },
];

const PROVIDER_LABEL: Record<Provider, string> = { anthropic: "Anthropic", openai_compat: "OpenAI-compatible" };

const HINTS: Record<Provider, { url: string; model: string; urlHelp: string }> = {
  anthropic: {
    url: "https://api.anthropic.com",
    model: "claude-sonnet-5-5",
    urlHelp: "Leave empty for api.anthropic.com. Set it for a gateway or proxy.",
  },
  openai_compat: {
    url: "https://api.openai.com/v1",
    model: "gpt-5",
    urlHelp: "Any /chat/completions endpoint: OpenAI, Azure OpenAI, vLLM, Ollama, LiteLLM.",
  },
};

const KEY_CMD = "satchel secrets generate-key";

function TestResult({ r }: { r: LlmTestOut | null }) {
  if (!r) return null;
  return (
    <div role="status" className="mt-3 text-sm">
      {r.ok ? (
        <StatusDot tone="good">connected</StatusDot>
      ) : (
        <StatusDot tone="bad">failed</StatusDot>
      )}
      <span className="ml-2 font-mono text-xs text-muted">
        {r.model_id ?? ""}
        {r.latency_ms != null ? ` · ${r.latency_ms} ms` : ""}
      </span>
      {r.error && <p className="mt-1 break-words font-mono text-xs text-bad [overflow-wrap:anywhere]">{r.error}</p>}
    </div>
  );
}

interface HeaderRow {
  name: string;
  value: string;
}

function HeadersEditor({ rows, onChange }: { rows: HeaderRow[]; onChange: (r: HeaderRow[]) => void }) {
  return (
    <div className="space-y-2">
      {rows.map((h, i) => (
        <div key={i} className="flex gap-2">
          <input
            className="input mono w-2/5 min-w-0"
            aria-label={`Header ${i + 1} name`}
            placeholder="X-Header"
            value={h.name}
            onChange={(e) => onChange(rows.map((r, j) => (j === i ? { ...r, name: e.target.value } : r)))}
          />
          <input
            className="input mono min-w-0 flex-1"
            type="password"
            autoComplete="off"
            aria-label={`Header ${i + 1} value`}
            placeholder="value"
            value={h.value}
            onChange={(e) => onChange(rows.map((r, j) => (j === i ? { ...r, value: e.target.value } : r)))}
          />
          <button type="button" className="btn btn-ghost btn-sm" aria-label={`Remove header ${i + 1}`} onClick={() => onChange(rows.filter((_, j) => j !== i))}>
            <Icon d={ICONS.close} size={14} />
          </button>
        </div>
      ))}
      <button type="button" className="btn btn-sm" onClick={() => onChange([...rows, { name: "", value: "" }])}>
        <Icon d={ICONS.plus} size={14} /> Add header
      </button>
    </div>
  );
}

function ConnectionForm(props: { editing: LlmConnection | null; encryption: boolean; first: boolean; onClose: () => void }) {
  const e = props.editing;
  const [name, setName] = useState(e?.name ?? "");
  const [provider, setProvider] = useState<Provider>((e?.provider as Provider) ?? "anthropic");
  const [baseUrl, setBaseUrl] = useState(e?.base_url ?? "");
  const [model, setModel] = useState(e?.default_model ?? "");
  const [apiKey, setApiKey] = useState("");
  const [clearKey, setClearKey] = useState(false);
  const [replaceHeaders, setReplaceHeaders] = useState(!e);
  const [headers, setHeaders] = useState<HeaderRow[]>([]);
  const [makeDefault, setMakeDefault] = useState(e ? e.is_default : props.first);
  const [result, setResult] = useState<LlmTestOut | null>(null);
  const [error, setError] = useState<string | null>(null);
  const qc = useQueryClient();
  const toast = useToast();
  const hint = HINTS[provider];

  const headerMap = (): Record<string, string> =>
    Object.fromEntries(headers.filter((h) => h.name.trim()).map((h) => [h.name.trim(), h.value]));

  const test = useMutation({
    mutationFn: () => {
      const req: LlmTestRequest = { provider, base_url: baseUrl || null, default_model: model || null };
      if (e) req.connection_id = e.id;
      if (apiKey) req.api_key = apiKey;
      else if (clearKey) req.api_key = null;
      if (replaceHeaders) req.extra_headers = headerMap();
      return endpoints.testLlmConnection(req);
    },
    onMutate: () => setResult(null),
    onSuccess: setResult,
    onError: (err) => setResult({ ok: false, model_id: null, latency_ms: null, error: errMsg(err) }),
  });

  const save = useMutation({
    mutationFn: () => {
      if (!e) {
        return endpoints.createLlmConnection({
          name,
          provider,
          base_url: baseUrl || null,
          default_model: model || null,
          api_key: apiKey || null,
          extra_headers: headerMap(),
          is_default: makeDefault || null,
        });
      }
      const patch: LlmConnectionPatch = { base_url: baseUrl || null, default_model: model || null };
      if (name !== e.name) patch.name = name;
      if (apiKey) patch.api_key = apiKey;
      else if (clearKey) patch.api_key = null;
      if (replaceHeaders) patch.extra_headers = headerMap();
      if (makeDefault !== e.is_default) patch.is_default = makeDefault;
      return endpoints.updateLlmConnection(e.id, patch);
    },
    onSuccess: (c) => {
      toast("ok", e ? `Saved ${c.name}` : `Added ${c.name}`);
      void qc.invalidateQueries({ queryKey: ["llm-connections"] });
      props.onClose();
    },
    onError: (err) => setError(errMsg(err)),
  });

  const needsKey = Boolean(apiKey) || (replaceHeaders && headers.some((h) => h.value));
  const blocked = needsKey && !props.encryption;

  return (
    <Modal
      open
      onClose={props.onClose}
      title={e ? `Edit ${e.name}` : "Add LLM connection"}
      footer={
        <>
          <div className="mr-auto flex min-w-0 items-center gap-2">
            <button type="button" className="btn" onClick={() => test.mutate()} disabled={test.isPending}>
              {test.isPending ? "Testing..." : "Test"}
            </button>
            {result && (
              <span role="status" className="truncate">
                {result.ok ? <StatusDot tone="good">connected</StatusDot> : <StatusDot tone="bad">failed</StatusDot>}
              </span>
            )}
          </div>
          <button type="button" className="btn" onClick={props.onClose}>
            Cancel
          </button>
          <button type="submit" form="llm-form" className="btn btn-primary" disabled={save.isPending || blocked}>
            {save.isPending ? "Saving..." : "Save"}
          </button>
        </>
      }
    >
      <form
        id="llm-form"
        className="space-y-4"
        onSubmit={(ev) => {
          ev.preventDefault();
          setError(null);
          save.mutate();
        }}
      >
        <label className="block">
          <span className="label">Name</span>
          <input className="input w-full" value={name} onChange={(ev) => setName(ev.target.value)} placeholder="claude" required autoFocus />
        </label>
        <div>
          <span className="label">Provider</span>
          {e ? (
            <span className="text-sm">{PROVIDER_LABEL[provider]}</span>
          ) : (
            <Segmented label="Provider" options={PROVIDERS} value={provider} onChange={setProvider} />
          )}
        </div>
        <label className="block">
          <span className="label">Base URL (optional)</span>
          <input className="input mono w-full" value={baseUrl} onChange={(ev) => setBaseUrl(ev.target.value)} placeholder={hint.url} inputMode="url" />
          <span className="mt-1 block text-xs text-muted">{hint.urlHelp}</span>
        </label>
        <label className="block">
          <span className="label">Default model{provider === "openai_compat" ? "" : " (optional)"}</span>
          <input className="input mono w-full" value={model} onChange={(ev) => setModel(ev.target.value)} placeholder={hint.model} required={provider === "openai_compat"} />
          <span className="mt-1 block text-xs text-muted">Used by curation and by eval suites that don't pin a model.</span>
        </label>
        <div>
          <label className="block">
            <span className="label">API key</span>
            <input
              className="input mono w-full"
              type="password"
              autoComplete="off"
              spellCheck={false}
              value={apiKey}
              disabled={clearKey}
              onChange={(ev) => setApiKey(ev.target.value)}
              placeholder={e?.has_api_key ? `Stored (${e.api_key_hint ?? "set"}). Leave empty to keep it.` : "sk-..."}
            />
          </label>
          <span className="mt-1 block text-xs text-muted">Encrypted at rest and never shown again.</span>
          {e?.has_api_key && (
            <label className="mt-2 flex items-center gap-2 text-sm">
              <input type="checkbox" checked={clearKey} onChange={(ev) => { setClearKey(ev.target.checked); if (ev.target.checked) setApiKey(""); }} />
              Remove the stored key
            </label>
          )}
        </div>
        <div>
          <span className="label">Extra headers (optional)</span>
          {replaceHeaders ? (
            <HeadersEditor rows={headers} onChange={setHeaders} />
          ) : (
            <div className="flex flex-wrap items-center gap-2 text-sm">
              {e && e.extra_header_names.length > 0 ? (
                e.extra_header_names.map((n) => <span key={n} className="badge font-mono">{n}</span>)
              ) : (
                <span className="text-muted">None</span>
              )}
              <button
                type="button"
                className="btn btn-sm"
                onClick={() => {
                  setReplaceHeaders(true);
                  setHeaders((e?.extra_header_names ?? []).map((n) => ({ name: n, value: "" })));
                }}
              >
                {e && e.extra_header_names.length > 0 ? "Replace" : "Add"}
              </button>
            </div>
          )}
          <span className="mt-1 block text-xs text-muted">For gateways that authenticate with their own header (Azure OpenAI's api-key, Portkey, Helicone). Values are encrypted.</span>
        </div>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={makeDefault} onChange={(ev) => setMakeDefault(ev.target.checked)} />
          Workspace default
        </label>
        {blocked && (
          <Notice tone="warn">
            Secrets can't be saved: the server has no SATCHEL_ENCRYPTION_KEY. You can still test.
          </Notice>
        )}
        {error && <ErrorBox error={error} />}
        <TestResult r={result} />
      </form>
    </Modal>
  );
}

function DeploymentCard({ d, admin }: { d: LlmDeployment; admin: boolean }) {
  const [result, setResult] = useState<LlmTestOut | null>(null);
  const test = useMutation({
    mutationFn: () => endpoints.testLlmConnection({ deployment: true }),
    onMutate: () => setResult(null),
    onSuccess: setResult,
    onError: (err) => setResult({ ok: false, model_id: null, latency_ms: null, error: errMsg(err) }),
  });
  return (
    <div className="card p-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <StatusDot tone="good">in use</StatusDot>
          <span className="badge">managed by deployment</span>
        </div>
        {admin && (
          <button type="button" className="btn btn-sm" onClick={() => test.mutate()} disabled={test.isPending}>
            {test.isPending ? "Testing..." : "Test"}
          </button>
        )}
      </div>
      <DescList
        rows={[
          ["Provider", PROVIDER_LABEL[d.provider as Provider] ?? d.provider],
          ["Model", <span className="font-mono">{d.model ?? "–"}</span>],
          ["Base URL", <span className="font-mono [overflow-wrap:anywhere]">{d.base_url ?? "provider default"}</span>],
          ["API key", d.has_api_key ? "set" : "–"],
        ]}
      />
      <p className="mt-3 text-xs text-muted">
        Set by <span className="font-mono">SATCHEL_CHAT_*</span> on the server, so it applies to every workspace and can't be changed here. The connections below take over when it's removed.
      </p>
      <TestResult r={result} />
    </div>
  );
}

function ConnectionsTable(props: {
  rows: LlmConnection[];
  admin: boolean;
  overridden: boolean;
  onEdit: (c: LlmConnection) => void;
  onDelete: (c: LlmConnection) => void;
}) {
  const qc = useQueryClient();
  const toast = useToast();
  const [results, setResults] = useState<Record<string, LlmTestOut>>({});
  const test = useMutation({
    mutationFn: (c: LlmConnection) => endpoints.testLlmConnection({ connection_id: c.id }),
    onSuccess: (r, c) => {
      setResults((m) => ({ ...m, [c.id]: r }));
      toast(r.ok ? "ok" : "err", r.ok ? `${c.name}: connected (${r.latency_ms} ms)` : `${c.name}: ${r.error ?? "failed"}`);
    },
    onError: (e) => toast("err", errMsg(e)),
  });
  const makeDefault = useMutation({
    mutationFn: (c: LlmConnection) => endpoints.updateLlmConnection(c.id, { is_default: true }),
    onSuccess: (c) => {
      toast("ok", `${c.name} is now the workspace default`);
      void qc.invalidateQueries({ queryKey: ["llm-connections"] });
    },
    onError: (e) => toast("err", errMsg(e)),
  });
  return (
    <TableCard>
      <table className="w-full text-sm">
        <thead>
          <tr>
            <th className="th w-full">Name</th>
            <th className="th w-px whitespace-nowrap">Provider</th>
            <th className="th hidden w-px whitespace-nowrap md:table-cell">Model</th>
            <th className="th hidden w-px whitespace-nowrap sm:table-cell">Key</th>
            <th className="th hidden w-px whitespace-nowrap lg:table-cell">Updated</th>
            {props.admin && <th className="th w-px"><span className="sr-only">Actions</span></th>}
          </tr>
        </thead>
        <tbody>
          {props.rows.map((c) => {
            const r = results[c.id];
            return (
              <tr key={c.id} className="border-t border-line">
                <td className="td">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-medium">{c.name}</span>
                    {c.is_default && (props.overridden ? <span className="badge" title="The deployment provider is used instead">default (overridden)</span> : <span className="badge badge-good">default</span>)}
                    {r && <StatusDot tone={r.ok ? "good" : "bad"}>{r.ok ? "ok" : "failed"}</StatusDot>}
                  </div>
                  {c.base_url && <div className="mt-0.5 font-mono text-xs text-muted [overflow-wrap:anywhere]">{c.base_url}</div>}
                </td>
                <td className="td whitespace-nowrap">{PROVIDER_LABEL[c.provider as Provider] ?? c.provider}</td>
                <td className="td hidden whitespace-nowrap font-mono text-xs md:table-cell">{c.default_model ?? "–"}</td>
                <td className="td hidden whitespace-nowrap font-mono text-xs sm:table-cell">
                  {c.has_api_key ? c.api_key_hint : "–"}
                  {c.extra_header_names.length > 0 && <span className="ml-1 text-muted" title={c.extra_header_names.join(", ")}>+{c.extra_header_names.length} hdr</span>}
                </td>
                <td className="td hidden whitespace-nowrap text-xs text-muted lg:table-cell" title={fullTime(c.updated_at)}>{timeAgo(c.updated_at)}</td>
                {props.admin && (
                  <td className="td whitespace-nowrap">
                    <div className="flex justify-end gap-1">
                      <button type="button" className="btn btn-ghost btn-sm" onClick={() => test.mutate(c)} disabled={test.isPending && test.variables?.id === c.id}>
                        {test.isPending && test.variables?.id === c.id ? "Testing..." : "Test"}
                      </button>
                      {!c.is_default && (
                        <button type="button" className="btn btn-ghost btn-sm hidden sm:inline-flex" onClick={() => makeDefault.mutate(c)}>
                          Make default
                        </button>
                      )}
                      <button type="button" className="btn btn-ghost btn-sm" onClick={() => props.onEdit(c)}>
                        Edit
                      </button>
                      <button type="button" className="btn btn-ghost btn-sm text-bad" onClick={() => props.onDelete(c)} aria-label={`Delete ${c.name}`}>
                        Delete
                      </button>
                    </div>
                  </td>
                )}
              </tr>
            );
          })}
        </tbody>
      </table>
    </TableCard>
  );
}

function ModelsSection({ admin }: { admin: boolean }) {
  const q = useQuery({ queryKey: ["llm-connections"], queryFn: endpoints.listLlmConnections });
  const [form, setForm] = useState<{ editing: LlmConnection | null } | null>(null);
  const [deleting, setDeleting] = useState<LlmConnection | null>(null);
  const qc = useQueryClient();
  const toast = useToast();
  const del = useMutation({
    mutationFn: (c: LlmConnection) => endpoints.deleteLlmConnection(c.id),
    onSuccess: (_, c) => {
      toast("ok", `Deleted ${c.name}`);
      setDeleting(null);
      void qc.invalidateQueries({ queryKey: ["llm-connections"] });
    },
    onError: (e) => toast("err", errMsg(e)),
  });

  if (q.isPending) return <Loading />;
  if (q.isError) {
    if (q.error instanceof ApiError && q.error.status === 403) {
      return <Notice>Model settings are visible to curators and admins.</Notice>;
    }
    return <ErrorBox error={q.error} />;
  }
  const d = q.data;
  const add = admin && (
    <button type="button" className="btn btn-primary btn-sm" onClick={() => setForm({ editing: null })}>
      <Icon d={ICONS.plus} size={14} /> Add connection
    </button>
  );
  return (
    <section>
      <SectionHeading
        icon={ICONS.bolt}
        title={
          <span className="inline-flex items-center gap-1">
            Models
            <InfoTip text="The LLM that curation uses to draft suggestions and that evals use to drive the reference agent and judges. Retrieval never calls it." />
          </span>
        }
        count={d.connections.length}
        actions={(d.connections.length > 0 || d.deployment) && add ? add : undefined}
      />
      <div className="space-y-4">
        {!d.encryption_configured && (
          <Notice tone="warn">
            The server has no <span className="font-mono">SATCHEL_ENCRYPTION_KEY</span>, so API keys can't be stored. Ask whoever runs the deployment to set one (<span className="font-mono">{KEY_CMD}</span>) and restart. Keyless endpoints such as a local Ollama still work.
          </Notice>
        )}
        {d.deployment && <DeploymentCard d={d.deployment} admin={admin} />}
        {!d.deployment && !d.effective && d.connections.length > 0 && (
          <Notice tone="warn">No connection is the workspace default, so curation drafts nothing and agent-driven eval cases are skipped. Make one the default.</Notice>
        )}
        {d.connections.length === 0 && !d.deployment ? (
          <EmptyState title="No model connected" action={add || undefined}>
            Without an LLM, curation still clusters signals and runs hygiene checks but drafts no suggestions, and eval cases that need an agent are skipped. {admin ? "Add a connection to Anthropic or any OpenAI-compatible endpoint." : "An admin can add one here."}
          </EmptyState>
        ) : (
          d.connections.length > 0 && (
            <ConnectionsTable rows={d.connections} admin={admin} overridden={Boolean(d.deployment)} onEdit={(c) => setForm({ editing: c })} onDelete={setDeleting} />
          )
        )}
      </div>
      {form && <ConnectionForm editing={form.editing} encryption={d.encryption_configured} first={d.connections.length === 0} onClose={() => setForm(null)} />}
      <Modal
        open={deleting !== null}
        onClose={() => setDeleting(null)}
        title={`Delete ${deleting?.name ?? ""}?`}
        footer={
          <>
            <button type="button" className="btn" onClick={() => setDeleting(null)}>
              Cancel
            </button>
            <button type="button" className="btn btn-danger" disabled={del.isPending} onClick={() => deleting && del.mutate(deleting)}>
              {del.isPending ? "Deleting..." : "Delete"}
            </button>
          </>
        }
      >
        <p className="text-sm">
          The stored key is destroyed.
          {deleting?.is_default ? " This is the workspace default: curation and evals have no model until you make another connection the default." : ""}
        </p>
      </Modal>
    </section>
  );
}

type Tab = "models" | "connections" | "limits" | "deployment";
const TABS: { id: Tab; label: string; admin?: boolean }[] = [
  { id: "models", label: "Models" },
  { id: "connections", label: "Connections" },
  { id: "limits", label: "Limits" },
  { id: "deployment", label: "Deployment", admin: true },
];

export function Settings() {
  const me = useQuery({ queryKey: ["me"], queryFn: endpoints.me, retry: false });
  // Whether secrets can be stored (shared by every tab).
  const llm = useQuery({ queryKey: ["llm-connections"], queryFn: endpoints.listLlmConnections, retry: false });
  const [params, setParams] = useSearchParams();
  const admin = me.data?.role === "admin";
  const tabs = TABS.filter((t) => !t.admin || admin);
  const tab: Tab = tabs.find((t) => t.id === params.get("tab"))?.id ?? "models";
  return (
    <>
      <PageHeader
        title="Settings"
        sub={
          <>
            Workspace configuration. Anything the deployment sets in its environment is shown read-only and takes precedence.
            {me.data && !admin && " Only admins can change settings."}
          </>
        }
      />
      <Tabs tabs={tabs} value={tab} onChange={(t) => setParams(t === "models" ? {} : { tab: t }, { replace: true })} />
      <div className="mt-6 space-y-9">
        {tab === "models" && <ModelsSection admin={admin} />}
        {tab === "connections" && <ConnectionsSection admin={admin} encryption={llm.data?.encryption_configured ?? true} />}
        {tab === "limits" && <LimitsSection admin={admin} />}
        {tab === "deployment" && <DeploymentSection />}
      </div>
    </>
  );
}
