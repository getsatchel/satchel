// Screenshots of the built widget for every result in test/fixtures/results.json, the way Claude
// shows them: each result's own card (opened) and the answer card with it as the first tab, in
// dark and light. For checking charts, facts and tables by eye after a change.
//
//   npm run build && npm run gallery            # writes test/gallery/*.png (gitignored)
//   npm run gallery -- top_customers            # only the results named
import { mkdirSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const here = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(resolve(here, "../../server/satchel/resources/widgets/result.html"), "utf8");
const results = JSON.parse(readFileSync(resolve(here, "fixtures/results.json"), "utf8"));
const out = resolve(here, "gallery");
mkdirSync(out, { recursive: true });
const only = process.argv.slice(2);

const host = (theme) => `<!doctype html><body style="margin:0;background:${theme === "dark" ? "#1f1f1e" : "#fff"}">
<iframe id="f" sandbox="allow-scripts" style="width:720px;height:1400px;border:0"></iframe>
<script>{
const f = document.getElementById('f');
const send = (m) => f.contentWindow.postMessage({ jsonrpc: '2.0', ...m }, '*');
window.addEventListener('message', (e) => {
  const m = e.data; if (!m || e.source !== f.contentWindow) return;
  if (m.method === 'ui/initialize') {
    send({ id: m.id, result: { protocolVersion: m.params.protocolVersion, hostInfo: { name: 'gallery', version: '1' }, hostCapabilities: { serverTools: {} }, hostContext: { theme: '${theme}' } } });
  } else if (m.method === 'ui/notifications/initialized') {
    send({ method: 'ui/notifications/tool-result', params: { content: [{ type: 'text', text: 'x' }], structuredContent: window.__payload } });
  } else if (m.id !== undefined) {
    send({ id: m.id, result: {} });
  }
});
}</script></body>`;

const answerOf = (r) => ({
  satchel: 1, kind: "answer", session_id: "S1", question: r.title, headline: `Answer for: ${r.title}`,
  summary: null, points: ["A key point from the results."], caveats: [], confidence: "high",
  results: [r], steps: [{ tool: "run_sql", label: "Ran SQL", detail: r.title, error: false, result_id: r.result_id }],
  ctx_items: [],
});

const browser = await chromium.launch({ executablePath: process.env.PW_CHROMIUM || undefined });
const page = await browser.newPage({ viewport: { width: 720, height: 1400 }, deviceScaleFactor: 1 });
page.on("console", (m) => process.env.DEBUG && console.error("console:", m.text()));
page.on("pageerror", (e) => console.error("pageerror:", e.message));
async function shot(payload, theme, file, open) {
  await page.setContent(host(theme));
  await page.evaluate(([p, doc]) => { window.__payload = p; document.getElementById("f").srcdoc = doc; }, [payload, html]);
  const frame = page.frameLocator("#f");
  await frame.locator("#app:not([hidden])").waitFor();
  if (open) await frame.locator("#toggle").click();
  await page.waitForTimeout(150);
  const h = await frame.locator("body").evaluate((b) => b.scrollHeight);
  await page.screenshot({ path: resolve(out, file), clip: { x: 0, y: 0, width: 720, height: Math.min(h, 1400) } });
}
for (const [name, r] of Object.entries(results)) {
  if (only.length && !only.includes(name)) continue;
  for (const theme of ["dark", "light"]) {
    await shot(r, theme, `${name}.step.${theme}.png`, true);
    await shot(answerOf(r), theme, `${name}.answer.${theme}.png`, false);
  }
  console.log(name, r.chart ? `${r.chart.type} ${r.chart.y} by ${r.chart.x.join(" ")}` : "no chart");
}
await browser.close();
console.log(`wrote ${out}`);
