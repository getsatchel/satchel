// Headless check of the built widget: a fake MCP Apps host (parent page) speaks the postMessage
// JSON-RPC protocol to result.html in an iframe, pushes a sample result, and we click through
// the feedback bar asserting the tools/call messages the host would proxy to satchel.
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";
import { chromium } from "playwright";

const here = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(resolve(here, "../../server/satchel/resources/widgets/result.html"), "utf8");

const rows = Array.from({ length: 60 }, (_, i) => [`2025-${String((i % 12) + 1).padStart(2, "0")}-01`, 100 + i * 3]);
const payload = (r, offset, rowCount) => ({
  satchel: 1, tool: "query_metrics", session_id: "S1", result_id: "R1", tool_call_id: 42,
  connection: "jaffle", question: "monthly revenue", sql: "select 1", metric: { metrics: ["revenue"], dimensions: ["order_date:month"], cube: "orders" },
  columns: [{ name: "month", type: "date32[day]", kind: "time" }, { name: "revenue", type: "double", kind: "number" }],
  rows: r, offset, row_count: rowCount, truncated: false, ctx: ["[ctx:metrics/revenue.md@v2]"],
  facts: [{ kind: "change", text: "revenue went from 100 to 277, up 177%." }],
});

const hostPage = `<!doctype html><body><iframe id="f" sandbox="allow-scripts" style="width:700px;height:900px"></iframe>
<script>
window.calls = []; window.seen = [];
const f = document.getElementById('f');
const send = (m) => f.contentWindow.postMessage({ jsonrpc: '2.0', ...m }, '*');
window.addEventListener('message', (e) => {
  const m = e.data; if (!m || e.source !== f.contentWindow) return;
  window.seen.push(m.method);
  if (m.method === 'ui/initialize') {
    send({ id: m.id, result: { protocolVersion: m.params.protocolVersion, hostInfo: { name: 'fake-host', version: '1' }, hostCapabilities: { serverTools: {} }, hostContext: { theme: 'dark' } } });
  } else if (m.method === 'ui/notifications/initialized') {
    send({ method: 'ui/notifications/tool-result', params: { content: [{ type: 'text', text: 'x' }], structuredContent: window.__first } });
  } else if (m.method === 'tools/call') {
    window.calls.push(m.params);
    const sc = m.params.name === 'get_result' ? window.__more : { recorded: true };
    send({ id: m.id, result: { content: [{ type: 'text', text: 'ok' }], structuredContent: sc } });
  }
});
</script></body>`;

const browser = await chromium.launch({ executablePath: process.env.PW_CHROMIUM || undefined });
async function open(first, more) {
  const page = await browser.newPage();
  await page.setContent(hostPage);
  await page.evaluate(([f, m, doc]) => {
    window.__first = f; window.__more = m;
    document.getElementById('f').srcdoc = doc;
  }, [first, more, html]);
  page.on("console", (m) => console.error("console:", m.text()));
  page.on("pageerror", (e) => console.error("pageerror:", e.message));
  return { page, frame: page.frameLocator("#f") };
}
/** A query's own card starts collapsed to one line; open it to see the result. */
async function expand(frame) {
  await frame.locator("#toggle").click();
  await frame.locator("#rbody").waitFor();
}
const { page, frame } = await open(payload(rows.slice(0, 50), 0, 60), payload(rows.slice(50), 50, 60));

// collapsed: the brand row, the title, a fact at a glance; no table, chart or feedback yet
await frame.locator("#toggle").waitFor();
assert.equal(await frame.locator(".brand .word").innerText(), "SATCHEL");
assert.equal(await frame.locator("#kind").innerText(), "METRIC QUERY");
assert.equal(await frame.locator("#toggle").getAttribute("aria-expanded"), "false");
assert.equal(await frame.locator("#glance").innerText(), "revenue went from 100 to 277, up 177%.");
for (const id of ["#rbody", "#fb", "#ctxbox"]) assert.equal(await frame.locator(id).isVisible(), false, `${id} hidden while collapsed`);
await expand(frame);
assert.equal(await frame.locator("#glance").isVisible(), false);
assert.equal(await frame.locator("#factbox li").count(), 1);
await frame.locator("#grid tbody tr").first().waitFor();
assert.equal(await frame.locator("#grid tbody tr").count(), 50, "first 50 rows shown");
assert.ok(await frame.locator("#chart svg polyline").count(), "time column -> line chart");
assert.equal(await frame.locator("html").getAttribute("data-theme"), "dark", "host theme applied");
assert.match(await frame.locator("#ctx-summary").innerText(), /Context: 1 item$/);
assert.match(await frame.locator("#ctx .chip").first().getAttribute("title"), /ctx:metrics\/revenue\.md@v2/);
assert.equal(await frame.locator("#kpi").isVisible(), false, "multi-row result is not a KPI");
assert.match(await frame.locator("#fb-label").innerText(), /Your data team sees this/);

