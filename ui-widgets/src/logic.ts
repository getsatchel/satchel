// Pure helpers (no DOM): payload shape, chart choice, formatting, sorting.

export type Kind = "time" | "number" | "text";
export type Cell = string | number | boolean | null;
export interface Column { name: string; type: string; kind: Kind }
export interface Filter { dimension: string; operator: string; value?: unknown }
export interface MetricQuery {
  metrics?: string[]; dimensions?: string[]; cube?: string;
  filters?: Filter[]; order_by?: { member: string; direction: string }[]; limit?: number | null;
}
export interface Format { style: "currency" | "prefix" | "percent" | "unit" | "number"; currency?: string; symbol?: string; unit?: string; decimals?: number }
export interface CtxItem { tag: string; key: string; version: number; type: string; type_label: string; title: string; url: string | null }
export interface Payload {
  satchel: 1;
  tool: string;
  session_id: string;
  result_id: string;
  tool_call_id: number | null;
  connection: string | null;
  question: string | null;
  sql: string;
  metric: MetricQuery | null;
  formats?: Record<string, Format>;
  columns: Column[];
  rows: Cell[][];
  offset: number;
  row_count: number;
  truncated: boolean;
  ctx: string[];
  ctx_items?: CtxItem[];
}

export function isPayload(v: unknown): v is Payload {
  if (typeof v !== "object" || v === null) return false;
  const o = v as Record<string, unknown>;
  return o["satchel"] === 1 && Array.isArray(o["columns"]) && Array.isArray(o["rows"])
    && typeof o["result_id"] === "string" && typeof o["session_id"] === "string";
}

export type ChartSpec =
  | { type: "line"; xLabel: string; yLabel: string; points: { x: number; y: number; label: string }[] }
  | { type: "bar"; xLabel: string; yLabel: string; bars: { label: string; y: number }[] };

const MAX_BARS = 30;

export function chooseChart(columns: Column[], rows: Cell[][]): ChartSpec | null {
  if (rows.length < 2) return null;
  const timeIdx = columns.findIndex((c) => c.kind === "time");
  const numIdx = columns.findIndex((c, i) => c.kind === "number" && i !== timeIdx);
  if (numIdx < 0) return null;
  const yName = columns[numIdx]?.name ?? "";
  if (timeIdx >= 0) {
    const pts: { x: number; y: number; label: string }[] = [];
    for (const r of rows) {
      const t = r[timeIdx];
      const y = r[numIdx];
      const x = typeof t === "string" ? Date.parse(t) : NaN;
      if (typeof y === "number" && !Number.isNaN(x)) pts.push({ x, y, label: String(t).slice(0, 10) });
    }
    pts.sort((a, b) => a.x - b.x);
    return pts.length >= 2
      ? { type: "line", xLabel: columns[timeIdx]?.name ?? "", yLabel: yName, points: pts }
      : null;
  }
  const textCols = columns.filter((c) => c.kind === "text");
  const numCols = columns.filter((c) => c.kind === "number");
  if (textCols.length !== 1 || numCols.length < 1 || rows.length > MAX_BARS) return null;
  const ti = columns.findIndex((c) => c.kind === "text");
  const bars = rows
    .map((r) => ({ label: String(r[ti] ?? ""), y: r[numIdx] }))
    .filter((b): b is { label: string; y: number } => typeof b.y === "number");
  return bars.length >= 2
    ? { type: "bar", xLabel: columns[ti]?.name ?? "", yLabel: yName, bars }
    : null;
}

export function fmt(v: Cell): string {
  if (v === null) return "";
  if (typeof v === "number") {
    return Number.isInteger(v) ? v.toLocaleString() : v.toLocaleString(undefined, { maximumFractionDigits: 2 });
  }
  return String(v);
}

export function compact(n: number): string {
  const a = Math.abs(n);
  if (a >= 1e9) return `${+(n / 1e9).toFixed(1)}B`;
  if (a >= 1e6) return `${+(n / 1e6).toFixed(1)}M`;
  if (a >= 1e4) return `${+(n / 1e3).toFixed(1)}k`;
  return String(+n.toFixed(2));
}

export function sortRows(rows: Cell[][], col: number, dir: "asc" | "desc"): Cell[][] {
  const sign = dir === "asc" ? 1 : -1;
  return [...rows].sort((a, b) => {
    const x = a[col] ?? null;
    const y = b[col] ?? null;
    if (x === y) return 0;
    if (x === null) return 1; // nulls last in both directions
    if (y === null) return -1;
    if (typeof x === "number" && typeof y === "number") return (x - y) * sign;
    return String(x).localeCompare(String(y), undefined, { numeric: true }) * sign;
  });
}

// ---- number formatting (units only when the server sent them; nothing is invented) ----

