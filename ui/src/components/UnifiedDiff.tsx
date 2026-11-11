/** Renders the API's unified diff text with +/- colouring. */
export function UnifiedDiff({ text, empty = "No textual change." }: { text: string; empty?: string }) {
  if (!text.trim()) return <p className="text-sm text-muted">{empty}</p>;
  return (
    <pre className="max-h-80 overflow-auto rounded border border-line bg-surface text-xs leading-5">
      {text.split("\n").map((ln, i) => {
        const cls =
          ln.startsWith("+++") || ln.startsWith("---")
            ? "text-muted"
            : ln.startsWith("+")
              ? "bg-good-soft text-good"
              : ln.startsWith("-")
                ? "bg-bad-soft text-bad"
                : ln.startsWith("@@")
                  ? "text-accent"
                  : "";
        return (
          <div key={i} className={`whitespace-pre-wrap px-2.5 ${cls}`}>
            {ln || " "}
          </div>
        );
      })}
    </pre>
  );
}

export function DiffStat({ add, del }: { add: number; del: number }) {
  return (
    <span className="mono text-xs tabular-nums">
      <span className="text-good">+{add}</span> <span className="text-bad">-{del}</span>
    </span>
  );
}
