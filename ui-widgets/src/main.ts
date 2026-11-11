import { App, applyDocumentTheme, applyHostFonts, applyHostStyleVariables } from "@modelcontextprotocol/ext-apps/app-with-deps";
import { chooseChart, compact, filterChips, fmtValue, humanize, isKpi, isPayload, queryLine, sortRows } from "./logic";
import type { Cell, ChartSpec, CtxItem, Format, Payload } from "./logic";

const PAGE = 200; // rows fetched per "Load more"
const SHOWN = 50; // rows rendered before "N more"
const NS = "http://www.w3.org/2000/svg";

const $ = <T extends HTMLElement>(id: string): T => {
  const el = document.getElementById(id);
  if (!el) throw new Error(`missing #${id}`);
  return el as T;
};

interface ToolResultLike { isError?: boolean; structuredContent?: unknown; content?: { type: string; text?: string }[] }

let data: Payload | null = null;
let rows: Cell[][] = [];
let sort: { col: number; dir: "asc" | "desc" } | null = null;
let showAll = false;
let asTable = false;
let rated: "up" | "down" | null = null;
let hostName = "";

const app = new App({ name: "satchel-result", version: "1.0.0" }, {});

function el<K extends keyof HTMLElementTagNameMap>(tag: K, text?: string, cls?: string): HTMLElementTagNameMap[K] {
  const e = document.createElement(tag);
  if (text !== undefined) e.textContent = text;
  if (cls) e.className = cls;
  return e;
}

function renderTable(): void {
  if (!data) return;
  const grid = $<HTMLTableElement>("grid");
  grid.replaceChildren();
  const head = el("tr");
  data.columns.forEach((c, i) => {
    const th = el("th", c.name, c.kind === "number" ? "num" : "");
    th.tabIndex = 0;
    th.setAttribute("role", "columnheader");
    th.setAttribute("aria-sort", sort?.col === i ? (sort.dir === "asc" ? "ascending" : "descending") : "none");
    const toggle = (): void => {
      sort = sort?.col === i && sort.dir === "asc" ? { col: i, dir: "desc" } : { col: i, dir: "asc" };
      renderTable();
    };
    th.addEventListener("click", toggle);
    th.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } });
    head.append(th);
  });
  const thead = el("thead");
  thead.append(head);
  grid.append(thead);
  const ordered = sort ? sortRows(rows, sort.col, sort.dir) : rows;
  const visible = showAll ? ordered : ordered.slice(0, SHOWN);
  const body = el("tbody");
  for (const r of visible) {
    const tr = el("tr");
    data.columns.forEach((c, i) => {
      const v = r[i] ?? null;
      const td = el("td", fmtValue(v, data?.formats?.[c.name]), c.kind === "number" ? "num" : v === null ? "null" : "");
      if (v === null) td.textContent = "∅";
      tr.append(td);
    });
    body.append(tr);
  }
  grid.append(body);

  const loaded = data.offset + rows.length;
  const hiddenLoaded = ordered.length - visible.length;
  const remote = data.row_count - loaded;
  const more = $("more");
  more.hidden = hiddenLoaded <= 0 && remote <= 0;
  const parts: string[] = [];
  if (hiddenLoaded > 0) parts.push(`${hiddenLoaded.toLocaleString()} more loaded`);
  if (remote > 0) parts.push(`${remote.toLocaleString()} more on the server`);
  $("more-text").textContent = parts.join(" · ");
  const btn = $<HTMLButtonElement>("more-btn");
  btn.textContent = hiddenLoaded > 0 ? "Show loaded rows" : "Load more rows";
  btn.hidden = false;
}

function svg<K extends keyof SVGElementTagNameMap>(tag: K, attrs: Record<string, string | number>): SVGElementTagNameMap[K] {
  const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, String(v));
  return e;
}

function axisLabel(n: number, f?: Format): string {
  const c = compact(n);
  if (!f) return c;
  if (f.style === "percent") return `${c}%`;
  if (f.style === "prefix") return `${f.symbol ?? ""}${c}`;
  if (f.style === "currency" && f.currency) {
    try {
      const sym = new Intl.NumberFormat(undefined, { style: "currency", currency: f.currency }).formatToParts(0).find((p) => p.type === "currency")?.value;
      return `${sym ?? ""}${c}`;
    } catch { return c; }
  }
  return c;
}

