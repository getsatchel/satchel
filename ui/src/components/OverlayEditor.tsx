import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { endpoints } from "@/api/endpoints";
import type { ItemDetail } from "@/api/types";
import { useToast } from "@/components/Toast";
import { errMsg } from "@/lib/format";

type Rec = Record<string, unknown>;

const isRec = (v: unknown): v is Rec => v !== null && typeof v === "object" && !Array.isArray(v);

/** The `source` and `overlay` blocks every source-managed version keeps in its meta. */
export function sourceBlocks(item: ItemDetail): { source: Rec; overlay: Rec } {
  const s = item.meta["source"];
  const o = item.meta["overlay"];
  return { source: isRec(s) ? s : {}, overlay: isRec(o) ? o : {} };
}

/** Column names of the source asset (asset docs only). */
export function sourceColumns(source: Rec): string[] {
  const asset = source["asset"];
  const cols = isRec(asset) ? asset["columns"] : undefined;
  return Array.isArray(cols) ? cols.filter(isRec).map((c) => String(c["name"])) : [];
}

/** Overlay columns the upstream source no longer has: the sync keeps them, a curator should tidy. */
export function orphanedOverlayColumns(source: Rec, overlay: Rec): string[] {
  const have = new Set(sourceColumns(source).map((c) => c.toLowerCase()));
  const cols = overlay["columns"];
  if (!isRec(cols) || have.size === 0) return [];
  return Object.keys(cols).filter((c) => !have.has(c.toLowerCase()));
}

interface ColRow {
  name: string;
  notes: string;
}

function initialRows(overlay: Rec): ColRow[] {
  const cols = overlay["columns"];
  if (!isRec(cols)) return [];
  return Object.entries(cols).map(([name, v]) => ({ name, notes: isRec(v) ? String(v["notes"] ?? "") : "" }));
}

export function OverlayEditor({ item, onDone }: { item: ItemDetail; onDone: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const { source, overlay } = sourceBlocks(item);
  const asset = item.type === "asset_doc";
  const known = sourceColumns(source);
  const [description, setDescription] = useState(String(overlay["description"] ?? ""));
  const [notes, setNotes] = useState(String(overlay["notes"] ?? ""));
  const [caveats, setCaveats] = useState(Array.isArray(overlay["caveats"]) ? (overlay["caveats"] as string[]).join("\n") : "");
  const [endorsed, setEndorsed] = useState(typeof overlay["endorsed"] === "boolean" ? String(overlay["endorsed"]) : "");
  const [rows, setRows] = useState<ColRow[]>(initialRows(overlay));

  const save = useMutation({
    mutationFn: () => {
      const out: Rec = {};
      if (notes.trim()) out["notes"] = notes.trim();
      if (asset) {
        if (description.trim()) out["description"] = description.trim();
        const cv = caveats.split("\n").map((l) => l.trim()).filter(Boolean);
        if (cv.length) out["caveats"] = cv;
        if (endorsed !== "") out["endorsed"] = endorsed === "true";
        const cols = rows.filter((r) => r.name.trim() && r.notes.trim());
        if (cols.length) out["columns"] = Object.fromEntries(cols.map((r) => [r.name.trim(), { notes: r.notes.trim() }]));
      }
      return endpoints.updateOverlay(item.key, out);
    },
    onSuccess: () => {
      toast("ok", "Overlay saved as a draft. Review and publish it from Changes.");
      void qc.invalidateQueries({ queryKey: ["item", item.key] });
      void qc.invalidateQueries({ queryKey: ["items"] });
      void qc.invalidateQueries({ queryKey: ["changes"] });
      onDone();
    },
    onError: (e) => toast("err", errMsg(e)),
  });

  return (
    <div className="card space-y-4 p-4">
      <p className="text-sm text-muted">
        Your additions sit on top of what the source provides. The source text is always shown to agents, and a sync never overwrites the overlay.
      </p>
      {asset && (
        <label className="block">
          <span className="label">Lead description (optional; the source description is still shown after it)</span>
          <textarea className="input w-full" rows={2} value={description} onChange={(e) => setDescription(e.target.value)} />
        </label>
      )}
      <label className="block">
        <span className="label">Notes for agents</span>
        <textarea className="input w-full" rows={3} value={notes} onChange={(e) => setNotes(e.target.value)} placeholder="Context the source cannot know: how finance uses this, gotchas, who to ask." />
      </label>
      {asset && (
        <>
          <label className="block">
            <span className="label">Caveats (one per line)</span>
            <textarea className="input w-full" rows={2} value={caveats} onChange={(e) => setCaveats(e.target.value)} />
          </label>
          <label className="block sm:w-64">
            <span className="label">Endorsement</span>
            <select className="input w-full" value={endorsed} onChange={(e) => setEndorsed(e.target.value)}>
              <option value="">Follow the source</option>
              <option value="true">Endorsed: preferred table</option>
              <option value="false">Not endorsed</option>
            </select>
          </label>
          <div>
            <div className="label">Column notes</div>
            <ul className="space-y-2">
              {rows.map((r, i) => (
                <li key={i} className="flex flex-wrap items-start gap-2">
                  <input
                    className="input w-full font-mono sm:w-48"
                    list={`cols-${item.key}`}
                    aria-label="Column name"
                    value={r.name}
                    placeholder="column"
                    onChange={(e) => setRows(rows.map((x, j) => (j === i ? { ...x, name: e.target.value } : x)))}
                  />
                  <input
                    className="input min-w-0 flex-1"
                    aria-label={`Notes for ${r.name || "column"}`}
                    value={r.notes}
                    placeholder="note"
                    onChange={(e) => setRows(rows.map((x, j) => (j === i ? { ...x, notes: e.target.value } : x)))}
                  />
                  <button className="btn btn-sm btn-ghost" onClick={() => setRows(rows.filter((_, j) => j !== i))} aria-label="Remove column note">
                    Remove
                  </button>
                </li>
              ))}
            </ul>
            <datalist id={`cols-${item.key}`}>
              {known.map((c) => (
                <option key={c} value={c} />
              ))}
            </datalist>
            <button className="btn btn-sm mt-2" onClick={() => setRows([...rows, { name: "", notes: "" }])}>
              Add column note
            </button>
          </div>
        </>
      )}
      <div className="flex flex-wrap items-center justify-end gap-2">
        <span className="mr-auto w-full text-xs text-muted sm:w-auto">Saving creates a draft version. Nothing is published until you release it.</span>
        <button className="btn" onClick={onDone}>
          Cancel
        </button>
        <button className="btn btn-primary" disabled={save.isPending} onClick={() => save.mutate()}>
          Save overlay
        </button>
      </div>
    </div>
  );
}
