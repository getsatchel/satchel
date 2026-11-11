import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { endpoints } from "@/api/endpoints";
import type { ConnectionKind, ConnectionOut, ConnectionPatch, ConnectionTestOut, ConnectionTestRequest } from "@/api/types";
import { useToast } from "@/components/Toast";
import { EmptyState, ErrorBox, ICONS, Icon, InfoTip, Loading, Modal, Notice, SectionHeading, StatusDot, TableCard } from "@/components/ui";
import { errMsg } from "@/lib/format";

/** Next to the Test button, so the outcome is visible without scrolling the form. */
function TestBadge({ r }: { r: ConnectionTestOut | null }) {
  if (!r) return null;
  return (
    <span role="status" className="truncate">
      {r.ok ? <StatusDot tone="good">connected</StatusDot> : <StatusDot tone="bad">failed</StatusDot>}
      {r.ok && <span className="ml-1.5 font-mono text-xs text-muted">{r.latency_ms} ms</span>}
    </span>
  );
}

/** The full error, at the end of the form. */
function TestLine({ r }: { r: ConnectionTestOut | null }) {
  if (!r?.error) return null;
  return (
    <div className="mt-3 text-sm">
      {r.error && <p className="break-words font-mono text-xs text-bad [overflow-wrap:anywhere]">{r.error}</p>}
    </div>
  );
}

