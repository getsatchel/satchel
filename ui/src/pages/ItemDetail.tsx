import { useState, type ReactNode } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { endpoints } from "@/api/endpoints";
import { CodeDiff, Pre } from "@/components/Code";
import { ItemEditor, metaToText, parseMeta, type EditorValue } from "@/components/ItemEditor";
import { Markdown } from "@/components/Markdown";
import { OverlayEditor, orphanedOverlayColumns, sourceBlocks } from "@/components/OverlayEditor";
import { useToast } from "@/components/Toast";
import { Crumb, DescList, DescribedBy, ErrorBox, Feedback, GitBadge, ICONS, Loading, Modal, Notice, PageHeader, PanelSection, SectionHeading, SourceBadge, StateBadge, StatusDot, Tabs, TypeBadge } from "@/components/ui";
import { errMsg, fullTime, isYamlType, timeAgo } from "@/lib/format";
import type { ItemDetail, VersionSummary } from "@/api/types";

export function ItemDetailPage() {
  const params = useParams();
  const key = decodeURIComponent(params["*"] ?? "");
  const q = useQuery({ queryKey: ["item", key], queryFn: () => endpoints.getItem(key) });
  if (q.isPending) return <Loading />;
  if (q.isError) return <ErrorBox error={q.error} />;
  return <ItemView item={q.data} />;
}

