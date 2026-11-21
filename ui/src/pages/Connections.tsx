import { useEffect, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { endpoints } from "@/api/endpoints";
import type { ConnectionKind, ConnectionOut, ConnectionPatch, ConnectionTestOut, ConnectionTestRequest } from "@/api/types";
import { initialMode, isRef, refsOnly, SecretModeSwitch, SecretRefInput, useSecretPolicy, type SecretMode } from "@/components/SecretRef";
import { useToast } from "@/components/Toast";
import { docsUrl } from "@/components/nav";
import { EmptyState, ErrorBox, ICONS, Icon, InfoTip, Loading, Modal, Notice, PageHeader, Segmented, Sheet, StatusDot, TableCard, useSheetParam } from "@/components/ui";
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

type Values = Record<string, string>;

function defaults(spec: ConnectionKind | undefined, cfg: Record<string, unknown> = {}): Values {
  const out: Values = {};
  for (const f of spec?.fields ?? []) {
    const v = cfg[f.name] ?? f.default;
    out[f.name] = v == null ? "" : String(v);
  }
  return out;
}

const nonEmpty = (values: Values) => Object.fromEntries(Object.entries(values).filter(([, v]) => v.trim() !== ""));

/** A generated credential: a key pair (the private key is the secret) or a password. */
interface Credential {
  kind: string;
  secret: string;
  publicKey?: string;
  password?: string;
}

const b64 = (buf: ArrayBuffer) => btoa(String.fromCharCode(...new Uint8Array(buf)));

/** Web Crypto needs a secure context (HTTPS or localhost). */
const canGenerate = () => typeof window !== "undefined" && window.isSecureContext && Boolean(window.crypto?.subtle);

async function generate(kind: string, what: "keypair" | "password"): Promise<Credential> {
  if (what === "password") {
    const bytes = new Uint8Array(18);
    crypto.getRandomValues(bytes);
    const password = b64(bytes.buffer).replace(/\+/g, "A").replace(/\//g, "b").replace(/=/g, "");
    return { kind, secret: password, password };
  }
  const pair = await crypto.subtle.generateKey(
    { name: "RSASSA-PKCS1-v1_5", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" },
    true,
    ["sign", "verify"],
  );
  const priv = b64(await crypto.subtle.exportKey("pkcs8", pair.privateKey));
  const pem = `-----BEGIN PRIVATE KEY-----\n${(priv.match(/.{1,64}/g) ?? []).join("\n")}\n-----END PRIVATE KEY-----\n`;
  return { kind, secret: pem, publicKey: b64(await crypto.subtle.exportKey("spki", pair.publicKey)) };
}

/** A kind's setup commands with the form's values (else each field's example) and the credential filled in. */
function fillSetup(spec: ConnectionKind, values: Values, cred: Credential | null): string {
  return (spec.setup ?? "").replace(/\{([a-z_|]+)\}/g, (_, token: string) => {
    const alts = token.split("|");
    for (const a of alts) {
      if (a === "public_key") return cred?.publicKey ?? "<the public key of your key pair>";
      if (a === "password") return cred?.password ?? "<password>";
      const v = values[a]?.trim();
      if (v) return v;
    }
    const f = spec.fields.find((x) => x.name === alts[alts.length - 1]);
    return f?.default != null ? String(f.default) : (f?.placeholder?.replace(/^e\.g\.\s*/, "") ?? `<${alts[0]}>`);
  });
}

function ConfigFields({ spec, values, onChange }: { spec: ConnectionKind | undefined; values: Values; onChange: (v: Values) => void }) {
  return (
    <>
      {spec?.fields.map((f) => (
        <label key={f.name} className="block">
          <span className="label">{f.label}</span>
          <input
            className={`input w-full ${f.type === "text" ? "" : "mono"}`}
            inputMode={f.type === "number" ? "numeric" : undefined}
            title={f.type === "number" ? `${f.min} to ${f.max}` : undefined}
            value={values[f.name] ?? ""}
            onChange={(ev) => onChange({ ...values, [f.name]: ev.target.value })}
            placeholder={f.placeholder ?? (f.default != null ? String(f.default) : undefined)}
            required={f.required}
          />
          {f.help && <span className="mt-1 block text-xs text-muted">{f.help}</span>}
        </label>
      ))}
    </>
  );
}

/**
 * The secret (password, key file) and, for an encrypted key, its passphrase; or, where the
 * deployment allows it, a secret manager reference that Satchel reads when it connects.
 */
function SecretField(props: {
  spec: ConnectionKind;
  secret: string;
  onSecret: (v: string) => void;
  passphrase: string;
  onPassphrase: (v: string) => void;
  placeholder?: string;
  disabled?: boolean;
  /** The saved reference, when editing a connection that has one. */
  current?: string | null;
}) {
  const { spec } = props;
  const policy = useSecretPolicy();
  const [mode, setMode] = useState<SecretMode>(() => initialMode(policy, props.current ?? props.secret));
  useEffect(() => {
    if (refsOnly(policy)) setMode("ref");
  }, [policy]);
  const switcher = (
    <SecretModeSwitch
      policy={policy}
      mode={mode}
      what={(spec.secret_label ?? "secret").toLowerCase()}
      onMode={(m) => {
        setMode(m);
        props.onSecret("");
        props.onPassphrase("");
      }}
    />
  );
  if (mode === "ref")
    return (
      <div className="space-y-3">
        {switcher}
        <SecretRefInput
          policy={policy}
          label={`${spec.secret_label ?? "Secret"} reference`}
          value={props.secret}
          onChange={props.onSecret}
          disabled={props.disabled}
          placeholder={props.current ? `${props.current} (type to replace)` : undefined}
        />
      </div>
    );
  return (
    <div className="space-y-3">
      {switcher}
      <div>
      <label className="block">
        <span className="label">{spec.secret_label}</span>
        {spec.secret_multiline ? (
          // A PEM or JSON key file: a password input would drop its newlines. Masked like one.
          <textarea
            className="input mono w-full text-xs [-webkit-text-security:disc]"
            rows={5}
            autoComplete="off"
            spellCheck={false}
            value={props.secret}
            disabled={props.disabled}
            onChange={(ev) => props.onSecret(ev.target.value)}
            placeholder={props.placeholder}
          />
        ) : (
          <input
            className="input mono w-full"
            type="password"
            autoComplete="off"
            spellCheck={false}
            value={props.secret}
            disabled={props.disabled}
            onChange={(ev) => props.onSecret(ev.target.value)}
            placeholder={props.placeholder}
          />
        )}
      </label>
      <span className="mt-1 block text-xs text-muted">
        {spec.secret_help ? `${spec.secret_help} ` : ""}Encrypted at rest and never shown again.
      </span>
      {spec.passphrase_label && props.secret && (
        <label className="mt-3 block">
          <span className="label">{spec.passphrase_label}</span>
          <input className="input mono w-full" type="password" autoComplete="off" spellCheck={false} value={props.passphrase} onChange={(ev) => props.onPassphrase(ev.target.value)} />
          <span className="mt-1 block text-xs text-muted">For an encrypted key. It decrypts the key once, before it is encrypted with the server key, and is not stored.</span>
        </label>
      )}
      </div>
    </div>
  );
}

function DefaultForAgents({ checked, disabled, onChange }: { checked: boolean; disabled?: boolean; onChange: (v: boolean) => void }) {
  return (
    <label className="flex items-center gap-2 text-sm">
      <input type="checkbox" checked={checked} disabled={disabled} onChange={(ev) => onChange(ev.target.checked)} />
      Default for agents
      <InfoTip text="run_sql and the other data tools use this connection when the agent does not name one." />
    </label>
  );
}

function SetupGuide({ note }: { note?: string | null }) {
  return (
    <p className="text-xs text-muted">
      {note ? `${note} ` : ""}
      <a className="text-accent hover:underline" href={docsUrl("integrations/warehouses")} target="_blank" rel="noreferrer">
        Setup guide
      </a>
    </p>
  );
}

/** The setup commands, with Copy. */
function SetupBlock({ lang, text }: { lang: "sql" | "shell"; text: string }) {
  const toast = useToast();
  return (
    <div>
      <div className="mb-1.5 flex items-center justify-between gap-2">
        <span className="label mb-0">{lang === "sql" ? "Setup SQL" : "Setup commands"}</span>
        <button
          type="button"
          className="btn btn-sm"
          onClick={() =>
            void navigator.clipboard.writeText(text).then(
              () => toast("ok", lang === "sql" ? "Setup SQL copied" : "Setup commands copied"),
              () => toast("err", "Copy failed: select the text and copy it"),
            )
          }
        >
          Copy
        </button>
      </div>
      <pre className="max-h-80 overflow-auto rounded-md border border-line bg-sunken p-3 font-mono text-xs leading-relaxed">{text}</pre>
    </div>
  );
}

/** The one-action second state after a create: crawl the new connection. */
function CrawlStep({ crawl }: { crawl: ReturnType<typeof useCrawlNew> }) {
  return (
    <div className="space-y-3 text-sm">
      {crawl.isSuccess ? (
        <p role="status">
          {crawl.data.seen.toLocaleString()} objects found, {crawl.data.created.toLocaleString()} new. See them in the{" "}
          <Link className="text-accent hover:underline" to="/catalog">
            Catalog
          </Link>
          .
        </p>
      ) : (
        <p>Crawl it to list its schemas, tables and columns in the Catalog. A crawl reads names and types, not rows.</p>
      )}
      {crawl.isError && <ErrorBox error={crawl.error} />}
    </div>
  );
}

function useCrawlNew(id: string | undefined) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => endpoints.crawlConnection(id ?? ""),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["connections"] });
      void qc.invalidateQueries({ queryKey: ["assets"] });
    },
  });
}

