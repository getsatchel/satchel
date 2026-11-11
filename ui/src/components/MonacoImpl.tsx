import Editor, { DiffEditor } from "@monaco-editor/react";
import "@/lib/monaco";
import type { Lang } from "./Code";

const OPTS = {
  minimap: { enabled: false },
  fontSize: 13,
  lineNumbers: "on" as const,
  scrollBeyondLastLine: false,
  wordWrap: "on" as const,
  renderLineHighlight: "none" as const,
  automaticLayout: true,
  padding: { top: 8, bottom: 8 },
  fontFamily: "ui-monospace, SF Mono, Menlo, Consolas, monospace",
};

export function EditorImpl(p: {
  value: string;
  onChange?: (v: string) => void;
  language: Lang;
  height?: number | string;
  readOnly?: boolean;
  dark: boolean;
}) {
  return (
    <Editor
      value={p.value}
      language={p.language}
      theme={p.dark ? "satchel-dark" : "satchel-light"}
      height={p.height ?? 320}
      onChange={(v) => p.onChange?.(v ?? "")}
      options={{ ...OPTS, readOnly: p.readOnly ?? false }}
    />
  );
}

export function DiffImpl(p: {
  original: string;
  modified: string;
  language: Lang;
  height?: number | string;
  dark: boolean;
}) {
  return (
    <DiffEditor
      original={p.original}
      modified={p.modified}
      language={p.language}
      theme={p.dark ? "satchel-dark" : "satchel-light"}
      height={p.height ?? 360}
      options={{ ...OPTS, readOnly: true, renderSideBySide: true, originalEditable: false, diffWordWrap: "on" }}
    />
  );
}