function ConnectionForm(props: {
  kinds: ConnectionKind[];
  editing: ConnectionOut | null;
  encryption: boolean;
  first: boolean;
  onClose: () => void;
}) {
  const e = props.editing;
  const available = props.kinds.filter((k) => k.available);
  const [kind, setKind] = useState(e?.kind ?? available[0]?.kind ?? "postgres");
  const spec = props.kinds.find((k) => k.kind === kind);
  const [name, setName] = useState(e?.name ?? "");
  const [values, setValues] = useState<Record<string, string>>(() => {
    const out: Record<string, string> = {};
    const cfg = (e?.config ?? {}) as Record<string, unknown>;
    for (const f of spec?.fields ?? []) {
      const v = cfg[f.name] ?? f.default;
      out[f.name] = v == null ? "" : String(v);
    }
    return out;
  });
  const [secret, setSecret] = useState("");
  const [passphrase, setPassphrase] = useState("");
  const [clearSecret, setClearSecret] = useState(false);
  const [makeDefault, setMakeDefault] = useState(e ? e.default_for_ai : props.first);
  const [result, setResult] = useState<ConnectionTestOut | null>(null);
  const [error, setError] = useState<string | null>(null);
  const qc = useQueryClient();
  const toast = useToast();

  const config = () => Object.fromEntries(Object.entries(values).filter(([, v]) => v.trim() !== ""));

  const test = useMutation({
    mutationFn: () => {
      const req: ConnectionTestRequest = { kind, config: config() };
      if (e) req.connection_id = e.id;
      if (secret) {
        req.secret = secret;
        if (passphrase) req.passphrase = passphrase;
      } else if (clearSecret) req.secret = null;
      return endpoints.testConnection(req);
    },
    onMutate: () => setResult(null),
    onSuccess: setResult,
    onError: (err) => setResult({ ok: false, latency_ms: 0, error: errMsg(err) }),
  });

  const save = useMutation({
    mutationFn: () => {
      if (!e) {
        return endpoints.createConnection({ name, kind, config: config(), secret: secret || null, passphrase: (secret && passphrase) || null, default_for_ai: makeDefault });
      }
      const patch: ConnectionPatch = { config: config() };
      if (name !== e.name) patch.name = name;
      if (secret) {
        patch.secret = secret;
        if (passphrase) patch.passphrase = passphrase;
      } else if (clearSecret) patch.secret = null;
      if (makeDefault && !e.default_for_ai) patch.default_for_ai = true;
      return endpoints.updateConnection(e.id, patch);
    },
    onSuccess: (c) => {
      toast("ok", e ? `Saved ${c.name}` : `Added ${c.name}. Crawl it to fill the catalog.`);
      void qc.invalidateQueries({ queryKey: ["connections"] });
      props.onClose();
    },
    onError: (err) => setError(errMsg(err)),
  });

  const blocked = Boolean(secret) && !props.encryption;
  const secretPlaceholder =
    e?.secret === "stored" ? "Stored. Leave empty to keep it." : e?.secret === "env" ? "Read from an environment variable (set with the CLI). Type to replace." : "";

  return (
    <Modal
      open
      onClose={props.onClose}
      title={e ? `Edit ${e.name}` : "Add connection"}
      footer={
        <>
          <div className="mr-auto flex min-w-0 items-center gap-2">
            <button type="button" className="btn" onClick={() => test.mutate()} disabled={test.isPending || !spec?.available}>
              {test.isPending ? "Testing..." : "Test"}
            </button>
            <TestBadge r={result} />
          </div>
          <button type="button" className="btn" onClick={props.onClose}>
            Cancel
          </button>
          <button type="submit" form="conn-form" className="btn btn-primary" disabled={save.isPending || blocked || !spec?.available}>
            {save.isPending ? "Saving..." : "Save"}
          </button>
        </>
      }
    >
      <form
        id="conn-form"
        className="space-y-4"
        onSubmit={(ev) => {
          ev.preventDefault();
          setError(null);
          save.mutate();
        }}
      >
        <label className="block">
          <span className="label">Name</span>
          <input className="input mono w-full" value={name} onChange={(ev) => setName(ev.target.value)} placeholder="warehouse" required autoFocus />
          <span className="mt-1 block text-xs text-muted">Agents pass this name to run_sql when there is more than one connection.</span>
        </label>
        <label className="block">
          <span className="label">Kind</span>
          {e ? (
            <span className="text-sm">{spec?.label ?? kind}</span>
          ) : (
            <select
              className="input w-full"
              value={kind}
              onChange={(ev) => {
                const k = props.kinds.find((x) => x.kind === ev.target.value);
                setKind(ev.target.value);
                setValues(Object.fromEntries((k?.fields ?? []).map((f) => [f.name, f.default == null ? "" : String(f.default)])));
                setSecret("");
                setPassphrase("");
                setResult(null);
              }}
            >
              {props.kinds.map((k) => (
                <option key={k.kind} value={k.kind} disabled={!k.available}>
                  {k.label}
                  {k.available ? "" : ` (${k.note ?? "unavailable"})`}
                </option>
              ))}
            </select>
          )}
        </label>
        {spec?.fields.map((f) => (
          <label key={f.name} className="block">
            <span className="label">{f.label}</span>
            <input
              className={`input w-full ${f.type === "text" ? "" : "mono"}`}
              inputMode={f.type === "number" ? "numeric" : undefined}
              title={f.type === "number" ? `${f.min} to ${f.max}` : undefined}
              value={values[f.name] ?? ""}
              onChange={(ev) => setValues({ ...values, [f.name]: ev.target.value })}
              placeholder={f.placeholder ?? (f.default != null ? String(f.default) : undefined)}
              required={f.required}
            />
            {f.help && <span className="mt-1 block text-xs text-muted">{f.help}</span>}
          </label>
        ))}
        {spec?.secret_label && (
          <div>
            <label className="block">
              <span className="label">{spec.secret_label}</span>
              {spec.secret_multiline ? (
                // A PEM or JSON key file: a password input would drop its newlines.
                <textarea
                  // Masked like a password field, so the key isn't readable on screen.
                  className="input mono w-full text-xs [-webkit-text-security:disc]"
                  rows={5}
                  autoComplete="off"
                  spellCheck={false}
                  value={secret}
                  disabled={clearSecret}
                  onChange={(ev) => setSecret(ev.target.value)}
                  placeholder={secretPlaceholder}
                />
              ) : (
                <input
                  className="input mono w-full"
                  type="password"
                  autoComplete="off"
                  spellCheck={false}
                  value={secret}
                  disabled={clearSecret}
                  onChange={(ev) => setSecret(ev.target.value)}
                  placeholder={secretPlaceholder}
                />
              )}
            </label>
            <span className="mt-1 block text-xs text-muted">
              {spec.secret_help ? `${spec.secret_help} ` : ""}Encrypted at rest and never shown again.
            </span>
            {spec.passphrase_label && secret && (
              <label className="mt-3 block">
                <span className="label">{spec.passphrase_label}</span>
                <input className="input mono w-full" type="password" autoComplete="off" spellCheck={false} value={passphrase} onChange={(ev) => setPassphrase(ev.target.value)} />
                <span className="mt-1 block text-xs text-muted">For an encrypted key. It decrypts the key once, before it is encrypted with the server key, and is not stored.</span>
              </label>
            )}
            {e?.secret && (
              <label className="mt-2 flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={clearSecret}
                  onChange={(ev) => {
                    setClearSecret(ev.target.checked);
                    if (ev.target.checked) setSecret("");
                  }}
                />
                Remove the stored {spec.secret_label.toLowerCase()}
              </label>
            )}
          </div>
        )}
        {spec?.note && <p className="text-xs text-muted">{spec.note}</p>}
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={makeDefault} disabled={Boolean(e?.default_for_ai)} onChange={(ev) => setMakeDefault(ev.target.checked)} />
          Default for agents
          <InfoTip text="run_sql and the other data tools use this connection when the agent does not name one." />
        </label>
        {blocked && (
          <Notice tone="warn">The {spec?.secret_label?.toLowerCase() ?? "secret"} can't be saved: the server has no SATCHEL_ENCRYPTION_KEY. You can still test.</Notice>
        )}
        {error && <ErrorBox error={error} />}
        <TestLine r={result} />
      </form>
    </Modal>
  );
}

