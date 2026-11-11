import { useState } from "react";
import { CodeEditor } from "./Code";
import { Chevron } from "./ui";
import { isYamlType } from "@/lib/format";

export interface EditorValue {
  body: string;
  /** Raw JSON text of meta; parsed via parseMeta(). */
  metaText: string;
}

export function metaToText(meta: Record<string, unknown> | undefined): string {
  return JSON.stringify(meta ?? {}, null, 2);
}

export function parseMeta(text: string): { ok: true; value: Record<string, unknown> } | { ok: false; error: string } {
  try {
    const v: unknown = JSON.parse(text.trim() || "{}");
    if (v === null || typeof v !== "object" || Array.isArray(v)) {
      return { ok: false, error: "Metadata must be a JSON object." };
    }
    return { ok: true, value: v as Record<string, unknown> };
  } catch (e) {
    return { ok: false, error: `Metadata is not valid JSON: ${(e as Error).message}` };
  }
}

export function ItemEditor(props: {
  type: string;
  value: EditorValue;
  onChange: (v: EditorValue) => void;
  height?: number;
}) {
  const yaml = isYamlType(props.type);
  const [showMeta, setShowMeta] = useState(!yaml || props.value.metaText.trim() !== "{}");
  const parsed = parseMeta(props.value.metaText);
  return (
    <div className="space-y-3">
      <div>
        <div className="label">{yaml ? "Definition (YAML)" : "Body (Markdown)"}</div>
        <CodeEditor
          label={yaml ? "YAML definition editor" : "Markdown body editor"}
          language={yaml ? "yaml" : "markdown"}
          value={props.value.body}
          height={props.height ?? "min(340px, 45vh)"}
          onChange={(body) => props.onChange({ ...props.value, body })}
        />
      </div>
      <div>
        <button className="inline-flex items-center gap-1.5 text-xs font-medium text-muted hover:text-ink" onClick={() => setShowMeta((s) => !s)} aria-expanded={showMeta}>
          <Chevron open={showMeta} />
          Metadata (JSON)
        </button>
        {showMeta && (
          <div className="mt-1.5">
            <CodeEditor
              label="Metadata JSON editor"
              language="json"
              value={props.value.metaText}
              height={130}
              onChange={(metaText) => props.onChange({ ...props.value, metaText })}
            />
            {!parsed.ok && <p className="mt-1 text-xs text-bad">{parsed.error}</p>}
          </div>
        )}
      </div>
    </div>
  );
}