function renderChart(spec: ChartSpec | null): void {
  const box = $("chart");
  box.replaceChildren();
  box.hidden = spec === null;
  if (!spec) return;
  const W = 640, H = 200, L = 46, R = 10, T = 8, B = 28;
  const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": `${spec.yLabel} by ${spec.xLabel}` });
  const yf = data?.formats?.[spec.yLabel];
  const ys = spec.type === "line" ? spec.points.map((p) => p.y) : spec.bars.map((b) => b.y);
  const ymin = Math.min(0, ...ys), ymax = Math.max(0, ...ys);
  const span = ymax - ymin || 1;
  const py = (y: number): number => T + (H - T - B) * (1 - (y - ymin) / span);
  for (const t of [ymin, ymin + span / 2, ymax]) {
    s.append(svg("line", { class: "ax", x1: L, x2: W - R, y1: py(t), y2: py(t) }));
    const lab = svg("text", { x: L - 4, y: py(t) + 3, "text-anchor": "end" });
    lab.textContent = axisLabel(t, yf);
    s.append(lab);
  }
  if (spec.type === "line") {
    const xs = spec.points.map((p) => p.x);
    const x0 = Math.min(...xs), x1 = Math.max(...xs);
    const px = (x: number): number => L + (W - L - R) * ((x - x0) / (x1 - x0 || 1));
    s.append(svg("polyline", { class: "ln", points: spec.points.map((p) => `${px(p.x)},${py(p.y)}`).join(" ") }));
    if (spec.points.length <= 40) {
      for (const p of spec.points) {
        const d = svg("circle", { class: "dot", cx: px(p.x), cy: py(p.y), r: 2.5 });
        const t = svg("title", {});
        t.textContent = `${p.label}: ${fmtValue(p.y, yf)}`;
        d.append(t);
        s.append(d);
      }
    }
    const first = spec.points[0], last = spec.points[spec.points.length - 1];
    if (first && last) {
      const a = svg("text", { x: L, y: H - 8 });
      a.textContent = first.label;
      const b = svg("text", { x: W - R, y: H - 8, "text-anchor": "end" });
      b.textContent = last.label;
      s.append(a, b);
    }
  } else {
    const slot = (W - L - R) / spec.bars.length;
    const every = Math.ceil(spec.bars.length / 10);
    spec.bars.forEach((b, i) => {
      const x = L + i * slot + slot * 0.15;
      const top = py(Math.max(b.y, 0)), bottom = py(Math.min(b.y, 0));
      const r = svg("rect", { class: "bar", x, y: top, width: slot * 0.7, height: Math.max(1, bottom - top), rx: 2 });
      const t = svg("title", {});
      t.textContent = `${b.label}: ${fmtValue(b.y, yf)}`;
      r.append(t);
      s.append(r);
      if (i % every === 0) {
        const lab = svg("text", { x: x + slot * 0.35, y: H - 8, "text-anchor": "middle" });
        lab.textContent = b.label.length > 12 ? `${b.label.slice(0, 11)}…` : b.label;
        s.append(lab);
      }
    });
  }
  box.append(s);
}

function chip(text: string, cls: string): HTMLSpanElement {
  const c = el("span", undefined, `chip ${cls}`);
  c.append(el("span", text, "tt"));
  c.title = text;
  return c;
}

function renderKpi(): void {
  if (!data) return;
  const box = $("kpi");
  box.replaceChildren();
  const row = rows[0] ?? [];
  const labelIdx = data.columns.findIndex((c) => c.kind !== "number");
  const subs: string[] = [];
  if (labelIdx >= 0 && row[labelIdx] != null) subs.push(`${data.columns[labelIdx]?.name}: ${String(row[labelIdx])}`);
  subs.push(...filterChips(data.metric?.filters).map((c) => c.text));
  data.columns.forEach((c, i) => {
    if (c.kind !== "number") return;
    const tile = el("div", undefined, "tile");
    const big = el("div", fmtValue(row[i] ?? null, data?.formats?.[c.name]) || "\u2205", "big");
    tile.append(big, el("div", humanize(c.name), "lbl"));
    if (subs.length) tile.append(el("div", subs.join(" \u00b7 "), "sub"));
    box.append(tile);
  });
}

