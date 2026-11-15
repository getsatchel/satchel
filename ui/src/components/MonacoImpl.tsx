import Editor, { DiffEditor } from "@monaco-editor/react";
import "@/lib/monaco";
import type { Lang } from "./Code";
import { useWide } from "./ui";

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
  label?: string;
}) {
  return (
    <Editor
      value={p.value}
      language={p.language}
      theme={p.dark ? "satchel-dark" : "satchel-light"}
      height={p.height ?? 320}
      onChange={(v) => p.onChange?.(v ?? "")}
      options={{ ...OPTS, readOnly: p.readOnly ?? false, ariaLabel: p.label }}
    />
  );
}

export function DiffImpl(p: {
  original: string;
  modified: string;
  language: Lang;
  height?: number | string;
  dark: boolean;
  label?: string;
}) {
  // On a phone the diff is inline; line numbers would take a quarter of its width.
  const roomy = useWide("(min-width: 640px)");
  return (
    <DiffEditor
      original={p.original}
      modified={p.modified}
      language={p.language}
      theme={p.dark ? "satchel-dark" : "satchel-light"}
      height={p.height ?? 360}
      options={{ ...OPTS, ...(roomy ? {} : { lineNumbers: "off" as const, folding: false, lineDecorationsWidth: 6 }), readOnly: true, originalAriaLabel: p.label && `${p.label}: before`, modifiedAriaLabel: p.label && `${p.label}: after`, renderSideBySide: true, originalEditable: false, diffWordWrap: "on" }}
    />
  );
}