// sorting
await frame.locator("#grid th").nth(1).click();
await frame.locator("#grid th").nth(1).click();
assert.equal((await frame.locator("#grid tbody tr").first().locator("td").nth(1).innerText()).replace(/\D/g, ""), "247");

// paging: shows loaded rows first (none hidden here: 50 loaded of 60) -> server fetch
await frame.locator("#more-btn").click();
await page.waitForFunction(() => window.calls.some((c) => c.name === "get_result"));
const gr = await page.evaluate(() => window.calls.find((c) => c.name === "get_result"));
assert.deepEqual(gr.arguments, { session_id: "S1", result_id: "R1", offset: 50, limit: 200 });
await frame.locator("#grid tbody tr").nth(59).waitFor();

// no separate "This is wrong" button
assert.equal(await frame.locator("#fb-wrong").count(), 0);
// thumbs down records at once, then reveals reasons + comment
await frame.locator("#fb-down").click();
await frame.locator("#fb-status").filter({ hasText: "sent to your data team" }).waitFor();
assert.equal(await frame.locator("#wrong").isVisible(), true, "reasons panel opens after a thumbs down");
assert.equal(await frame.locator("#reasons .btn").count(), 4);
await frame.locator("#reasons [data-reason=wrong_definition]").click();
await frame.locator("#reasons [data-reason=missing_filter]").click();
await frame.locator("#wrong-text").fill("should exclude refunds");
await frame.locator("#wrong-send").click();
await page.waitForFunction(() => window.calls.filter((c) => c.name === "rate_result").length === 2).catch(async (e) => { console.error(await page.evaluate(() => JSON.stringify(window.calls)), await frame.locator("#fb-status").innerText()); throw e; });
const rates = await page.evaluate(() => window.calls.filter((c) => c.name === "rate_result"));
assert.deepEqual(rates[0].arguments, { session_id: "S1", tool_call_id: 42, result_id: "R1", sentiment: "down", client: "fake-host" });
assert.deepEqual(rates[1].arguments, { session_id: "S1", tool_call_id: 42, result_id: "R1", sentiment: "down", client: "fake-host", reasons: ["wrong_definition", "missing_filter"], comment: "should exclude refunds" });
assert.match(await frame.locator("#fb-status").innerText(), /Thanks/);
// switching to thumbs up hides the panel
await frame.locator("#fb-up").click();
await frame.locator("#wrong").waitFor({ state: "hidden" });
assert.equal(await frame.locator("#fb-up").getAttribute("aria-pressed"), "true");

