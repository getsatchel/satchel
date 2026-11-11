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
const { page, frame } = await open(payload(rows.slice(0, 50), 0, 60), payload(rows.slice(50), 50, 60));

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
await kf.locator("#kpi .tile").first().waitFor();
assert.match(await kf.locator("#kpi .big").innerText(), /\$22,777\.05|A\$22,777\.05|AUD/, "currency formatted");
assert.equal(await kf.locator("#tablewrap").isVisible(), false, "no table in KPI mode");
assert.equal(await kf.locator("#kpi .lbl").innerText(), "revenue");
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
await pl.frame.locator("#kpi .big").waitFor();
assert.equal(await pl.frame.locator("#kpi .big").innerText(), "1,234");
assert.match(await pl.frame.locator("#kpi .sub").innerText(), /status: completed/);
assert.equal(await pl.frame.locator("#chips .chip").count(), 0);
assert.equal(await pl.frame.locator("#ctxbox").isVisible(), false);
await pl.page.close();

await browser.close();
console.log("widget e2e ok");