export function ConnectionsSection({ admin, encryption }: { admin: boolean; encryption: boolean }) {
  const conns = useQuery({ queryKey: ["connections"], queryFn: endpoints.listConnections });
  const kinds = useQuery({ queryKey: ["connection-kinds"], queryFn: endpoints.listConnectionKinds });
  const [form, setForm] = useState<{ editing: ConnectionOut | null } | null>(null);
  const [removing, setRemoving] = useState<ConnectionOut | null>(null);
  const qc = useQueryClient();
  const toast = useToast();

  const crawl = useMutation({
    mutationFn: (c: ConnectionOut) => endpoints.crawlConnection(c.id),
    onSuccess: (r) => {
      toast("ok", `Crawled ${r.connection}: ${r.seen} objects, ${r.created} new, ${r.dropped} dropped`);
      void qc.invalidateQueries({ queryKey: ["connections"] });
      void qc.invalidateQueries({ queryKey: ["assets"] });
    },
    onError: (e) => toast("err", errMsg(e)),
  });
  const test = useMutation({
    mutationFn: (c: ConnectionOut) => endpoints.testConnection({ connection_id: c.id }),
    onSuccess: (r, c) => toast(r.ok ? "ok" : "err", r.ok ? `${c.name}: connected (${r.latency_ms} ms)` : `${c.name}: ${r.error ?? "failed"}`),
    onError: (e) => toast("err", errMsg(e)),
  });
  const makeDefault = useMutation({
    mutationFn: (c: ConnectionOut) => endpoints.updateConnection(c.id, { default_for_ai: true }),
    onSuccess: (c) => {
      toast("ok", `${c.name} is now the default for agents`);
      void qc.invalidateQueries({ queryKey: ["connections"] });
    },
    onError: (e) => toast("err", errMsg(e)),
  });
  const remove = useMutation({
    mutationFn: (c: ConnectionOut) => endpoints.deleteConnection(c.id),
    onSuccess: (_, c) => {
      toast("ok", `Removed ${c.name}`);
      setRemoving(null);
      void qc.invalidateQueries({ queryKey: ["connections"] });
      void qc.invalidateQueries({ queryKey: ["assets"] });
    },
    onError: (e) => toast("err", errMsg(e)),
  });

  if (conns.isPending || kinds.isPending) return <Loading />;
  if (conns.isError) return <ErrorBox error={conns.error} />;
  if (kinds.isError) return <ErrorBox error={kinds.error} />;
  const rows = conns.data;
  const label = (k: string) => kinds.data.find((x) => x.kind === k)?.label ?? k;
  const add = admin && (
    <button type="button" className="btn btn-primary btn-sm" onClick={() => setForm({ editing: null })}>
      <Icon d={ICONS.plus} size={14} /> Add connection
    </button>
  );
  const busy = (m: { isPending: boolean; variables?: ConnectionOut }, c: ConnectionOut) => m.isPending && m.variables?.id === c.id;

  return (
    <section>
      <SectionHeading
        icon={ICONS.database}
        title={
          <span className="inline-flex items-center gap-1">
            Connections
            <InfoTip text="Warehouses agents query through run_sql and the semantic layer, and that Satchel crawls into the catalog. Queries run read-only." />
          </span>
        }
        count={rows.length}
        actions={rows.length > 0 && add ? add : undefined}
      />
      {rows.length === 0 ? (
        <EmptyState title="No warehouse connected" action={add || undefined}>
          Connect a warehouse so agents can query it and Satchel can catalog its tables. {admin ? "" : "An admin can add one here."}
        </EmptyState>
      ) : (
        <TableCard>
          <table className="w-full text-sm">
            <thead>
              <tr>
                <th className="th w-full">Name</th>
                <th className="th w-px whitespace-nowrap">Kind</th>
                <th className="th hidden w-px whitespace-nowrap text-right md:table-cell">Assets</th>
                <th className="th hidden w-px whitespace-nowrap sm:table-cell">Secret</th>
                {admin && (
                  <th className="th w-px">
                    <span className="sr-only">Actions</span>
                  </th>
                )}
              </tr>
            </thead>
            <tbody>
              {rows.map((c) => {
                const cfg = (c.config ?? {}) as Record<string, unknown>;
                const where = cfg.host
                  ? `${String(cfg.user ?? "")}@${String(cfg.host)}/${String(cfg.database ?? "")}`
                  : cfg.account
                    ? `${String(cfg.user ?? "")}@${String(cfg.account)}/${String(cfg.database ?? "")}`
                    : cfg.project
                      ? `${String(cfg.data_project ?? cfg.project)}${cfg.datasets ? ` (${String(cfg.datasets)})` : ""}`
                      : cfg.path
                        ? String(cfg.path)
                        : null;
                return (
                  <tr key={c.id} className="border-t border-line">
                    <td className="td">
                      <div className="flex flex-wrap items-center gap-2">
                        <Link className="font-medium hover:underline" to={`/catalog?connection=${encodeURIComponent(c.name)}`}>
                          {c.name}
                        </Link>
                        {c.default_for_ai && <span className="badge badge-good">default</span>}
                      </div>
                      {where && <div className="mt-0.5 font-mono text-xs text-muted [overflow-wrap:anywhere]">{where}</div>}
                    </td>
                    <td className="td whitespace-nowrap">{label(c.kind)}</td>
                    <td className="td hidden whitespace-nowrap text-right tabular-nums md:table-cell">{c.asset_count}</td>
                    <td className="td hidden whitespace-nowrap text-xs text-muted sm:table-cell">
                      {c.secret === "stored" ? "encrypted" : c.secret === "env" ? <span title="An env:NAME reference set with the CLI">environment</span> : "–"}
                    </td>
                    {admin && (
                      <td className="td whitespace-nowrap">
                        <div className="flex justify-end gap-1">
                          <button type="button" className="btn btn-ghost btn-sm" onClick={() => test.mutate(c)} disabled={busy(test, c)}>
                            {busy(test, c) ? "Testing..." : "Test"}
                          </button>
                          <button type="button" className="btn btn-ghost btn-sm" onClick={() => crawl.mutate(c)} disabled={busy(crawl, c)}>
                            {busy(crawl, c) ? "Crawling..." : "Crawl"}
                          </button>
                          {!c.default_for_ai && (
                            <button type="button" className="btn btn-ghost btn-sm hidden lg:inline-flex" onClick={() => makeDefault.mutate(c)}>
                              Make default
                            </button>
                          )}
                          <button type="button" className="btn btn-ghost btn-sm" onClick={() => setForm({ editing: c })}>
                            Edit
                          </button>
                          <button type="button" className="btn btn-ghost btn-sm text-bad" onClick={() => setRemoving(c)} aria-label={`Remove ${c.name}`}>
                            Remove
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
      )}
      {form && <ConnectionForm kinds={kinds.data} editing={form.editing} encryption={encryption} first={rows.length === 0} onClose={() => setForm(null)} />}
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
          Agents can no longer query it. Its {removing?.asset_count ?? 0} catalogued tables and columns are marked dropped, so context that mentions them is flagged for review. Context items and their history are kept. A connection a source still uses can't be removed.
        </p>
      </Modal>
    </section>
  );
}