export function fmtValue(v: Cell, f?: Format): string {
  if (v === null) return "";
  if (typeof v !== "number" || !f) return fmt(v);
  const dec = f.decimals;
  const digits = dec !== undefined
    ? { minimumFractionDigits: dec, maximumFractionDigits: dec }
    : { maximumFractionDigits: Number.isInteger(v) ? 0 : 2 };
  if (f.style === "currency" && f.currency) {
    try {
      return new Intl.NumberFormat(undefined, { style: "currency", currency: f.currency, ...(dec !== undefined ? digits : {}) }).format(v);
    } catch {
      return `${f.currency} ${v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
    }
  }
  const body = v.toLocaleString(undefined, digits);
  if (f.style === "prefix") return `${f.symbol ?? ""}${body}`;
  if (f.style === "percent") return `${body}%`;
  if (f.style === "unit") return `${body} ${f.unit ?? ""}`.trim();
  return body;
}

// ---- filters / time range ----

const OPS: Record<string, string> = { eq: "=", neq: "\u2260", gt: ">", gte: "\u2265", lt: "<", lte: "\u2264" };
const DATE = /^\d{4}-\d{2}-\d{2}/;

function dayBefore(d: string): string {
  const t = Date.parse(`${d.slice(0, 10)}T00:00:00Z`);
  return Number.isNaN(t) ? d : new Date(t - 86400000).toISOString().slice(0, 10);
}

function valText(v: unknown): string {
  return Array.isArray(v) ? v.map(String).join(", ") : String(v ?? "");
}

function single(f: Filter): string {
  const d = f.dimension;
  switch (f.operator) {
    case "in": return `${d} in (${valText(f.value)})`;
    case "not_in": return `${d} not in (${valText(f.value)})`;
    case "contains": return `${d} contains ${valText(f.value)}`;
    case "starts_with": return `${d} starts with ${valText(f.value)}`;
    case "is_null": return `${d} is empty`;
    case "is_not_null": return `${d} is not empty`;
    default: return `${d} ${OPS[f.operator] ?? f.operator} ${valText(f.value)}`;
  }
}

export interface FilterChip { text: string; short: string; range: boolean }

/** "order_date >= 2025-01-01" + "order_date < 2026-01-01" -> one range chip; others one chip each. */
export function filterChips(filters: Filter[] | undefined): FilterChip[] {
  const fs = filters ?? [];
  const used = new Set<number>();
  const chips: FilterChip[] = [];
  for (const [i, lo] of fs.entries()) {
    if (used.has(i) || lo.operator !== "gte" || typeof lo.value !== "string" || !DATE.test(lo.value)) continue;
    const j = fs.findIndex((f, k) => k !== i && !used.has(k) && f.dimension === lo.dimension
      && (f.operator === "lt" || f.operator === "lte") && typeof f.value === "string" && DATE.test(f.value));
    const hi = fs[j];
    if (j < 0 || !hi || typeof hi.value !== "string") continue;
    used.add(i); used.add(j);
    const start = lo.value.slice(0, 10);
    const end = hi.operator === "lt" ? dayBefore(hi.value) : hi.value.slice(0, 10);
    chips.push({ text: `${lo.dimension}: ${start} \u2192 ${end}`, short: `${lo.dimension} ${rangeShort(start, end)}`, range: true });
  }
  fs.forEach((f, i) => { if (!used.has(i)) chips.push({ text: single(f), short: single(f), range: false }); });
  return chips;
}

function rangeShort(start: string, end: string): string {
  if (start.endsWith("-01-01") && end === `${start.slice(0, 4)}-12-31`) return `in ${start.slice(0, 4)}`;
  const lastDay = new Date(Date.UTC(+start.slice(0, 4), +start.slice(5, 7), 0)).toISOString().slice(0, 10);
  if (start.endsWith("-01") && end === lastDay) return `in ${start.slice(0, 7)}`;
  return `${start} \u2192 ${end}`;
}

/** "revenue \u00b7 by month \u00b7 order_date in 2025" */
export function queryLine(m: MetricQuery): string {
  const parts = [(m.metrics ?? []).join(", ")];
  const dims = (m.dimensions ?? []).map((d) => (d.includes(":") ? d.split(":")[1] ?? d : d));
  if (dims.length) parts.push(`by ${dims.join(", ")}`);
  for (const c of filterChips(m.filters)) parts.push(c.short);
  return parts.filter(Boolean).join(" \u00b7 ");
}

/** A single-value result (one row, 1-3 numbers, at most one label column) renders as KPI tiles. */
export function isKpi(columns: Column[], rows: Cell[][], rowCount: number): boolean {
  if (rows.length !== 1 || rowCount !== 1) return false;
  const nums = columns.filter((c) => c.kind === "number").length;
  return nums >= 1 && nums <= 3 && columns.length - nums <= 1;
}

export function humanize(name: string): string {
  return name.replace(/_/g, " ");
}