function renderCtx(): void {
  if (!data) return;
  const items: CtxItem[] = data.ctx_items ?? data.ctx.map((tag) => {
    const m = /^\[ctx:(.+)@v(\d+)\]$/.exec(tag);
    const key = m?.[1] ?? tag;
    return { tag, key, version: Number(m?.[2] ?? 0), type: "", type_label: "", title: key, url: null };
  });
  const holder = $("ctx");
  holder.replaceChildren();
  $("ctxbox").hidden = items.length === 0;
  $("ctx-summary").textContent = `Context: ${items.length} item${items.length === 1 ? "" : "s"}`;
  for (const it of items) {
    const c = it.url ? el("a", undefined, "chip") : el("span", undefined, "chip");
    if (it.type_label) c.append(el("span", it.type_label, "ty"));
    c.append(el("span", it.title, "tt"));
    c.title = `${it.tag}${it.title !== it.key ? ` \u2013 ${it.key}` : ""}`;
    if (it.url && c instanceof HTMLAnchorElement) {
      const url = it.url;
      c.href = url;
      c.target = "_blank";
      c.rel = "noopener noreferrer";
      // Sandboxed iframes cannot navigate; ask the host to open the page.
      c.addEventListener("click", (e) => { e.preventDefault(); void app.openLink({ url }).catch(() => undefined); });
    }
    holder.append(c);
  }
}

function render(): void {
  if (!data) return;
  $("empty").hidden = true;
  $("app").hidden = false;
  const m = data.metric;
  const title = m?.metrics?.length ? `${m.metrics.join(", ")}${m.dimensions?.length ? ` by ${m.dimensions.join(", ")}` : ""}` : data.question ?? "Query result";
  $("title").textContent = title;
  const kpi = isKpi(data.columns, rows, data.row_count);
  const showKpi = kpi && !asTable;
  const meta = $("meta");
  meta.replaceChildren();
  meta.append(el("span", data.tool === "query_metrics" ? "metric" : "sql", "badge"));
  const n = data.row_count;
  meta.append(document.createTextNode(`${n.toLocaleString()} row${n === 1 ? "" : "s"}${data.truncated ? " (truncated at the row cap)" : ""}${data.connection ? ` \u00b7 ${data.connection}` : ""}`));
  const vb = $<HTMLButtonElement>("view-btn");
  vb.hidden = !kpi;
  vb.textContent = asTable ? "Show as number" : "Show as table";
  const chips = $("chips");
  chips.replaceChildren();
  for (const c of filterChips(m?.filters)) chips.append(chip(c.text, "filter"));
  $("qline").textContent = m ? queryLine(m) : "";
  $("qline").hidden = !m;
  $("rawbox").hidden = !m;
  $("rawq").textContent = m ? JSON.stringify(m, null, 2) : "";
  $("sql").textContent = data.sql;
  renderCtx();
  $("fb-label").textContent = `${kpi ? "Was this number right?" : "Was this result right?"} Your data team sees this.`;
  $("kpi").hidden = !showKpi;
  $("tablewrap").hidden = showKpi;
  if (showKpi) {
    renderKpi();
    renderChart(null);
    $("more").hidden = true;
  } else {
    renderChart(chooseChart(data.columns, rows));
    renderTable();
  }
  $("fb").hidden = data.tool_call_id === null;
}

function accept(res: ToolResultLike, append: boolean): void {
  if (!isPayload(res.structuredContent)) return;
  const p = res.structuredContent;
  if (append && data) {
    rows = rows.concat(p.rows);
    data = { ...data, row_count: p.row_count, truncated: p.truncated };
  } else {
    data = p;
    rows = p.rows;
    sort = null;
    showAll = false;
    asTable = false;
    rated = null;
    setStatus("");
    $("wrong").hidden = true;
    $<HTMLTextAreaElement>("wrong-text").value = "";
    document.querySelectorAll("#reasons .btn").forEach((b) => b.setAttribute("aria-pressed", "false"));
    syncButtons();
  }
  render();
}

async function loadMore(): Promise<void> {
  if (!data) return;
  const btn = $<HTMLButtonElement>("more-btn");
  if (data.offset + rows.length >= data.row_count) return;
  btn.disabled = true;
  try {
    const res = (await app.callServerTool({
      name: "get_result",
      arguments: { session_id: data.session_id, result_id: data.result_id, offset: data.offset + rows.length, limit: PAGE },
    })) as ToolResultLike;
    if (res.isError) throw new Error(res.content?.[0]?.text ?? "get_result failed");
    accept(res, true);
    showAll = true;
    renderTable();
  } catch (e) {
    $("more-text").textContent = e instanceof Error ? e.message : "Could not load more rows";
  } finally {
    btn.disabled = false;
  }
}

