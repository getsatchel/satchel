import { useToast } from "@/components/Toast";

/** A read-only value with a Copy button (a URL, a command, a secret shown once). */
export function CopyField({ label, value, secret }: { label: string; value: string; secret?: boolean }) {
  const toast = useToast();
  return (
    <div>
      <span className="label">{label}</span>
      <div className="flex min-w-0 items-center gap-2">
        <code className={`min-w-0 flex-1 rounded-md border border-line bg-sunken px-2 py-1.5 text-xs whitespace-pre-wrap [overflow-wrap:anywhere] ${secret ? "select-all" : ""}`}>{value}</code>
        <button
          type="button"
          className="btn btn-sm shrink-0"
          onClick={() =>
            void navigator.clipboard.writeText(value).then(
              () => toast("ok", `${label} copied`),
              () => toast("err", "Copy failed: select the text and copy it"),
            )
          }
        >
          Copy
        </button>
      </div>
    </div>
  );
}