function CrawlFooter({ crawl, onClose }: { crawl: ReturnType<typeof useCrawlNew>; onClose: () => void }) {
  if (crawl.isSuccess)
    return (
      <button type="button" className="btn btn-primary" onClick={onClose}>
        Close
      </button>
    );
  return (
    <>
      <button type="button" className="btn" onClick={onClose} disabled={crawl.isPending}>
        Not now
      </button>
      <button type="button" className="btn btn-primary" onClick={() => crawl.mutate()} disabled={crawl.isPending}>
        {crawl.isPending ? "Crawling..." : "Crawl"}
      </button>
    </>
  );
}

function useTest(build: () => ConnectionTestRequest) {
  const [result, setResult] = useState<ConnectionTestOut | null>(null);
  const test = useMutation({
    mutationFn: () => endpoints.testConnection(build()),
    onMutate: () => setResult(null),
    onSuccess: setResult,
    onError: (err) => setResult({ ok: false, latency_ms: 0, error: errMsg(err) }),
  });
  return { test, result, reset: () => setResult(null) };
}

function TestButton({ t, disabled }: { t: ReturnType<typeof useTest>; disabled?: boolean }) {
  return (
    <>
      <button type="button" className="btn" onClick={() => t.test.mutate()} disabled={t.test.isPending || disabled}>
        {t.test.isPending ? "Testing..." : "Test"}
      </button>
      <TestBadge r={t.result} />
    </>
  );
}