// ---- KPI tile: one row, one number, filters + time range, units, context chips ----
const kpi = {
  satchel: 1, tool: "query_metrics", session_id: "S2", result_id: "R2", tool_call_id: 7,
  connection: "jaffle", question: "revenue in 2025", sql: "SELECT\n  SUM(amount)\nFROM t",
  metric: {
    metrics: ["revenue"], dimensions: [], cube: "sales",
    filters: [
      { dimension: "order_date", operator: "gte", value: "2025-01-01" },
      { dimension: "order_date", operator: "lt", value: "2026-01-01" },
      { dimension: "status", operator: "neq", value: "returned" },
    ],
  },
  formats: { revenue: { style: "currency", currency: "AUD" } },
  columns: [{ name: "revenue", type: "double", kind: "number" }],
  rows: [[22777.05]], offset: 0, row_count: 1, truncated: false,
  ctx: ["[ctx:glossary/revenue@v3]", "[ctx:assets/orders@v1]"],
  ctx_items: [
    { tag: "[ctx:glossary/revenue@v3]", key: "glossary/revenue", version: 3, type: "glossary_term", type_label: "glossary", title: "Revenue", url: "http://localhost:8080/library/item/glossary/revenue" },
    { tag: "[ctx:assets/orders@v1]", key: "assets/orders", version: 1, type: "asset_doc", type_label: "table doc", title: "analytics.orders", url: "http://localhost:8080/library/item/assets/orders" },
  ],
};
const k = await open(kpi, kpi);
const kf = k.frame;
await expand(kf);
await kf.locator("#kpi .tile").first().waitFor();
assert.match(await kf.locator("#kpi .big").innerText(), /\$22,777\.05|A\$22,777\.05|AUD/, "currency formatted");
assert.equal(await kf.locator("#tablewrap").isVisible(), false, "no table in KPI mode");
assert.equal(await kf.locator("#kpi .lbl").textContent(), "revenue");
const chipTexts = await kf.locator("#chips .chip").allInnerTexts();
assert.deepEqual(chipTexts, ["order_date: 2025-01-01 \u2192 2025-12-31", "status \u2260 returned"]);
assert.match(await kf.locator("#kpi .sub").innerText(), /order_date: 2025-01-01/);
assert.equal(await kf.locator("#fb-label").innerText(), "Was this number right? Your data team sees this.");
// query block: collapsed, readable line, raw JSON behind it
assert.equal(await kf.locator("#sqlbox").evaluate((d) => d.open), false, "query block collapsed by default");
await kf.locator("#sql-summary").click();
assert.equal(await kf.locator("#qline").innerText(), "revenue \u00b7 order_date in 2025 \u00b7 status \u2260 returned");
assert.equal(await kf.locator("#rawbox").evaluate((d) => d.open), false);
assert.ok((await kf.locator("#sql").innerText()).includes("SUM(amount)\nFROM t"), "pretty sql kept verbatim");
// context chips: collapsed summary, typed titles, links
assert.equal(await kf.locator("#ctx-summary").innerText(), "Context: 2 items");
await kf.locator("#ctx-summary").click();
assert.deepEqual((await kf.locator("#ctx .chip .ty").allInnerTexts()).map((t) => t.toLowerCase()), ["glossary", "table doc"]);
assert.deepEqual(await kf.locator("#ctx .chip .tt").allInnerTexts(), ["Revenue", "analytics.orders"]);
assert.equal(await kf.locator("#ctx a.chip").first().getAttribute("href"), "http://localhost:8080/library/item/glossary/revenue");
// toggle to the table and back
await kf.locator("#view-btn").click();
await kf.locator("#grid tbody tr").first().waitFor();
assert.match(await kf.locator("#grid tbody td").first().innerText(), /22,777\.05/);
await kf.locator("#view-btn").click();
assert.equal(await kf.locator("#kpi").isVisible(), true);
// thumbs up: reassurance copy, no comment box
await kf.locator("#fb-up").click();
await kf.locator("#fb-status").filter({ hasText: "Thanks \u2014 sent to your data team." }).waitFor();
assert.equal(await kf.locator("#wrong").isVisible(), false);
await k.page.close();

// ---- no units known: plain number, no invented currency; single row with a label stays a KPI ----
const plain = { ...kpi, result_id: "R3", metric: { metrics: ["order_count"], dimensions: [], cube: "sales" }, formats: {},
  columns: [{ name: "status", type: "varchar", kind: "text" }, { name: "order_count", type: "int64", kind: "number" }], rows: [["completed", 1234]], ctx_items: undefined, ctx: [] };
const pl = await open(plain, plain);
await expand(pl.frame);
await pl.frame.locator("#kpi .big").waitFor();
assert.equal(await pl.frame.locator("#kpi .big").innerText(), "1,234");
assert.match(await pl.frame.locator("#kpi .sub").innerText(), /status: completed/);
assert.equal(await pl.frame.locator("#chips .chip").count(), 0);
assert.equal(await pl.frame.locator("#ctxbox").isVisible(), false);
await pl.page.close();

// ---- run_sql: the agent's title heads the card; zero rows says so instead of a bare header ----
const empty = { ...plain, tool: "run_sql", result_id: "R4", metric: null, title: "Signups by month, 2025", question: "how about users?",
  columns: [{ name: "m", type: "date", kind: "time" }, { name: "signups", type: "int64", kind: "number" }], rows: [], row_count: 0 };
const em = await open(empty, empty);
await expand(em.frame);
await em.frame.locator("#none").waitFor();
assert.equal(await em.frame.locator("#title").innerText(), "Signups by month, 2025");
assert.equal(await em.frame.locator("#tablewrap").isVisible(), false);
assert.equal(await em.frame.locator("#chart").isVisible(), false);
await em.page.close();
const untitled = { ...empty, result_id: "R5", title: null };
const un = await open(untitled, untitled);
await expand(un.frame);
await un.frame.locator("#none").waitFor();
assert.equal(await un.frame.locator("#title").innerText(), "how about users?");
await un.page.close();

// ---- answer card: headline, points, results as tabs with facts and marks, caveats, steps ----
const monthly = Array.from({ length: 12 }, (_, i) => [`2025-${String(i + 1).padStart(2, "0")}-01T00:00:00`, [16, 19, 23, 24, 21, 18, 20, 21, 17, 18, 16, 20][i]]);
const buyers = { ...plain, result_id: "RA", tool_call_id: 11, tool: "run_sql", metric: null, title: "Active buyers by month",
  columns: [{ name: "m", type: "timestamp", kind: "time" }, { name: "active_buyers", type: "int64", kind: "number" }],
  rows: monthly, row_count: 12, facts: [{ kind: "range", text: "Peak 24 in 2025-04; low 16 in 2025-01." }],
  annotations: { column: "active_buyers", average: 19.42, marks: [{ x: "2025-04-01", label: "Peak 24" }, { x: "2025-01-01", label: "Low 16" }] } };
