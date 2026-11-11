import { useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { endpoints } from "@/api/endpoints";
import { ItemEditor, metaToText, parseMeta, type EditorValue } from "@/components/ItemEditor";
import { useToast } from "@/components/Toast";
import { CardGrid, Crumb, ICONS, PageHeader, SectionHeading } from "@/components/ui";
import { TEMPLATES, type ItemTemplate } from "@/lib/templates";
import { errMsg } from "@/lib/format";

function initial(t: ItemTemplate): EditorValue {
  return { body: t.body, metaText: metaToText(t.meta) };
}

function prefill(params: URLSearchParams): { tpl: ItemTemplate; ev: EditorValue; key: string } {
  const tpl = TEMPLATES.find((t) => t.type === params.get("type")) ?? TEMPLATES[0]!;
  const asset = params.get("asset");
  if (!asset || !/^asset: .*$/m.test(tpl.body)) return { tpl, ev: initial(tpl), key: tpl.keyPrefix };
  const body = tpl.body.replace(/^asset: .*$/m, `asset: ${asset}`);
  return { tpl, ev: { ...initial(tpl), body }, key: tpl.keyPrefix + asset.split(".").pop()!.toLowerCase() };
}

export function ItemNew() {
  const nav = useNavigate();
  const qc = useQueryClient();
  const toast = useToast();
  const [params] = useSearchParams();
  const [start] = useState(() => prefill(params));
  const [tpl, setTpl] = useState<ItemTemplate>(start.tpl);
  const [ev, setEv] = useState<EditorValue>(start.ev);
  const [key, setKey] = useState(start.key);

  function pick(t: ItemTemplate) {
    setTpl(t);
    setEv(initial(t));
    setKey(t.keyPrefix);
  }

  const create = useMutation({
    mutationFn: () => {
      const meta = parseMeta(ev.metaText);
      if (!meta.ok) throw new Error(meta.error);
      return endpoints.createItem({ key: key.trim(), type: tpl.type, body: ev.body, meta: meta.value, scope_kind: "workspace" });
    },
    onSuccess: (item) => {
      toast("ok", "Created as a draft. Publish it from Changes.");
      void qc.invalidateQueries({ queryKey: ["items"] });
      void qc.invalidateQueries({ queryKey: ["changes"] });
      nav(`/library/item/${item.key.split("/").map(encodeURIComponent).join("/")}`);
    },
    onError: (e) => toast("err", errMsg(e)),
  });

  return (
    <>
      <Crumb to="/library" label="Library" rest="New item" />
      <PageHeader title="New item" sub="Pick a type to start from a template. The item is created as a draft; nothing goes live until you publish." />
      <fieldset className="mb-8">
        <legend className="sr-only">Type</legend>
        <SectionHeading icon={ICONS.layers} title="Type" />
        <CardGrid min="13rem" gap="gap-2">
          {TEMPLATES.map((t) => (
            <label
              key={t.type}
              className={`card cursor-pointer p-3.5 hover:bg-sunken focus-within:outline focus-within:outline-2 focus-within:outline-accent ${tpl.type === t.type ? "border-accent bg-accent-soft/40 shadow-[inset_0_0_0_1px_rgb(var(--accent))]" : ""}`}
            >
              <input type="radio" name="type" className="sr-only" checked={tpl.type === t.type} onChange={() => pick(t)} />
              <div className="text-sm font-medium">{t.label}</div>
              <div className="mt-0.5 text-xs text-muted">{t.hint}</div>
            </label>
          ))}
        </CardGrid>
      </fieldset>
      <SectionHeading icon={ICONS.edit} title="Content" />
      <div className="card p-4 sm:p-5">
        <label className="mb-4 block">
          <span className="label">Key</span>
          <input className="input mono w-full max-w-md" value={key} onChange={(e) => setKey(e.target.value)} placeholder="folder/name" />
          <span className="mt-1 block text-xs text-muted">A stable path such as <span className="font-mono">glossary/active_customer</span>. Agents cite it, so pick it once.</span>
        </label>
        <ItemEditor key={tpl.type} type={tpl.type} value={ev} onChange={setEv} />
        <div className="mt-4 flex flex-wrap justify-end gap-2">
          <Link to="/library" className="btn">
            Cancel
          </Link>
          <button className="btn btn-primary" disabled={create.isPending || !key.trim() || key.trim() === tpl.keyPrefix} onClick={() => create.mutate()}>
            Create draft
          </button>
        </div>
      </div>
    </>
  );
}