/**
 * Adding a connection, in named steps: the warehouse, its details, the setup to run in the
 * warehouse (with a generated key pair or password), then a crawl. A kind with no setup skips
 * Prepare.
 */
function AddConnection(props: { kinds: ConnectionKind[]; encryption: boolean; first: boolean; onClose: () => void }) {
  const available = props.kinds.filter((k) => k.available);
  const [step, setStep] = useState(0);
  const [kind, setKind] = useState(available[0]?.kind ?? "postgres");
  const spec = props.kinds.find((k) => k.kind === kind);
  const [name, setName] = useState("");
  const [values, setValues] = useState<Values>(() => defaults(spec));
  const policy = useSecretPolicy();
  const [own, setOwn] = useState(!canGenerate());
  const [cred, setCred] = useState<Credential | null>(null);
  const [secret, setSecret] = useState("");
  const [passphrase, setPassphrase] = useState("");
  const [makeDefault, setMakeDefault] = useState(props.first);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<{ id: string; name: string } | null>(null);
  const qc = useQueryClient();
  const crawl = useCrawlNew(saved?.id);

  const names = spec?.setup ? ["Kind", "Details", "Prepare", "Crawl"] : ["Kind", "Details", "Crawl"];
  const lastForm = names.length - 2;
  // A generated key or password would be stored: a references-only deployment can't use it.
  const generating = Boolean(spec?.generates) && !own && !refsOnly(policy);
  const usedSecret = generating ? (cred?.kind === kind ? cred.secret : "") : secret;
  const usedPassphrase = generating ? "" : passphrase;

  // Make the key pair or password when Prepare first opens (and again for another kind).
  useEffect(() => {
    if (step !== 2 || !generating || !spec?.generates || cred?.kind === kind) return;
    let live = true;
    generate(kind, spec.generates).then(
      (c) => live && setCred(c),
      (e: unknown) => live && setError(errMsg(e)),
    );
    return () => {
      live = false;
    };
  }, [step, generating, kind, spec?.generates, cred?.kind]);

  const t = useTest(() => ({ kind, config: nonEmpty(values), secret: usedSecret || undefined, passphrase: usedPassphrase || undefined }));
  const save = useMutation({
    mutationFn: () =>
      endpoints.createConnection({ name, kind, config: nonEmpty(values), secret: usedSecret || null, passphrase: (usedSecret && usedPassphrase) || null, default_for_ai: makeDefault }),
    onSuccess: (c) => {
      void qc.invalidateQueries({ queryKey: ["connections"] });
      setSaved({ id: c.id, name: c.name });
    },
    onError: (err) => setError(errMsg(err)),
  });

  const pickKind = (next: string) => {
    setKind(next);
    setValues(defaults(props.kinds.find((x) => x.kind === next)));
    setSecret("");
    setPassphrase("");
    t.reset();
  };
  const blocked = Boolean(usedSecret) && !isRef(usedSecret) && !props.encryption;
  const dirty = !saved && (step > 0 || name !== "");
  const isKeypair = spec?.generates === "keypair";

  return (
    <Sheet
      open
      wide
      onClose={props.onClose}
      dirty={dirty}
      title={saved ? `Added ${saved.name}` : "Add connection"}
      steps={{ current: saved ? names.length - 1 : step, names }}
      footer={
        saved ? (
          <CrawlFooter crawl={crawl} onClose={props.onClose} />
        ) : (
          <>
            <div className="mr-auto flex min-w-0 items-center gap-2">
              {step > 0 && (
                <button
                  type="button"
                  className="btn"
                  onClick={() => {
                    setError(null);
                    setStep(step - 1);
                  }}
                >
                  Back
                </button>
              )}
              {step === lastForm && <TestButton t={t} disabled={!spec?.available || (generating && !usedSecret)} />}
            </div>
            {step === 0 && (
              <button type="button" className="btn" onClick={props.onClose}>
                Cancel
              </button>
            )}
            <button type="submit" form="conn-add" className="btn btn-primary" disabled={save.isPending || !spec?.available || (step === lastForm && blocked)}>
              {step < lastForm ? "Next" : save.isPending ? "Saving..." : "Save"}
            </button>
          </>
        )
      }
    >
      {saved ? (
        <CrawlStep crawl={crawl} />
      ) : (
        <form
          id="conn-add"
          className="max-w-xl space-y-4"
          onSubmit={(ev) => {
            ev.preventDefault();
            setError(null);
            if (step < lastForm) setStep(step + 1);
            else save.mutate();
          }}
        >
          {step === 0 && (
            <>
              <div>
                <span className="label">Kind</span>
                <Segmented
                  label="Kind"
                  value={kind}
                  onChange={pickKind}
                  options={props.kinds.map((k) => ({ id: k.kind, label: k.label, disabled: k.available ? null : (k.note ?? "Not installed on this server") }))}
                />
              </div>
              <label className="block">
                <span className="label">Name</span>
                <input className="input mono w-full" value={name} onChange={(ev) => setName(ev.target.value)} placeholder="warehouse" required />
                <span className="mt-1 block text-xs text-muted">Agents pass this name to run_sql when there is more than one connection.</span>
              </label>
              <SetupGuide note={spec?.note} />
            </>
          )}
          {step === 1 && (
            <>
              <ConfigFields spec={spec} values={values} onChange={setValues} />
              {!spec?.setup && spec?.secret_label && (
                <SecretField spec={spec} secret={secret} onSecret={setSecret} passphrase={passphrase} onPassphrase={setPassphrase} />
              )}
              <DefaultForAgents checked={makeDefault} onChange={setMakeDefault} />
            </>
          )}
          {step === 2 && spec?.setup && (
            <>
              <p className="text-sm">
                {spec.setup_lang === "sql"
                  ? `Run this in ${spec.label} as an admin. It creates a role and user that can only read.`
                  : "Run these as a project owner. They create a service account that can only run queries and read data."}
              </p>
              {spec.generates && canGenerate() && !refsOnly(policy) && (
                <Segmented
                  label="Credential"
                  value={own ? "own" : "generate"}
                  onChange={(v) => {
                    setOwn(v === "own");
                    t.reset();
                  }}
                  options={[
                    { id: "generate", label: isKeypair ? "Generate a key pair" : "Generate a password" },
                    { id: "own", label: isKeypair ? "Use my own key" : "Use my own password" },
                  ]}
                />
              )}
              <SetupBlock lang={spec.setup_lang} text={fillSetup(spec, values, generating && cred?.kind === kind ? cred : null)} />
              {generating ? (
                <p className="text-xs text-muted" role="status">
                  {!usedSecret
                    ? "Generating..."
                    : isKeypair
                      ? "Satchel made this key pair in your browser. The public key is in the SQL; the private key is saved encrypted and never shown."
                      : "Satchel made this password in your browser. It is in the SQL, and saved encrypted."}
                </p>
              ) : (
                spec.secret_label && <SecretField spec={spec} secret={secret} onSecret={setSecret} passphrase={passphrase} onPassphrase={setPassphrase} />
              )}
              {spec.generates && !canGenerate() && !refsOnly(policy) && <Notice tone="info">Generating a key needs HTTPS, so paste your own.</Notice>}
            </>
          )}
          {step === lastForm && blocked && (
            <Notice tone="warn">The {spec?.secret_label?.toLowerCase() ?? "secret"} can't be saved: the server has no SATCHEL_ENCRYPTION_KEY. You can still test.</Notice>
          )}
          {error && <ErrorBox error={error} />}
          {step === lastForm && <TestLine r={t.result} />}
        </form>
      )}
    </Sheet>
  );
}

