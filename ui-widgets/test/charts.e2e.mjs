// The chart, facts and table the widget shows for real result shapes (test/fixtures/results.json,
// made by scripts/widget_fixtures.py from the Jaffle Shop data): bars for a ranking even when it
// has date columns, a line only for a time series, no chart for several series, and a table that
// folds away when the chart already shows every column.
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";
import { chromium } from "playwright";

const here = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(resolve(here, "../../server/satchel/resources/widgets/result.html"), "utf8");
const results = JSON.parse(readFileSync(resolve(here, "fixtures/results.json"), "utf8"));

const host = `<!doctype html><body><iframe id="f" sandbox="allow-scripts" style="width:720px;height:1200px"></iframe>
<script>{
const f = document.getElementById('f');
const send = (m) => f.contentWindow.postMessage({ jsonrpc: '2.0', ...m }, '*');
window.addEventListener('message', (e) => {
  const m = e.data; if (!m || e.source !== f.contentWindow) return;
  if (m.method === 'ui/initialize') {
    send({ id: m.id, result: { protocolVersion: m.params.protocolVersion, hostInfo: { name: 'fake-host', version: '1' }, hostCapabilities: { serverTools: {} }, hostContext: { theme: 'dark' } } });
  } else if (m.method === 'ui/notifications/initialized') {
    send({ method: 'ui/notifications/tool-result', params: { content: [{ type: 'text', text: 'x' }], structuredContent: window.__payload } });
  } else if (m.id !== undefined) {
    send({ id: m.id, result: {} });
  }
});
}</script></body>`;

const browser = await chromium.launch({ executablePath: process.env.PW_CHROMIUM || undefined });
const page = await browser.newPage();
page.on("pageerror", (e) => console.error("pageerror:", e.message));

/** Open a result in its own card and return the frame. */
async function open(name) {
  await page.setContent(host);
  await page.evaluate(([p, doc]) => { window.__payload = p; document.getElementById("f").srcdoc = doc; }, [results[name], html]);
  const frame = page.frameLocator("#f");
  await frame.locator("#toggle").click();
  await frame.locator("#rbody").waitFor();
  return frame;
}
const glance = (name) => results[name].facts[0]?.text ?? "";

// A ranking with first/last order dates: bars of spend by name, not a line of customer_id.
let f = await open("top_customers");
assert.equal(await f.locator("#chart svg rect.bar").count(), 10, "one bar per customer");
assert.equal(await f.locator("#chart svg polyline").count(), 0, "no line");
assert.equal(await f.locator("#chart text.axt").textContent(), "lifetime spend aud", "y axis titled");
assert.ok((await f.locator("#chart svg").innerHTML()).includes("Ava Taylor"), "bars labelled by name");
assert.match(glance("top_customers"), /^Ava Taylor has the most lifetime_spend_aud/);
assert.ok(!results.top_customers.facts.some((x) => x.text.includes("customer_id")), "no facts about the id");
assert.equal(await f.locator("#tablewrap").isVisible(), true, "the table has columns the chart doesn't");
assert.equal(await f.locator("#view-btn").isVisible(), false);

// A time series: a line with dates along the axis.
f = await open("monthly_revenue");
assert.equal(await f.locator("#chart svg polyline").count(), 1);
assert.ok(await f.locator("#chart text").filter({ hasText: /^2025-0\d$/ }).count() >= 1, "month ticks between the ends");
// ...and the chart shows every column, so the table folds behind "Show table".
assert.equal(await f.locator("#tablewrap").isVisible(), false);
assert.equal(await f.locator("#view-btn").innerText(), "Show table");
await f.locator("#view-btn").click();
assert.equal(await f.locator("#tablewrap").isVisible(), true);
assert.equal(await f.locator("#view-btn").innerText(), "Hide table");

// Several rows per month (one per payment method): no chart, the table.
f = await open("monthly_by_method");
assert.equal(await f.locator("#chart").isVisible(), false);
assert.equal(await f.locator("#tablewrap").isVisible(), true);

// Identifiers only: bars labelled by the id, facts that name the column.
f = await open("orders_by_customer_id");
assert.equal(await f.locator("#chart svg rect.bar").count(), 10);
assert.match(glance("orders_by_customer_id"), /^customer_id \d+ has the most orders/);

// A list with no measure: no chart.
f = await open("customer_list");
assert.equal(await f.locator("#chart").isVisible(), false);

await browser.close();
console.log("charts e2e: ok");
