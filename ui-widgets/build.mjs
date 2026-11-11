// Bundles src/main.ts + style.css into ONE self-contained HTML file and writes it to
// ../server/satchel/resources/widgets/result.html. `--check` fails if the committed file is stale.
import { readFileSync, writeFileSync, mkdirSync, existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";

const here = dirname(fileURLToPath(import.meta.url));
const out = resolve(here, "../server/satchel/resources/widgets/result.html");

const js = await build({
  entryPoints: [resolve(here, "src/main.ts")],
  bundle: true,
  minify: true,
  format: "iife",
  target: "es2022",
  write: false,
  legalComments: "none",
  logLevel: "error",
  define: { "process.env.NODE_ENV": '"production"' },
});
const code = js.outputFiles[0].text.replace(/<\/script/gi, "<\\/script");
const css = readFileSync(resolve(here, "src/style.css"), "utf8");
const html = readFileSync(resolve(here, "src/index.html"), "utf8")
  .replace("/*__CSS__*/", () => css)
  .replace("/*__JS__*/", () => code);

if (process.argv.includes("--check")) {
  if (!existsSync(out) || readFileSync(out, "utf8") !== html) {
    console.error("result.html is stale: run `npm run build` in ui-widgets/ and commit it.");
    process.exit(1);
  }
  console.log("result.html is up to date");
} else {
  mkdirSync(dirname(out), { recursive: true });
  writeFileSync(out, html);
  console.log(`wrote ${out} (${(html.length / 1024).toFixed(0)} KiB)`);
}
