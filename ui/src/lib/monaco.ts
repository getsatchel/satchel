/** Bundled Monaco (no CDN): only the editor core plus the languages satchel needs. */
import { loader } from "@monaco-editor/react";
import * as monaco from "monaco-editor/esm/vs/editor/editor.api";
import "monaco-editor/esm/vs/basic-languages/markdown/markdown.contribution";
import "monaco-editor/esm/vs/basic-languages/yaml/yaml.contribution";
import "monaco-editor/esm/vs/basic-languages/sql/sql.contribution";
import "monaco-editor/esm/vs/language/json/monaco.contribution";
import EditorWorker from "monaco-editor/esm/vs/editor/editor.worker?worker";
import JsonWorker from "monaco-editor/esm/vs/language/json/json.worker?worker";

self.MonacoEnvironment = {
  getWorker(_id: string, label: string): Worker {
    return label === "json" ? new JsonWorker() : new EditorWorker();
  },
};

monaco.editor.defineTheme("satchel-light", {
  base: "vs",
  inherit: true,
  rules: [],
  colors: {
    "editor.background": "#ffffff",
    "editorGutter.background": "#ffffff",
    "diffEditor.insertedTextBackground": "#1f7d4c22",
    "diffEditor.removedTextBackground": "#be322622",
    "diffEditor.insertedLineBackground": "#1f7d4c12",
    "diffEditor.removedLineBackground": "#be322612",
  },
});
monaco.editor.defineTheme("satchel-dark", {
  base: "vs-dark",
  inherit: true,
  rules: [],
  colors: {
    "editor.background": "#1f1d1b",
    "editorGutter.background": "#1f1d1b",
    "diffEditor.insertedTextBackground": "#6ac48a30",
    "diffEditor.removedTextBackground": "#f0807030",
    "diffEditor.insertedLineBackground": "#6ac48a14",
    "diffEditor.removedLineBackground": "#f0807014",
  },
});

loader.config({ monaco });
export { monaco };
