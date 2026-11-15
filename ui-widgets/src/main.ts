import { App, applyDocumentTheme } from "@modelcontextprotocol/ext-apps/app-with-deps";
import { chartMarks, chooseChart, compact, filterChips, fmtValue, humanize, isAnswer, isKpi, isPayload, queryLine, sortRows } from "./logic";
import type { AnswerPayload, Cell, ChartSpec, CtxItem, Format, Payload } from "./logic";

const PAGE = 200; // rows fetched per "Load more"
const SHOWN = 50; // rows rendered before "N more"
const NS = "http://www.w3.org/2000/svg";

const $ = <T extends HTMLElement>(id: string): T => {
  const el = document.getElementById(id);
  if (!el) throw new Error(`missing #${id}`);
  return el as T;
};

interface ToolResultLike { isError?: boolean; structuredContent?: unknown; content?: { type: string; text?: string }[] }

// Two kinds of card. "step": one run_sql / query_metrics / get_result call, collapsed to a line
// until opened, since the answer card shows the results that matter. "answer": report_answer's
// card, with the headline, points, the results as tabs, caveats and the steps taken.
let answer: AnswerPayload | null = null;
let tab = 0;
let expanded = false;
let data: Payload | null = null;
let rows: Cell[][] = [];
let sort: { col: number; dir: "asc" | "desc" } | null = null;
let showAll = false;
let asTable = false;
let rated: "up" | "down" | null = null;
let hostName = "";