function ItemView({ item }: { item: ItemDetail }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [tab, setTab] = useState<"content" | "history">("content");
  const [editing, setEditing] = useState(false);
  const [editingOverlay, setEditingOverlay] = useState(false);
  const [confirmDeprecate, setConfirmDeprecate] = useState(false);
  const git = item.managed_by === "git";
  const synced = item.managed_by === "source";
  const { source, overlay } = sourceBlocks(item);
  const orphaned = synced ? orphanedOverlayColumns(source, overlay) : [];
  const yaml = isYamlType(item.type);
  const editable = !git && !synced && item.state !== "deprecated" && item.state !== "proposed";
  const slash = item.key.lastIndexOf("/");
  const name = slash >= 0 ? item.key.slice(slash + 1) : item.key;
  const folder = slash >= 0 ? item.key.slice(0, slash + 1) : "";

  const deprecate = useMutation({
    mutationFn: () => endpoints.deprecateItem(item.key),
    onSuccess: () => {
      toast("ok", "Marked deprecated. Publish a release to retire it for agents.");
      setConfirmDeprecate(false);
      void qc.invalidateQueries({ queryKey: ["item", item.key] });
      void qc.invalidateQueries({ queryKey: ["items"] });
      void qc.invalidateQueries({ queryKey: ["changes"] });
    },
    onError: (e) => toast("err", errMsg(e)),
  });

  return (
    <>
      <Crumb to="/library" label="Library" rest={folder ? <span className="font-mono">{folder}</span> : undefined} />
      <PageHeader
        title={name}
        sub={
          <span className="flex flex-wrap items-center gap-2">
            <TypeBadge type={item.type} />
            <StateBadge state={item.state} />
            {git && <GitBadge />}
            {synced && <SourceBadge kind={String(source["kind"] ?? "source")} sourceKey={item.source_key} />}
            {item.scope_kind !== "workspace" && <span className="badge">scope {item.scope_kind}</span>}
            <span className="tabular-nums">served {item.usage.served_30d}× in 30 days</span>
            <Feedback up={item.usage.thumbs_up} down={item.usage.thumbs_down} />
          </span>
        }
        actions={
          synced && !editingOverlay && item.state !== "deprecated" ? (
            <button className="btn btn-primary" onClick={() => setEditingOverlay(true)}>
              Edit overlay
            </button>
          ) : editable && !editing ? (
            <>
              <button className="btn" onClick={() => setConfirmDeprecate(true)}>
                Deprecate
              </button>
              <button className="btn btn-primary" onClick={() => setEditing(true)}>
                Edit
              </button>
            </>
          ) : undefined
        }
      />
      {git && (
        <div className="mb-4">
          <Notice>
            This item is managed in the context git repository, so it is read-only here. Edit the file in the repo and run{" "}
            <code>satchel sync import</code> (or let your CI do it); opening pull requests from the UI is planned.
          </Notice>
        </div>
      )}
      {synced && (
        <div className="mb-4 space-y-2">
          <Notice>
            Synced from {source["kind"] ? `${String(source["kind"])} source` : "a source"}
            {source["source"] ? <> <span className="font-mono">{String(source["source"])}</span></> : null}. The source block is read-only and replaced on every sync;
            your overlay is kept. Change the content upstream, or add notes with Edit overlay.
          </Notice>
          {orphaned.length > 0 && (
            <Notice tone="warn">
              Out of sync: the overlay documents column{orphaned.length === 1 ? "" : "s"} <span className="font-mono">{orphaned.join(", ")}</span> that the source no longer provides. The notes are kept but not shown to agents. Move or
              remove them with Edit overlay.
            </Notice>
          )}
        </div>
      )}
      {item.primary === false && (() => {
        const prim = item.also_described_by.find((r) => r.primary);
        return (
          <div className="mb-4 flex flex-wrap items-center gap-2 text-sm">
            <span className="badge badge-warn">not served{prim ? `: ${prim.source} is primary` : ""}</span>
            {prim && (
              <Link className="text-accent hover:underline" to={`/library/item/${prim.key.split("/").map(encodeURIComponent).join("/")}`}>
                Open the primary doc
              </Link>
            )}
          </div>
        );
      })()}
      {item.primary === true && item.also_described_by.length > 0 && (
        <div className="mb-4">
          <DescribedBy refs={item.also_described_by} />
        </div>
      )}
      {item.has_changes && !editing && (
        <div className="mb-4">
          <Notice tone="warn">
            Version v{item.current_seq} is a draft change{item.published_seq != null ? ` over published v${item.published_seq}` : ""}.{" "}
            <Link className="underline" to="/changes">
              Review it in Changes
            </Link>
            .
          </Notice>
        </div>
      )}

      {editingOverlay ? (
        <OverlayEditor item={item} onDone={() => setEditingOverlay(false)} />
      ) : editing ? (
        <Editing item={item} onDone={() => setEditing(false)} />
      ) : (
        <>
          <Tabs
            tabs={[
              { id: "content", label: "Content" },
              { id: "history", label: `History (${item.versions.length})` },
            ]}
            value={tab}
            onChange={setTab}
          />
          {tab === "content" ? (
            <div className="grid items-start gap-x-4 gap-y-0 lg:grid-cols-[minmax(0,1fr)_280px]">
              <div className="min-w-0">
              <SectionHeading icon={ICONS.doc} title="Content" />
              <div className="card min-w-0 overflow-x-auto p-4 sm:p-5">
                {item.body == null ? (
                  <p className="text-sm text-muted">This item has no versions yet. Use Edit to write the first draft.</p>
                ) : yaml ? (
                  <Pre>{item.body}</Pre>
                ) : (
                  <Markdown>{item.body}</Markdown>
                )}
              </div>
              </div>
              <div className="mt-9 min-w-0 lg:sticky lg:top-5 lg:mt-0">
              <SectionHeading icon={ICONS.list} title="Details" />
              <aside className="card text-sm" aria-label="Item details">
                <PanelSection title="Properties">
                  <DescList
                    labelWidth="6rem"
                    rows={[
                      ["Scope", item.scope_kind],
                      ["Version", item.published_seq != null ? <span className="mono">v{item.published_seq}</span> : <span className="text-muted">unpublished</span>],
                      ["Served 30d", <span className="tabular-nums">{item.usage.served_30d}</span>],
                      ["Hash", <code className="break-all text-xs">{item.content_hash?.slice(0, 16) ?? "-"}</code>],
                    ]}
                  />
                </PanelSection>
                {synced && (
                  <PanelSection title="Source (read-only)">
                    <DescList
                      labelWidth="6rem"
                      rows={[
                        ["Source", String(source["source"] ?? "")],
                        ["Tool", String(source["kind"] ?? "")],
                        ["Upstream key", <span className="break-all font-mono text-xs">{item.source_key}</span>],
                        ["Priority", <span className="tabular-nums">{String(source["priority"] ?? 0)}</span>],
                      ]}
                    />
                  </PanelSection>
                )}
                {synced && (
                  <PanelSection title="Analyst overlay">
                    {Object.keys(overlay).length === 0 ? (
                      <p className="text-muted">None yet</p>
                    ) : (
                      <pre className="whitespace-pre-wrap break-words font-mono text-xs leading-5">{JSON.stringify(overlay, null, 2)}</pre>
                    )}
                  </PanelSection>
                )}
                <PanelSection title="Metadata">
                  {Object.keys(item.meta).length === 0 ? (
                    <p className="text-muted">None</p>
                  ) : (
                    <DescList
                      labelWidth="6rem"
                      rows={Object.entries(item.meta)
                        .filter(([k]) => !(synced && (k === "source" || k === "overlay")))
                        .map(([k, v]): [string, ReactNode] => [
                        k,
                        <span className="break-words font-mono text-xs leading-5">{typeof v === "string" ? v : JSON.stringify(v)}</span>,
                      ])}
                    />
                  )}
                </PanelSection>
              </aside>
              </div>
            </div>
          ) : (
            <History item={item} />
          )}
        </>
      )}

      <Modal
        open={confirmDeprecate}
        onClose={() => setConfirmDeprecate(false)}
        title="Deprecate this item?"
        footer={
          <>
            <button className="btn" onClick={() => setConfirmDeprecate(false)}>
              Cancel
            </button>
            <button className="btn btn-danger" disabled={deprecate.isPending} onClick={() => deprecate.mutate()}>
              Deprecate
            </button>
          </>
        }
      >
        <p className="text-sm">
          <span className="font-mono">{item.key}</span> will be removed from the next release. Agents keep the old version until you publish.
        </p>
      </Modal>
    </>
  );
}