function setStatus(text: string, err = false): void {
  const s = $("fb-status");
  s.textContent = text;
  s.className = err ? "fb-status err" : "fb-status";
}

function syncButtons(): void {
  for (const [id, key] of [["fb-up", "up"], ["fb-down", "down"]] as const) {
    const b = $<HTMLButtonElement>(id);
    b.setAttribute("aria-pressed", String(rated === key));
    b.classList.toggle("bad", key !== "up");
  }
}

async function rate(sentiment: "up" | "down", comment?: string, reasons: string[] = []): Promise<void> {
  if (!data || data.tool_call_id === null) return;
  const buttons = Array.from(document.querySelectorAll<HTMLButtonElement>("#fb .btn"));
  buttons.forEach((b) => (b.disabled = true));
  setStatus("Saving…");
  try {
    const args: Record<string, unknown> = {
      session_id: data.session_id,
      tool_call_id: data.tool_call_id,
      result_id: data.result_id,
      sentiment,
      client: hostName || undefined,
    };
    if (reasons.length) args["reasons"] = reasons;
    if (comment) args["comment"] = comment;
    const res = (await app.callServerTool({ name: "rate_result", arguments: args })) as ToolResultLike;
    if (res.isError) throw new Error(res.content?.[0]?.text ?? "rate_result failed");
    rated = sentiment;
    syncButtons();
    setStatus("Thanks \u2014 sent to your data team.");
    // A thumbs down is recorded at once; the panel then lets the user say what was off.
    $("wrong").hidden = sentiment === "up";
  } catch (e) {
    setStatus(e instanceof Error ? e.message : "Could not save feedback", true);
  } finally {
    buttons.forEach((b) => (b.disabled = false));
  }
}

function applyHost(ctx: { theme?: "light" | "dark"; styles?: { variables?: unknown; css?: { fonts?: string } } } | undefined): void {
  if (!ctx) return;
  if (ctx.theme) applyDocumentTheme(ctx.theme);
  if (ctx.styles?.variables) applyHostStyleVariables(ctx.styles.variables as never);
  if (ctx.styles?.css?.fonts) applyHostFonts(ctx.styles.css.fonts);
}

$("fb-up").addEventListener("click", () => void rate("up"));
$("fb-down").addEventListener("click", () => void rate("down"));
document.querySelectorAll<HTMLButtonElement>("#reasons .btn").forEach((b) => {
  b.addEventListener("click", () => b.setAttribute("aria-pressed", String(b.getAttribute("aria-pressed") !== "true")));
});
// No <form>: hosts may sandbox the iframe without allow-forms, which blocks submit events.
$("wrong-send").addEventListener("click", () => {
  const reasons = Array.from(document.querySelectorAll<HTMLButtonElement>("#reasons .btn[aria-pressed='true']"))
    .map((b) => b.dataset["reason"] ?? "").filter(Boolean);
  void rate("down", $<HTMLTextAreaElement>("wrong-text").value.trim() || undefined, reasons);
});
$("view-btn").addEventListener("click", () => { asTable = !asTable; render(); });
$("more-btn").addEventListener("click", () => {
  if (data && rows.length > SHOWN && !showAll) { showAll = true; renderTable(); } else void loadMore();
});

// Handlers first, then connect (the host may push the result right after the handshake).
app.ontoolresult = (params: unknown) => accept(params as ToolResultLike, false);
app.onhostcontextchanged = (params: unknown) => applyHost(params as Parameters<typeof applyHost>[0]);
app.onteardown = async () => ({});

app.connect().then(() => {
  hostName = app.getHostVersion()?.name ?? "";
  applyHost(app.getHostContext() as Parameters<typeof applyHost>[0]);
}).catch((e: unknown) => {
  $("empty").textContent = `Could not connect to the host: ${e instanceof Error ? e.message : String(e)}`;
});

// Test hook for the Playwright harness: nothing else touches window.
(window as unknown as { __satchelWidgetReady?: boolean }).__satchelWidgetReady = true;