/** Editing a connection: the whole form at once. */
function EditConnection(props: { kinds: ConnectionKind[]; editing: ConnectionOut; encryption: boolean; onClose: () => void }) {
  const e = props.editing;
  const spec = props.kinds.find((k) => k.kind === e.kind);
  const [name, setName] = useState(e.name);
  const [values, setValues] = useState<Values>(() => defaults(spec, (e.config ?? {}) as Record<string, unknown>));
  const [initial] = useState(() => JSON.stringify(values));
  const [secret, setSecret] = useState("");
  const [passphrase, setPassphrase] = useState("");
  const [clearSecret, setClearSecret] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const qc = useQueryClient();
  const toast = useToast();

  const t = useTest(() => {
    const req: ConnectionTestRequest = { kind: e.kind, config: nonEmpty(values), connection_id: e.id };
    if (secret) {
      req.secret = secret;
      if (passphrase) req.passphrase = passphrase;
    } else if (clearSecret) req.secret = null;
    return req;
  });
  const save = useMutation({
    mutationFn: () => {
      const patch: ConnectionPatch = { config: nonEmpty(values) };
      if (name !== e.name) patch.name = name;
      if (secret) {
        patch.secret = secret;
        if (passphrase) patch.passphrase = passphrase;
      } else if (clearSecret) patch.secret = null;
      return endpoints.updateConnection(e.id, patch);
    },
    onSuccess: (c) => {
      void qc.invalidateQueries({ queryKey: ["connections"] });
      toast("ok", `Saved ${c.name}`);
      props.onClose();
    },
    onError: (err) => setError(errMsg(err)),
  });

  const blocked = Boolean(secret) && !isRef(secret) && !props.encryption;
  const dirty = name !== e.name || JSON.stringify(values) !== initial || secret !== "" || passphrase !== "" || clearSecret;
  const placeholder = e.secret === "stored" ? "Stored. Leave empty to keep it." : e.secret === "env" ? "Read from an environment variable (set with the CLI). Type to replace." : "";
  const currentRef = e.secret === "reference" ? (e.secret_ref ?? null) : null;

  return (
    <Sheet
      open
      onClose={props.onClose}
      dirty={dirty}
      title={`Edit ${e.name}`}
      footer={
        <>
          <div className="mr-auto flex min-w-0 items-center gap-2">
            <TestButton t={t} disabled={!spec?.available} />
          </div>
          <button type="button" className="btn" onClick={props.onClose}>
            Cancel
          </button>
          <button type="submit" form="conn-edit" className="btn btn-primary" disabled={save.isPending || blocked || !spec?.available}>
            {save.isPending ? "Saving..." : "Save"}
          </button>
        </>
      }
    >
      <form
        id="conn-edit"
        className="space-y-4"
        onSubmit={(ev) => {
          ev.preventDefault();
          setError(null);
          save.mutate();
        }}
      >
        <div>
          <span className="label">Kind</span>
          <span className="text-sm">{spec?.label ?? e.kind}</span>
        </div>
        <label className="block">
          <span className="label">Name</span>
          <input className="input mono w-full" value={name} onChange={(ev) => setName(ev.target.value)} required />
          <span className="mt-1 block text-xs text-muted">Agents pass this name to run_sql when there is more than one connection.</span>
        </label>
        <ConfigFields spec={spec} values={values} onChange={setValues} />
        {spec?.secret_label && (
          <div>
            <SecretField spec={spec} secret={secret} onSecret={setSecret} passphrase={passphrase} onPassphrase={setPassphrase} placeholder={placeholder} disabled={clearSecret} current={currentRef} />
            {e.secret && (
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
        <SetupGuide note={spec?.note} />
        {blocked && <Notice tone="warn">The {spec?.secret_label?.toLowerCase() ?? "secret"} can't be saved: the server has no SATCHEL_ENCRYPTION_KEY. You can still test.</Notice>}
        {error && <ErrorBox error={error} />}
        <TestLine r={t.result} />
      </form>
    </Sheet>
  );
}

/** Warehouse connections: what agents query, and what the Catalog is crawled from. */
export function Connections() {
  const me = useQuery({ queryKey: ["me"], queryFn: endpoints.me, retry: false });
  // Whether secrets can be stored.
  const llm = useQuery({ queryKey: ["llm-connections"], queryFn: endpoints.listLlmConnections, retry: false });
  const admin = me.data?.role === "admin";
  const encryption = llm.data?.encryption_configured ?? true;
  const conns = useQuery({ queryKey: ["connections"], queryFn: endpoints.listConnections });
  const kinds = useQuery({ queryKey: ["connection-kinds"], queryFn: endpoints.listConnectionKinds });
  // ?sheet=new adds a connection (the dashboard's Set up list links there); ?sheet=<id> edits one.
  const [sheet, openSheet, closeForm] = useSheetParam();
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

  // A ?sheet= for a removed connection, or one this role can't open: drop it rather than show nothing.
  const stale = Boolean(sheet) && conns.isSuccess && me.isSuccess && !(admin && (sheet === "new" || conns.data.some((c) => c.id === sheet)));
  useEffect(() => {
    if (stale) closeForm();
  }, [stale]); // eslint-disable-line react-hooks/exhaustive-deps
  const header = (actions?: ReactNode) => (
    <PageHeader
      title="Connections"
      sub={<>The warehouses agents query, read-only, and that Satchel crawls into the Catalog.{me.data && !admin && " Only admins can add or change them."}</>}
      actions={actions}
    />
  );
  if (conns.isPending || kinds.isPending) return <>{header()}<Loading /></>;
  if (conns.isError || kinds.isError) return <>{header()}<ErrorBox error={conns.error ?? kinds.error} /></>;
  const rows = conns.data;
  const editing = sheet && sheet !== "new" ? rows.find((c) => c.id === sheet) : undefined;
  const form = admin && (sheet === "new" || editing) ? { editing: editing ?? null } : null;
  const label = (k: string) => kinds.data.find((x) => x.kind === k)?.label ?? k;
  const add = admin && (
    <button type="button" className="btn btn-primary" onClick={() => openSheet("new")}>
      <Icon d={ICONS.plus} size={14} /> Add connection
    </button>
  );
  const busy = (m: { isPending: boolean; variables?: ConnectionOut }, c: ConnectionOut) => m.isPending && m.variables?.id === c.id;

  return (
    <>
      {header(rows.length > 0 && add ? add : undefined)}
      {rows.length === 0 ? (
        <EmptyState title="No warehouse connected" action={add || undefined}>
          Connect a warehouse so agents can query it and Satchel can catalog its tables. {admin ? "" : "An admin can add one."}
        </EmptyState>
      ) : (
        <TableCard>
          <thead>
            <tr>
              <th className="th w-full">Name</th>
              <th className="th hidden w-px whitespace-nowrap sm:table-cell">Kind</th>
              <th className="th hidden w-px whitespace-nowrap text-right md:table-cell">Assets</th>
              <th className="th hidden w-px whitespace-nowrap sm:table-cell">Secret</th>
              {admin && (
                <th className="th hidden w-px lg:table-cell">
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
              // Below `lg` the actions fold under the name, so the name and path keep the width.
              const actions = (
                <>
                  <button type="button" className="btn btn-ghost btn-sm" onClick={() => test.mutate(c)} disabled={busy(test, c)}>
                    {busy(test, c) ? "Testing..." : "Test"}
                  </button>
                  <button type="button" className="btn btn-ghost btn-sm" onClick={() => crawl.mutate(c)} disabled={busy(crawl, c)}>
                    {busy(crawl, c) ? "Crawling..." : "Crawl"}
                  </button>
                  {!c.default_for_ai && (
                    <button type="button" className="btn btn-ghost btn-sm" onClick={() => makeDefault.mutate(c)}>
                      Make default
                    </button>
                  )}
                  <button type="button" className="btn btn-ghost btn-sm" onClick={() => openSheet(c.id)}>
                    Edit
                  </button>
                  <button type="button" className="btn btn-ghost btn-sm text-bad" onClick={() => setRemoving(c)} aria-label={`Remove ${c.name}`}>
                    Remove
                  </button>
                </>
              );
              return (
                <tr key={c.id} className="border-t border-line">
                  <td className="td">
                    <div className="flex flex-wrap items-center gap-2">
                      <Link className="font-medium hover:underline" to={`/catalog?connection=${encodeURIComponent(c.name)}`}>
                        {c.name}
                      </Link>
                      <span className="text-xs text-muted sm:hidden">{label(c.kind)}</span>
                      {c.default_for_ai && <span className="badge badge-good">default</span>}
                    </div>
                    {where && <div className="mt-0.5 font-mono text-xs text-muted [overflow-wrap:anywhere]">{where}</div>}
                    {admin && <div className="-ml-2 mt-1.5 flex flex-wrap gap-1 lg:hidden">{actions}</div>}
                  </td>
                  <td className="td hidden whitespace-nowrap sm:table-cell">{label(c.kind)}</td>
                  <td className="td hidden whitespace-nowrap text-right tabular-nums md:table-cell">{c.asset_count}</td>
                  <td className="td hidden whitespace-nowrap text-xs text-muted sm:table-cell">
                    {c.secret === "stored" ? "encrypted" : c.secret === "reference" ? <span className="[overflow-wrap:anywhere]" title={c.secret_ref ?? undefined}>secret manager</span> : c.secret === "env" ? <span title="An env:NAME reference set with the CLI">environment</span> : "–"}
                  </td>
                  {admin && (
                    <td className="td hidden whitespace-nowrap lg:table-cell">
                      <div className="flex justify-end gap-1">{actions}</div>
                    </td>
                  )}
                </tr>
              );
            })}
          </tbody>
        </TableCard>
      )}
      {form &&
        (form.editing ? (
          <EditConnection key={sheet} kinds={kinds.data} editing={form.editing} encryption={encryption} onClose={closeForm} />
        ) : (
          <AddConnection kinds={kinds.data} encryption={encryption} first={rows.length === 0} onClose={closeForm} />
        ))}
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
    </>
  );
}