const summary = { ...plain, result_id: "RB", tool_call_id: 12, tool: "run_sql", metric: null, title: "Buyer summary",
  columns: [{ name: "buyers", type: "int64", kind: "number" }], rows: [[40]], row_count: 1, facts: [], annotations: null };
const answerCard = {
  satchel: 1, kind: "answer", session_id: "S9", question: "how about users?",
  headline: "Active buyers held between 16 and 24 a month in 2025.", summary: null,
  points: ["April was the peak.", "December is partial."], caveats: ["No signup date in the warehouse."],
  confidence: "medium", results: [buyers, summary], ctx_items: [],
  steps: [
    { tool: "start_analysis", label: "Started the analysis", detail: null, error: false, result_id: null },
    { tool: "run_sql", label: "Ran SQL: Signups", detail: "0 rows", error: false, result_id: "R0" },
    { tool: "run_sql", label: "Ran SQL: Buyer summary", detail: "1 row", error: false, result_id: "RB" },
  ],
};
const an = await open(answerCard, answerCard);
const af = an.frame;
await af.locator("#headline").waitFor();
assert.equal(await af.locator("#kind").innerText(), "ANSWER");
assert.equal(await af.locator("#headline").innerText(), answerCard.headline);
assert.equal(await af.locator("#question").innerText(), "how about users?");
assert.equal(await af.locator("#points li").count(), 2);
assert.equal(await af.locator("#conf").innerText(), "MEDIUM CONFIDENCE");
assert.equal(await af.locator("#toggle").isVisible(), false, "the answer card is never collapsed");
assert.deepEqual(await af.locator("#tabs [role=tab]").allInnerTexts(), ["Active buyers by month", "Buyer summary"]);
assert.equal(await af.locator("#tab-0").getAttribute("aria-selected"), "true");
// the facts' average and marks are drawn on the chart
assert.equal(await af.locator("#chart line.avg").count(), 1);
assert.deepEqual(await af.locator("#chart text.hit-l").allTextContents(), ["Peak 24", "Low 16"]);
assert.equal(await af.locator("#factbox li").innerText(), "Peak 24 in 2025-04; low 16 in 2025-01.");
assert.equal(await af.locator("#caveat-list li").innerText(), "No signup date in the warehouse.");
assert.match(await af.locator("#fb-label").innerText(), /Was this answer right\?/);
// keyboard and the steps list both switch tabs; feedback rates the shown result
await af.locator("#tab-0").focus();
await an.page.keyboard.press("ArrowRight");
await af.locator("#tab-1[aria-selected=true]").waitFor();
await af.locator("#kpi .big").waitFor();
assert.equal(await af.locator("#kpi .big").innerText(), "40");
await af.locator("#fb-up").click();
await an.page.waitForFunction(() => window.calls.some((c) => c.name === "rate_result"));
const ar = await an.page.evaluate(() => window.calls.find((c) => c.name === "rate_result"));
assert.equal(ar.arguments.tool_call_id, 12);
await af.locator("#steps summary").click();
assert.match(await af.locator("#steps-summary").innerText(), /3 steps/);
assert.equal(await af.locator("#step-list li").nth(1).innerText(), "Ran SQL: Signups \u2014 0 rows");
await af.locator("#step-list li").nth(2).locator("button").click();
await af.locator("#tab-1[aria-selected=true]").waitFor();
await an.page.close();

// ---- an answer without data: just the answer, no result, no rating ----
const plainAnswer = { ...answerCard, results: [], steps: [], caveats: [], confidence: "high", points: [] };
const pa = await open(plainAnswer, plainAnswer);
await pa.frame.locator("#headline").waitFor();
for (const id of ["#result", "#tabs", "#fb", "#conf", "#points", "#caveats", "#steps"]) {
  assert.equal(await pa.frame.locator(id).isVisible(), false, `${id} hidden`);
}
await pa.page.close();

// ---- a payload from a newer server: say so instead of waiting for ever ----
const future = { satchel: 1, kind: "something-new", session_id: "S" };
const fu = await open(future, future);
await fu.frame.locator("#empty", { hasText: "newer widget" }).waitFor();
await fu.page.close();

await browser.close();
console.log("widget e2e ok");