function Editing({ item, onDone }: { item: ItemDetail; onDone: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [ev, setEv] = useState<EditorValue>({ body: item.body ?? "", metaText: metaToText(item.meta) });
  const save = useMutation({
    mutationFn: () => {
      const meta = parseMeta(ev.metaText);
      if (!meta.ok) throw new Error(meta.error);
      return endpoints.updateItem(item.key, ev.body, meta.value);
    },
    onSuccess: () => {
      toast("ok", "Saved as a draft. Review and publish it from Changes.");
      void qc.invalidateQueries({ queryKey: ["item", item.key] });
      void qc.invalidateQueries({ queryKey: ["items"] });
      void qc.invalidateQueries({ queryKey: ["changes"] });
      onDone();
    },
    onError: (e) => toast("err", errMsg(e)),
  });
  return (
    <div>
    <SectionHeading icon={ICONS.edit} title="Editing draft" />
    <div className="card p-4 sm:p-5">
      <ItemEditor type={item.type} value={ev} onChange={setEv} height={420} />
      <div className="mt-4 flex flex-wrap items-center justify-end gap-2">
        <span className="mr-auto w-full text-xs text-muted sm:w-auto">Saving creates a new draft version. Nothing is published until you release it.</span>
        <button className="btn" onClick={onDone}>
          Cancel
        </button>
        <button className="btn btn-primary" disabled={save.isPending} onClick={() => save.mutate()}>
          Save draft
        </button>
      </div>
    </div>
    </div>
  );
}

function History({ item }: { item: ItemDetail }) {
  const vs = item.versions;
  const newest = vs[0]?.seq;
  const prev = vs[1]?.seq;
  const [a, setA] = useState<number | undefined>(prev ?? newest);
  const [b, setB] = useState<number | undefined>(newest);
  const va = useQuery({
    queryKey: ["version", item.key, a],
    queryFn: () => endpoints.getVersion(item.key, a as number),
    enabled: a !== undefined,
  });
  const vb = useQuery({
    queryKey: ["version", item.key, b],
    queryFn: () => endpoints.getVersion(item.key, b as number),
    enabled: b !== undefined,
  });

  if (vs.length === 0) return <p className="text-sm text-muted">No versions yet.</p>;
  return (
    <div className="space-y-9">
      <section>
      <SectionHeading icon={ICONS.clock} title="Versions" count={vs.length} />
      <div className="card overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="border-b border-line">
            <tr>
              <th className="th">Version</th>
              <th className="th">Status</th>
              <th className="th hidden sm:table-cell">Source</th>
              <th className="th">Created</th>
              <th className="th hidden md:table-cell">Hash</th>
              <th className="th">Compare</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {vs.map((v) => (
              <tr key={v.seq}>
                <td className="td mono">v{v.seq}</td>
                <td className="td">
                  <VersionBadges v={v} />
                </td>
                <td className="td hidden text-muted sm:table-cell">{String(v.provenance.source ?? "-")}</td>
                <td className="td whitespace-nowrap text-muted" title={fullTime(v.created_at)}>
                  {timeAgo(v.created_at)}
                </td>
                <td className="td mono hidden text-muted md:table-cell">{v.content_hash.slice(0, 10)}</td>
                <td className="td whitespace-nowrap">
                  <label className="mr-3 text-xs">
                    <input type="radio" name="cmp-a" checked={a === v.seq} onChange={() => setA(v.seq)} /> from
                  </label>
                  <label className="text-xs">
                    <input type="radio" name="cmp-b" checked={b === v.seq} onChange={() => setB(v.seq)} /> to
                  </label>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      </section>
      <section>
      <SectionHeading icon={ICONS.layers} title="Compare" />
      {vs.length < 2 ? (
        <p className="text-sm text-muted">Only one version exists, so there is nothing to compare yet.</p>
      ) : va.data && vb.data ? (
        <div>
          <div className="mb-1.5 flex justify-between font-mono text-xs text-muted">
            <span>v{a}</span>
            <span>v{b}</span>
          </div>
          <CodeDiff
            label={`Compare v${a} with v${b}`}
            original={va.data.body}
            modified={vb.data.body}
            language={isYamlType(item.type) ? "yaml" : "markdown"}
          />
        </div>
      ) : (
        <Loading label="Loading versions" />
      )}
      </section>
    </div>
  );
}

function VersionBadges({ v }: { v: VersionSummary }) {
  return (
    <span className="flex flex-wrap items-center gap-2">
      {v.is_published && <StatusDot tone="good">published</StatusDot>}
      {v.is_current && !v.is_published && <StatusDot tone="warn">draft</StatusDot>}
      {v.is_current && v.is_published && <span className="badge">current</span>}
    </span>
  );
}