const app = new App({ name: "satchel-result", version: "1.1.0" }, {});

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
  // A number column without units shows one number of decimals, so the figures line up.
  const decimals = data.columns.map((c, i) => (c.kind === "number" && !data?.formats?.[c.name] ? colDecimals(rows, i) : null));
  const visible = showAll ? ordered : ordered.slice(0, SHOWN);
  const body = el("tbody");
  for (const r of visible) {
    const tr = el("tr");
    data.columns.forEach((c, i) => {
      const v = r[i] ?? null;
      // A midnight timestamp is a date (month or day buckets): show it as one.
      const dec = decimals[i];
      const shown = c.kind === "time" && typeof v === "string" ? v.replace(/T00:00:00(\.0+)?(Z|\+00:00)?$/, "")
        : dec != null && typeof v === "number" ? v.toLocaleString(undefined, { minimumFractionDigits: dec, maximumFractionDigits: dec })
        : fmtValue(v, data?.formats?.[c.name]);
      const td = el("td", shown, c.kind === "number" ? "num" : v === null ? "null" : "");
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

/** 0 when every loaded value is whole, else 2. */
function colDecimals(rs: Cell[][], i: number): number {
  return rs.some((r) => typeof r[i] === "number" && !Number.isInteger(r[i])) ? 2 : 0;
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
  const W = 640, H = 210, L = 46, R = 12, T = 16, B = 28;
  const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": `${spec.yLabel} by ${spec.xLabel}` });
  const yf = data?.formats?.[spec.yLabel];
  const ann = chartMarks(spec, data?.annotations);
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
  // Where each drawn point sits, for the marks.
  let at: (i: number) => { x: number; y: number };
  if (spec.type === "line") {
    const xs = spec.points.map((p) => p.x);
    const x0 = Math.min(...xs), x1 = Math.max(...xs);
    const px = (x: number): number => L + (W - L - R) * ((x - x0) / (x1 - x0 || 1));
    at = (i) => { const p = spec.points[i]; return p ? { x: px(p.x), y: py(p.y) } : { x: 0, y: 0 }; };
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
    at = (i) => { const b = spec.bars[i]; return b ? { x: L + i * slot + slot / 2, y: py(Math.max(b.y, 0)) } : { x: 0, y: 0 }; };
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
  // The average as a dashed reference line, and the peak / low (or top bar) labelled.
  if (ann.average !== null && ann.average > ymin && ann.average < ymax) {
    const y = py(ann.average);
    s.append(svg("line", { class: "avg", x1: L, x2: W - R, y1: y, y2: y }));
    const t = svg("text", { class: "avg-l", x: W - R, y: y - 4, "text-anchor": "end" });
    t.textContent = `avg ${fmtValue(+ann.average.toFixed(2), yf)}`;
    s.append(t);
  }
  for (const m of ann.marks) {
    const p = at(m.index);
    if (spec.type === "line") s.append(svg("circle", { class: "hit", cx: p.x, cy: p.y, r: 4 }));
    const anchor = p.x < L + 60 ? "start" : p.x > W - R - 60 ? "end" : "middle";
    const below = p.y < T + 14 || (m.label.startsWith("Low") && p.y < H - B - 18);
    const t = svg("text", { class: "hit-l", x: p.x, y: below ? p.y + 16 : p.y - 8, "text-anchor": anchor });
    t.textContent = m.label;
    s.append(t);
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
    const big = el("div", fmtValue(row[i] ?? null, data?.formats?.[c.name]) || "∅", "big");
    tile.append(big, el("div", humanize(c.name), "lbl"));
    if (subs.length) tile.append(el("div", subs.join(" · "), "sub"));
    box.append(tile);
  });
}

function ctxItemsOf(p: Payload): CtxItem[] {
  return p.ctx_items ?? p.ctx.map((tag) => {
    const m = /^\[ctx:(.+)@v(\d+)\]$/.exec(tag);
    const key = m?.[1] ?? tag;
    return { tag, key, version: Number(m?.[2] ?? 0), type: "", type_label: "", title: key, url: null };
  });
}

function renderCtx(items: CtxItem[]): void {
  const holder = $("ctx");
  holder.replaceChildren();
  $("ctxbox").hidden = items.length === 0;
  $("ctx-summary").textContent = `Context: ${items.length} item${items.length === 1 ? "" : "s"}`;
  for (const it of items) {
    const c = it.url ? el("a", undefined, "chip") : el("span", undefined, "chip");
    if (it.type_label) c.append(el("span", it.type_label, "ty"));
    c.append(el("span", it.title, "tt"));
    c.title = `${it.tag}${it.title !== it.key ? ` – ${it.key}` : ""}`;
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

function resultTitle(p: Payload): string {
  const m = p.metric;
  const metricLabel = m?.metrics?.length ? `${m.metrics.join(", ")}${m.dimensions?.length ? ` by ${m.dimensions.join(", ")}` : ""}` : null;
  return p.title || metricLabel || p.question || "Query result";
}

/** The current result: its own card (a step) or the selected tab of the answer card. */
function renderResult(): void {
  $("result").hidden = data === null;
  if (!data) return;
  const step = answer === null;
  const open = !step || expanded;
  const m = data.metric;
  const multi = (answer?.results.length ?? 0) > 1;
  $("title").textContent = resultTitle(data);
  $("title").hidden = multi; // the tab already names it
  const kpi = isKpi(data.columns, rows, data.row_count);
  const showKpi = kpi && !asTable;
  const meta = $("meta");
  meta.replaceChildren();
  meta.append(el("span", data.tool === "query_metrics" ? "metric" : "sql", "badge"));
  const n = data.row_count;
  meta.append(document.createTextNode(`${n.toLocaleString()} row${n === 1 ? "" : "s"}${data.truncated ? " (truncated at the row cap)" : ""}${data.connection ? ` · ${data.connection}` : ""}`));
  const vb = $<HTMLButtonElement>("view-btn");
  vb.hidden = !kpi || !open;
  vb.textContent = asTable ? "Show as number" : "Show as table";
  const tg = $<HTMLButtonElement>("toggle");
  tg.hidden = !step;
  tg.textContent = expanded ? "Hide result" : "Show result";
  tg.setAttribute("aria-expanded", String(expanded));
  const first = data.facts?.[0]?.text;
  $("glance").hidden = open || !first;
  $("glance").textContent = first ?? "";
  $("rbody").hidden = !open;
  const chips = $("chips");
  chips.replaceChildren();
  for (const c of filterChips(m?.filters)) chips.append(chip(c.text, "filter"));
  $("qline").textContent = m ? queryLine(m) : "";
  $("qline").hidden = !m;
  $("rawbox").hidden = !m;
  $("rawq").textContent = m ? JSON.stringify(m, null, 2) : "";
  $("sql").textContent = data.sql;
  const facts = $("facts");
  facts.replaceChildren(...(data.facts ?? []).filter((f) => f.kind !== "no_rows").map((f) => el("li", f.text)));
  $("factbox").hidden = facts.childElementCount === 0;
  const none = data.row_count === 0;
  $("none").hidden = !none;
  $("kpi").hidden = !showKpi;
  $("tablewrap").hidden = showKpi || none;
  if (none) {
    renderChart(null);
    $("more").hidden = true;
  } else if (showKpi) {
    renderKpi();
    renderChart(null);
    $("more").hidden = true;
  } else {
    renderChart(chooseChart(data.columns, rows));
    renderTable();
  }
}

function renderAnswer(): void {
  const a = answer;
  $("answer").hidden = a === null;
  $("caveats").hidden = !a?.caveats.length;
  $("steps").hidden = !a?.steps.length;
  $("tabs").hidden = (a?.results.length ?? 0) < 2;
  const conf = $("conf");
  conf.hidden = !a || a.confidence === "high";
  conf.textContent = a ? `${a.confidence} confidence` : "";
  if (!a) return;
  $("question").textContent = a.question ?? "";
  $("question").hidden = !a.question;
  $("headline").textContent = a.headline;
  $("points").replaceChildren(...a.points.map((p) => el("li", p)));
  $("points").hidden = a.points.length === 0;
  $("caveat-list").replaceChildren(...a.caveats.map((c) => el("li", c)));
  const tabs = $("tabs");
  tabs.replaceChildren();
  a.results.forEach((r, i) => {
    const b = el("button", resultTitle(r), "tab");
    b.type = "button";
    b.id = `tab-${i}`;
    b.setAttribute("role", "tab");
    b.setAttribute("aria-selected", String(i === tab));
    b.setAttribute("aria-controls", "result");
    b.tabIndex = i === tab ? 0 : -1;
    b.title = resultTitle(r);
    b.addEventListener("click", () => selectTab(i));
    b.addEventListener("keydown", (e) => {
      const k = e.key;
      const next = k === "ArrowRight" ? i + 1 : k === "ArrowLeft" ? i - 1 : k === "Home" ? 0 : k === "End" ? a.results.length - 1 : null;
      if (next === null) return;
      e.preventDefault();
      selectTab((next + a.results.length) % a.results.length);
      document.getElementById(`tab-${tab}`)?.focus();
    });
    tabs.append(b);
  });
  if (a.results.length > 1) $("result").setAttribute("aria-labelledby", `tab-${tab}`);
  else $("result").removeAttribute("aria-labelledby");
  const shown = new Map(a.results.map((r, i) => [r.result_id, i]));
  const list = $("step-list");
  list.replaceChildren();
  for (const s of a.steps) {
    const li = el("li", undefined, s.error ? "err" : "");
    const idx = s.result_id ? shown.get(s.result_id) : undefined;
    if (idx !== undefined && a.results.length > 1) {
      const b = el("button", s.label);
      b.type = "button";
      b.addEventListener("click", () => selectTab(idx));
      li.append(b);
    } else li.append(document.createTextNode(s.label));
    if (s.detail) li.append(el("span", ` — ${s.detail}`, "detail"));
    list.append(li);
  }
  $("steps-summary").textContent = `How this was answered (${a.steps.length} step${a.steps.length === 1 ? "" : "s"})`;
}

function render(): void {
  if (!answer && !data) return;
  $("empty").hidden = true;
  $("app").hidden = false;
  const step = answer === null;
  $("kind").textContent = step ? (data?.tool === "query_metrics" ? "Metric query" : "Query") : "Answer";
  renderAnswer();
  renderResult();
  renderCtx(answer ? answer.ctx_items : data ? ctxItemsOf(data) : []);
  $("ctxbox").hidden ||= step && !expanded;
  const kpi = data ? isKpi(data.columns, rows, data.row_count) : false;
  $("fb-label").textContent = `${answer ? "Was this answer right?" : kpi ? "Was this number right?" : "Was this result right?"} Your data team sees this.`;
  $("fb").hidden = !data || data.tool_call_id === null || (step && !expanded);
  $("wrong").hidden ||= $("fb").hidden;
}

function useResult(p: Payload | null): void {
  data = p;
  rows = p?.rows ?? [];
  sort = null;
  showAll = false;
  asTable = false;
}

function resetFeedback(): void {
  rated = null;
  setStatus("");
  $("wrong").hidden = true;
  $<HTMLTextAreaElement>("wrong-text").value = "";
  document.querySelectorAll("#reasons .btn").forEach((b) => b.setAttribute("aria-pressed", "false"));
  syncButtons();
}

function selectTab(i: number): void {
  if (!answer || i === tab) return;
  tab = i;
  useResult(answer.results[i] ?? null);
  resetFeedback();
  render();
}

function accept(res: ToolResultLike, append: boolean): void {
  const p = res.structuredContent;
  if (isAnswer(p)) {
    answer = p;
    tab = 0;
    useResult(p.results[0] ?? null);
    resetFeedback();
  } else if (isPayload(p)) {
    if (append && data) {
      rows = rows.concat(p.rows);
      data = { ...data, row_count: p.row_count, truncated: p.truncated };
    } else {
      answer = null;
      expanded = false;
      useResult(p);
      resetFeedback();
    }
  } else return;
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
    setStatus("Thanks — sent to your data team.");
    // A thumbs down is recorded at once; the panel then lets the user say what was off.
    $("wrong").hidden = sentiment === "up";
  } catch (e) {
    setStatus(e instanceof Error ? e.message : "Could not save feedback", true);
  } finally {
    buttons.forEach((b) => (b.disabled = false));
  }
}

// Satchel's own look (tokens and Geist) on purpose, so the card is familiar from the web UI;
// only light/dark follows the host.
function applyHost(ctx: { theme?: "light" | "dark" } | undefined): void {
  if (ctx?.theme) applyDocumentTheme(ctx.theme);
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
$("toggle").addEventListener("click", () => { expanded = !expanded; render(); });
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
