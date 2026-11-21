import { useQuery } from "@tanstack/react-query";
import { endpoints } from "@/api/endpoints";
import type { SecretPolicy } from "@/api/types";
import { Segmented } from "@/components/ui";

/** A secret field's value that names a secret in a secret manager rather than holding it. */
export const isRef = (v: string | null | undefined) => /^\s*(aws-sm|gcp-sm):/.test(v ?? "");

export type SecretMode = "paste" | "ref";

/** How this deployment treats secrets (admins only: every secret form is theirs). */
export function useSecretPolicy() {
  return useQuery({ queryKey: ["secret-policy"], queryFn: endpoints.secretPolicy, staleTime: Infinity }).data;
}

/** References are usable (the allow list is set). */
export const refsOn = (p: SecretPolicy | undefined) => Boolean(p && p.allow.length > 0);
/** The deployment refuses secret values: only references. */
export const refsOnly = (p: SecretPolicy | undefined) => p?.mode === "references";

/** The mode a field starts in: a reference already set, or the only mode allowed. */
export function initialMode(p: SecretPolicy | undefined, current?: string | null): SecretMode {
  return refsOnly(p) || isRef(current) ? "ref" : "paste";
}

/** "Paste it" or "From a secret manager", shown only when references are allowed and optional. */
export function SecretModeSwitch(props: { policy: SecretPolicy | undefined; mode: SecretMode; onMode: (m: SecretMode) => void; what: string }) {
  if (!refsOn(props.policy) || refsOnly(props.policy)) return null;
  return (
    <Segmented
      label={`Where the ${props.what} comes from`}
      value={props.mode}
      onChange={props.onMode}
      options={[
        { id: "paste", label: `Paste the ${props.what}` },
        { id: "ref", label: "From a secret manager" },
      ]}
    />
  );
}

/** An example reference built from the first allowed pattern. */
function example(p: SecretPolicy | undefined): string {
  const first = p?.allow[0];
  if (!first) return "aws-sm:satchel/warehouse#password";
  return first.replace(/\*+$/, "") + (first.endsWith("*") ? "name" : "");
}

/** A secret manager reference: not a secret itself, so shown in the clear. */
export function SecretRefInput(props: { policy: SecretPolicy | undefined; label: string; value: string; onChange: (v: string) => void; placeholder?: string; disabled?: boolean }) {
  const p = props.policy;
  return (
    <div>
      <label className="block">
        <span className="label">{props.label}</span>
        <input
          className="input mono w-full"
          autoComplete="off"
          spellCheck={false}
          value={props.value}
          disabled={props.disabled || !refsOn(p)}
          onChange={(ev) => props.onChange(ev.target.value)}
          placeholder={props.placeholder ?? example(p)}
        />
      </label>
      <span className="mt-1 block text-xs text-muted">
        {refsOn(p)
          ? "aws-sm:<name or ARN> or gcp-sm:projects/<p>/secrets/<s>; add #field for one field of a JSON secret. Satchel keeps the reference and reads the secret with the server's cloud identity when it connects."
          : "Secret manager references are off: an admin of the deployment sets SATCHEL_SECRET_REF_ALLOW."}
        {refsOn(p) && (
          <>
            {" "}
            Allowed: <span className="font-mono">{p?.allow.join(", ")}</span>
          </>
        )}
      </span>
    </div>
  );
}

/** Help under a plain secret input that also takes a reference (source tokens). */
export function secretHelp(p: SecretPolicy | undefined): string {
  if (refsOnly(p)) return "This deployment stores no secrets: give a secret manager reference (aws-sm:... or gcp-sm:...).";
  if (refsOn(p)) return "Encrypted at rest and never shown again. Or give a secret manager reference (aws-sm:... or gcp-sm:...) and Satchel keeps only that.";
  return "Encrypted at rest and never shown again.";
}

/** Placeholder for a saved secret: the reference it reads, or that one is stored. */
export function savedPlaceholder(saved: unknown, fallback: string): string {
  if (typeof saved === "string" && isRef(saved)) return `Reads ${saved}. Type to replace.`;
  if (saved === "***") return "Stored. Leave empty to keep it.";
  return fallback;
}
