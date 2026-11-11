export function timeAgo(iso: string | null | undefined): string {
  if (!iso) return "-";
  const s = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  if (s < 86400 * 45) return `${Math.round(s / 86400)}d ago`;
  return new Date(iso).toLocaleDateString();
}

/** "in 25m", "in 3h", "in 2d"; "due now" once the time has passed. */
export function timeUntil(iso: string | null | undefined): string {
  if (!iso) return "-";
  const s = Math.round((new Date(iso).getTime() - Date.now()) / 1000);
  if (s < 45) return "due now";
  if (s < 3600) return `in ${Math.round(s / 60)}m`;
  if (s < 86400) return `in ${Math.round(s / 3600)}h`;
  return `in ${Math.round(s / 86400)}d`;
}

export function fullTime(iso: string | null | undefined): string {
  return iso ? new Date(iso).toLocaleString() : "-";
}

export function ms(v: number | null | undefined): string {
  if (v === null || v === undefined) return "-";
  return v >= 1000 ? `${(v / 1000).toFixed(1)}s` : `${Math.round(v)}ms`;
}

export function pretty(v: unknown): string {
  return JSON.stringify(v, null, 2);
}

export function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

export const ITEM_TYPE_LABEL: Record<string, string> = {
  workspace_rule: "Workspace rule",
  guide: "Guide",
  glossary_term: "Glossary term",
  golden_query: "Golden query",
  asset_doc: "Asset doc",
  endorsement: "Endorsement",
  semantic_model: "Semantic model",
  caveat: "Caveat",
  user_memory: "User memory",
};

const YAML_TYPES = new Set(["glossary_term", "golden_query", "asset_doc", "endorsement"]);
export const isYamlType = (type: string): boolean => YAML_TYPES.has(type);
