import { lazy, Suspense } from "react";
import { Loading } from "./ui";
import { useTheme } from "@/lib/theme";

const MonacoEditor = lazy(() => import("./MonacoImpl").then((m) => ({ default: m.EditorImpl })));
const MonacoDiff = lazy(() => import("./MonacoImpl").then((m) => ({ default: m.DiffImpl })));

export type Lang = "markdown" | "yaml" | "sql" | "json" | "plaintext";

export function CodeEditor(props: {
  value: string;
  onChange?: (v: string) => void;
  language: Lang;
  height?: number | string;
  readOnly?: boolean;
  label: string;
}) {
  const [theme] = useTheme();
  return (
    <div className="overflow-hidden rounded border border-line" aria-label={props.label}>
      <Suspense fallback={<Loading label="Loading editor" />}>
        <MonacoEditor {...props} dark={theme === "dark"} />
      </Suspense>
    </div>
  );
}

export function CodeDiff(props: {
  original: string;
  modified: string;
  language: Lang;
  height?: number | string;
  label: string;
}) {
  const [theme] = useTheme();
  return (
    <div className="overflow-hidden rounded border border-line" aria-label={props.label}>
      <Suspense fallback={<Loading label="Loading diff" />}>
        <MonacoDiff {...props} dark={theme === "dark"} />
      </Suspense>
    </div>
  );
}

/** Syntax-neutral, cheap pretty printer for JSON / SQL blocks (no Monaco). */
export function Pre({ children }: { children: string }) {
  return (
    <pre className="max-h-72 overflow-auto rounded border border-line bg-sunken p-2.5 text-xs leading-relaxed">{children}</pre>
  );
}
