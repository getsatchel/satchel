import { useMemo, useState, type KeyboardEvent, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { endpoints } from "@/api/endpoints";
import type { AssetDetail, AssetNode, ConnectionOut } from "@/api/types";
import {
  Chevron,
  DescList,
  DescribedBy,
  EmptyState,
  ErrorBox,
  Icon,
  ICONS,
  ItemLink,
  ListDetail,
  Loading,
  Meter,
  PageHeader,
  PanelSection,
  SearchInput,
  SectionHeading,
  Segmented,
  StatCell,
  StatGrid,
  StateBadge,
  StatusDot,
} from "@/components/ui";
import { fullTime, timeAgo } from "@/lib/format";

type Filter = "all" | "undocumented" | "endorsed" | "dropped";
type Expand = "default" | "all" | "none";

const KIND_ICON: Record<string, string> = {
  database: "M12 3c4.4 0 8 1.3 8 3s-3.6 3-8 3-8-1.3-8-3 3.6-3 8-3zm8 3v12c0 1.7-3.6 3-8 3s-8-1.3-8-3V6m16 6c0 1.7-3.6 3-8 3s-8-1.3-8-3",
  schema: "M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z",
  table: "M4 5h16v14H4zM4 10h16M10 10v9",
  view: "M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6S2 12 2 12zm10 2.5a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5z",
  column: "M9 4 7 20M17 4l-2 16M4 9h16M3 15h16",
};

function KindIcon({ kind, className = "" }: { kind: string; className?: string }) {
  return <Icon d={KIND_ICON[kind] ?? KIND_ICON.column!} className={className} />;
}

const isDocumentable = (a: AssetNode) => (a.kind === "table" || a.kind === "view") && !a.dropped;

function coverage(nodes: AssetNode[]) {
  const c = { tables: 0, documented: 0, endorsed: 0, excluded: 0, columns: 0 };
  const walk = (n: AssetNode) => {
    if (isDocumentable(n)) {
      c.tables++;
      if (n.has_doc) c.documented++;
      if (n.endorsed) c.endorsed++;
      if (n.excluded_from_ai) c.excluded++;
    }
    if (n.kind === "column" && !n.dropped) c.columns++;
    n.children.forEach(walk);
  };
  nodes.forEach(walk);
  return c;
}

function matches(a: AssetNode, f: Filter) {
  if (f === "undocumented") return isDocumentable(a) && !a.has_doc;
  if (f === "endorsed") return isDocumentable(a) && a.endorsed;
  if (f === "dropped") return a.dropped && a.kind !== "column";
  return true;
}

/** "All" shows what exists now: dropped assets have their own filter. */
function live(nodes: AssetNode[]): AssetNode[] {
  return nodes.filter((n) => !n.dropped).map((n) => ({ ...n, children: live(n.children) }));
}

function countDropped(nodes: AssetNode[]): number {
  return nodes.reduce((k, n) => k + (n.dropped && n.kind !== "column" ? 1 : 0) + countDropped(n.children), 0);
}

function prune(nodes: AssetNode[], f: Filter): AssetNode[] {
  if (f === "all") return live(nodes);
  return nodes.flatMap((n) => {
    if (matches(n, f)) return [n];
    const kids = prune(n.children, f);
    return kids.length ? [{ ...n, children: kids }] : [];
  });
}

function findNode(nodes: AssetNode[], id: string): AssetNode | null {
  for (const n of nodes) {
    if (n.id === id) return n;
    const hit = findNode(n.children, id);
    if (hit) return hit;
  }
  return null;
}

export function Catalog() {
  const [params] = useSearchParams();
  const [conn, setConn] = useState(params.get("connection") ?? "");
  const [text, setText] = useState("");
  const [filter, setFilter] = useState<Filter>("all");
  const [selected, setSelected] = useState<string | null>(null);
  const [expand, setExpand] = useState<Expand>("default");
  const [overrides, setOverrides] = useState<Record<string, boolean>>({});
  const conns = useQuery({ queryKey: ["connections"], queryFn: endpoints.listConnections });
  const assets = useQuery({
    queryKey: ["assets", conn, text],
    queryFn: () => endpoints.listAssets({ connection: conn, q: text }),
    placeholderData: (p) => p,
  });

  const roots = useMemo(() => assets.data?.assets ?? [], [assets.data]);
  const cov = useMemo(() => coverage(roots), [roots]);
  const visible = useMemo(() => prune(roots, filter), [roots, filter]);
  const rolled = useMemo(() => rollups(roots), [roots]);
  const dropped = useMemo(() => countDropped(roots), [roots]);
  const narrowed = filter !== "all" || text.trim() !== "";

  function isOpen(n: AssetNode, depth: number) {
    const o = overrides[n.id];
    if (o !== undefined) return o;
    if (expand === "all") return true;
    if (expand === "none") return false;
    if (narrowed) return n.kind !== "table" && n.kind !== "view";
    return depth <= 1 && n.children.length <= 40;
  }
  const setOpen = (id: string, open: boolean) => setOverrides((m) => ({ ...m, [id]: open }));
  const expandAll = (mode: Expand) => {
    setOverrides({});
    setExpand(mode);
  };

  return (
    <>
      <PageHeader title="Catalog" sub="The tables and columns Satchel has crawled from your connections, and how well each is documented." />

      <section className="mb-9" aria-labelledby="conn-h">
        <SectionHeading
          id="conn-h"
          icon={ICONS.database}
          title="Connections"
          count={conns.data?.length}
          actions={
            <Link className="btn btn-sm" to="/settings?tab=connections">
              Manage
            </Link>
          }
        />
        {conns.isPending && <Loading />}
        {conns.isError && <ErrorBox error={conns.error} />}
        {conns.data && conns.data.length === 0 && (
          <EmptyState
            title="No connections"
            action={
              <Link className="btn btn-primary" to="/settings?tab=connections">
                Add a connection
              </Link>
            }
          >
            Connect a warehouse in Settings, then crawl it to fill the catalog. The CLI works too: <code>satchel connection add</code>.
          </EmptyState>
        )}
        {conns.data && conns.data.length > 0 && (
          <div className="grid grid-cols-[repeat(auto-fill,minmax(14rem,1fr))] gap-3">
            {conns.data.map((c) => (
              <ConnectionCard key={c.id} c={c} active={conn === c.name} onClick={() => setConn(conn === c.name ? "" : c.name)} />
            ))}
          </div>
        )}
      </section>

      <section aria-labelledby="assets-h">
        <SectionHeading
          id="assets-h"
          icon={ICONS.layers}
          title={
            <>
              Assets
              {conn && (
                <span className="normal-case tracking-normal">
                  {" "}
                  in <span className="font-mono text-ink">{conn}</span>{" "}
                  <button className="ml-1 text-accent hover:underline" onClick={() => setConn("")}>
                    show all
                  </button>
                </span>
              )}
            </>
          }
        />
        <div className="mb-3 flex w-full flex-wrap items-center gap-2">
          <Segmented
            label="Show"
            value={filter}
            onChange={setFilter}
            options={[
              { id: "all", label: "All" },
              { id: "undocumented", label: `Undocumented${cov.tables ? ` (${cov.tables - cov.documented})` : ""}` },
              { id: "endorsed", label: "Endorsed" },
              ...(dropped > 0 || filter === "dropped" ? [{ id: "dropped" as const, label: `Dropped (${dropped})` }] : []),
            ]}
          />
          <SearchInput className="w-full sm:ml-auto sm:w-60" placeholder="Filter by table or column" label="Filter assets" value={text} onChange={setText} />
        </div>

        {assets.isPending && <Loading />}
        {assets.isError && <ErrorBox error={assets.error} />}
        {assets.data && roots.length === 0 && !text.trim() && (
          <EmptyState title="No assets crawled yet">Run a crawl on a connection to populate the tree.</EmptyState>
        )}
        {assets.data && (roots.length > 0 || text.trim()) && (
          <>
            <CoverageBar cov={cov} onShowUndocumented={() => setFilter("undocumented")} />
            <ListDetail
              split="xl"
              hasDetail={selected !== null}
              onBack={() => setSelected(null)}
              backLabel="All assets"
              list={
                <div className="card overflow-hidden">
                  <div className="label-caps flex items-center gap-3 border-b border-line bg-sunken/40 px-3 py-2">
                    <span className="flex-1">Name</span>
                    <span className="hidden w-32 sm:block">Type</span>
                    <span className="w-12 text-right sm:w-36">
                      <span className="sm:hidden">Docs</span>
                      <span className="hidden sm:inline">Documentation</span>
                    </span>
                  </div>
                  {visible.length === 0 ? (
                    <p className="px-4 py-8 text-center text-sm text-muted">
                      {text.trim() ? <>Nothing matches “{text.trim()}”.</> : filter === "undocumented" ? "Every table is documented." : "No endorsed assets yet."}
                    </p>
                  ) : (
                    <div role="tree" aria-label="Assets" className="py-1">
                      {visible.map((a) => (
                        <AssetRow key={a.id} a={a} depth={0} selected={selected} onSelect={setSelected} isOpen={isOpen} setOpen={setOpen} rollups={rolled} />
                      ))}
                    </div>
                  )}
                  <div className="flex flex-wrap items-center gap-1 border-t border-line px-2 py-1.5">
                    <button className="btn btn-ghost btn-sm" onClick={() => expandAll("all")}>
                      Expand all
                    </button>
                    <button className="btn btn-ghost btn-sm" onClick={() => expandAll("none")}>
                      Collapse all
                    </button>
                    {assets.data.total > assets.data.assets.length && (
                      <span className="ml-auto pr-1 text-xs text-muted">
                        Showing {assets.data.assets.length} of {assets.data.total} top-level assets
                      </span>
                    )}
                  </div>
                </div>
              }
              detail={
                selected ? (
                  <div className="xl:sticky xl:top-5">
                    <AssetPanel id={selected} node={findNode(roots, selected)} onClose={() => setSelected(null)} />
                  </div>
                ) : (
                  <Legend />
                )
              }
            />
          </>
        )}
      </section>
    </>
  );
}

function ConnectionCard({ c, active, onClick }: { c: ConnectionOut; active: boolean; onClick: () => void }) {
  const meta = c.asset_count > 0 ? `${c.asset_count.toLocaleString()} assets` : "Not crawled yet";
  return (
    <button
      onClick={onClick}
      aria-pressed={active}
      title={meta}
      className={`card flex min-w-0 items-center gap-3 p-3 text-left transition-colors hover:bg-sunken ${active ? "border-accent shadow-[inset_0_0_0_1px_rgb(var(--accent))]" : ""}`}
    >
      <span className="grid h-8 w-8 shrink-0 place-items-center rounded-md bg-accent-soft text-accent">
        <KindIcon kind="database" />
      </span>
      <span className="min-w-0 flex-1">
        <span className="flex min-w-0 items-center gap-1.5">
          <span className="min-w-0 truncate font-mono text-sm font-semibold">{c.name}</span>
          <span className="badge shrink-0">{c.kind}</span>
          {c.default_for_ai && <span className="badge badge-accent shrink-0">default</span>}
        </span>
        <span className={`mt-0.5 block truncate text-xs ${c.asset_count > 0 ? "text-muted" : "text-warn"}`}>{meta}</span>
      </span>
    </button>
  );
}

function CoverageBar({ cov, onShowUndocumented }: { cov: ReturnType<typeof coverage>; onShowUndocumented: () => void }) {
  if (cov.tables === 0) return null;
  const pct = Math.round((cov.documented / cov.tables) * 100);
  const missing = cov.tables - cov.documented;
  return (
    <div className="card mb-4 overflow-hidden">
      <StatGrid>
        <StatCell label="Doc coverage" tone="good" glyph="ring" value={`${pct}%`} secondary={`${cov.documented}/${cov.tables} tables`} info="Share of live tables and views that have a context doc." />
        <StatCell label="Endorsed" tone="good" glyph="square" value={cov.endorsed} info="Tables and views marked as a source of truth." />
        <StatCell label="Columns" tone="muted" glyph="ring" value={cov.columns} info="Live columns in the crawled tables." />
        <StatCell label="Excluded" tone="bad" glyph="square" value={cov.excluded} info="Tables and views hidden from AI agents." />
      </StatGrid>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border-t border-line px-4 py-3 sm:px-5">
        <div className="min-w-0 flex-1 basis-40">
          <Meter value={pct} label="Documentation coverage" tone={pct === 100 ? "good" : "accent"} />
        </div>
        {missing > 0 ? (
          <button className="btn-pill" onClick={onShowUndocumented}>
            {missing} undocumented
          </button>
        ) : (
          <StatusDot tone="good">All tables documented</StatusDot>
        )}
      </div>
    </div>
  );
}

type Rollups = Map<string, ReturnType<typeof coverage>>;

function rollups(nodes: AssetNode[], out: Rollups = new Map()): Rollups {
  for (const n of nodes) {
    if (n.kind === "database" || n.kind === "schema") {
      out.set(n.id, coverage(n.children));
      rollups(n.children, out);
    }
  }
  return out;
}

function DocStatus({ a, rollup }: { a: AssetNode; rollup?: ReturnType<typeof coverage> }) {
  if (a.dropped) return <span className="badge">dropped</span>;
  if (a.kind === "database" || a.kind === "schema") {
    const r = rollup;
    if (!r?.tables) return null;
    const done = r.documented === r.tables;
    return (
      <span className={`text-xs tabular-nums ${done ? "text-muted" : "text-warn"}`} title={`${r.documented} of ${r.tables} tables documented`}>
        {r.documented}/{r.tables}
        <span className="hidden sm:inline"> documented</span>
      </span>
    );
  }
  return (
    <span className="flex items-center justify-end gap-1.5">
      {a.excluded_from_ai && <span className="badge badge-bad">excluded</span>}
      {a.endorsed && isDocumentable(a) && (
        <span className="badge badge-accent hidden sm:inline-flex" title="Endorsed as a source of truth">
          <Icon d={ICONS.check} size={11} strokeWidth={2.6} />
          endorsed
        </span>
      )}
      {isDocumentable(a) &&
        (a.has_doc ? (
          <StatusDot tone="good">
            <span className="sr-only sm:not-sr-only">Documented</span>
          </StatusDot>
        ) : (
          <StatusDot tone="warn">
            <span className="sr-only sm:not-sr-only">Undocumented</span>
          </StatusDot>
        ))}
    </span>
  );
}

function AssetRow(props: {
  a: AssetNode;
  depth: number;
  selected: string | null;
  onSelect: (id: string) => void;
  isOpen: (n: AssetNode, depth: number) => boolean;
  setOpen: (id: string, open: boolean) => void;
  rollups: Rollups;
}) {
  const { a, depth, selected, onSelect, isOpen, setOpen, rollups } = props;
  const hasKids = a.children.length > 0;
  const open = hasKids && isOpen(a, depth);
  const isSel = selected === a.id;
  const isCol = a.kind === "column";

  function onKey(e: KeyboardEvent) {
    if (!hasKids) return;
    if (e.key === "ArrowRight" && !open) setOpen(a.id, true);
    else if (e.key === "ArrowLeft" && open) setOpen(a.id, false);
    else return;
    e.preventDefault();
  }

  return (
    <div role="treeitem" aria-expanded={hasKids ? open : undefined} aria-selected={isSel}>
      <div
        className={`group relative flex h-8 items-center gap-3 pr-3 ${isSel ? "bg-accent-soft/70" : "hover:bg-sunken/70"}`}
        style={{ paddingLeft: 8 + Math.min(depth, 6) * 12 }}
      >
        {isSel && <span className="absolute inset-y-0 left-0 w-0.5 bg-accent" aria-hidden />}
        <span className="flex min-w-0 flex-1 items-center gap-1.5">
          {hasKids ? (
            <button
              aria-label={`${open ? "Collapse" : "Expand"} ${a.name}`}
              onClick={() => setOpen(a.id, !open)}
              className="grid h-5 w-5 shrink-0 place-items-center rounded hover:bg-line/60"
            >
              <Chevron open={open} />
            </button>
          ) : (
            <span className="w-5 shrink-0" />
          )}
          <button className="flex min-w-0 flex-1 items-center gap-2 text-left" onClick={() => onSelect(a.id)} onKeyDown={onKey}>
            <KindIcon kind={a.kind} className={isCol ? "text-muted/70" : "text-muted"} />
            <span
              className={`truncate font-mono text-[13px] ${a.dropped ? "text-muted line-through" : isCol ? "text-ink/85" : "font-medium text-ink"}`}
              title={a.fqn}
            >
              {a.name}
            </span>
            {hasKids && !isCol && <span className="shrink-0 text-xs tabular-nums text-muted/80">{a.children.length}</span>}
          </button>
        </span>
        <span className={`hidden w-32 truncate text-xs sm:block ${a.data_type ? "font-mono text-muted" : "text-muted/80"}`}>
          {a.data_type ?? a.kind}
        </span>
        <span className="flex w-12 shrink-0 justify-end sm:w-36">
          <DocStatus a={a} rollup={rollups.get(a.id)} />
        </span>
      </div>
      {open && (
        <div role="group">
          {a.children.map((c) => (
            <AssetRow key={c.id} {...props} a={c} depth={depth + 1} />
          ))}
        </div>
      )}
    </div>
  );
}

function Legend() {
  const rows: [ReactNode, string][] = [
    [<StatusDot key="d" tone="good">Documented</StatusDot>, "Has a published asset doc agents can read."],
    [<StatusDot key="u" tone="warn">Undocumented</StatusDot>, "Agents only see the raw schema."],
    [<span key="e" className="badge badge-accent">endorsed</span>, "Marked as a source of truth."],
    [<span key="x" className="badge badge-bad">excluded</span>, "Hidden from agents."],
    [<span key="r" className="badge">dropped</span>, "No longer found in the last crawl."],
  ];
  return (
    <aside className="card p-4 text-sm" aria-label="Status legend">
      <p className="text-muted">Select an asset to see its columns and the context that references it.</p>
      <dl className="mt-4 space-y-2.5 border-t border-line pt-4">
        {rows.map(([badge, desc], i) => (
          <div key={i} className="grid grid-cols-[7rem_1fr] items-center gap-2">
            <dt>{badge}</dt>
            <dd className="text-xs text-muted">{desc}</dd>
          </div>
        ))}
      </dl>
    </aside>
  );
}

const RELATION_LABEL: Record<string, string> = {
  defines: "Defines it",
  applies_to: "Caveats that apply",
  uses: "Queries and models that use it",
  mentions: "Mentioned in",
};
const relOrder = (r: string) => {
  const i = ["applies_to", "defines", "uses", "mentions"].indexOf(r);
  return i < 0 ? 99 : i;
};

function AssetPanel({ id, node, onClose }: { id: string; node: AssetNode | null; onClose: () => void }) {
  const q = useQuery({ queryKey: ["asset", id], queryFn: () => endpoints.getAsset(id) });
  if (q.isPending) return <div className="card px-4"><Loading /></div>;
  if (q.isError) return <ErrorBox error={q.error} />;
  const a: AssetDetail = q.data;
  const columns = (node ?? a).children.filter((c) => c.kind === "column");
  const stats = Object.entries(a.stats);
  const linked = a.linked_items.filter((l) => !(l.relation === "defines" && l.key === a.doc_item_key));
  const groups = Object.entries(
    linked.reduce<Record<string, typeof linked>>((m, l) => ((m[l.relation] ??= []).push(l), m), {}),
  ).sort(([x], [y]) => relOrder(x) - relOrder(y));
  return (
    <aside className="card text-sm" aria-label="Asset detail">
      <div className="flex items-start gap-3 p-4">
        <span className="grid h-8 w-8 shrink-0 place-items-center rounded-md bg-sunken text-muted">
          <KindIcon kind={a.kind} />
        </span>
        <div className="min-w-0 flex-1">
          <div className="break-all font-mono text-base font-semibold leading-tight">{a.name}</div>
          <div className="mt-0.5 break-all font-mono text-xs text-muted">{a.fqn}</div>
          <div className="mt-2 flex flex-wrap items-center gap-1.5">
            <span className="badge">{a.data_type ?? a.kind}</span>
            {isDocumentable(a) && <StatusDot tone={a.has_doc ? "good" : "warn"}>{a.has_doc ? "Documented" : "Undocumented"}</StatusDot>}
            {a.endorsed && <span className="badge badge-accent">endorsed</span>}
            {a.excluded_from_ai && <span className="badge badge-bad">excluded from AI</span>}
            {a.dropped && <span className="badge">dropped</span>}
          </div>
        </div>
        <button className="btn btn-ghost btn-sm -mr-1 -mt-1 hidden px-1.5 xl:inline-flex" onClick={onClose} aria-label="Close detail">
          <Icon d={ICONS.close} size={14} strokeWidth={2} />
        </button>
      </div>

      {isDocumentable(a) && (
        <PanelSection title="Documentation">
          {a.doc_item_key ? (
            <div className="space-y-1">
              <ItemLink itemKey={a.doc_item_key} />
              {a.primary && (
                <p className="text-xs text-muted">
                  Primary: <span className="font-medium text-ink">{a.primary.source}</span>
                  {a.primary.key !== a.doc_item_key && (
                    <>
                      {" "}
                      <ItemLink itemKey={a.primary.key} />
                    </>
                  )}
                </p>
              )}
              <DescribedBy refs={a.also_described_by} />
            </div>
          ) : (
            <div className="flex items-center justify-between gap-2">
              <span className="text-muted">Agents only see the raw schema.</span>
              <Link to={`/library/new?type=asset_doc&asset=${encodeURIComponent(a.fqn)}`} className="btn btn-primary btn-sm">
                Document
              </Link>
            </div>
          )}
        </PanelSection>
      )}

      {columns.length > 0 && (
        <PanelSection title={`Columns (${columns.length})`}>
          <ul className="-mx-1 max-h-72 overflow-auto">
            {columns.map((c) => (
              <li key={c.id} className="flex items-center gap-2 rounded px-1 py-1 hover:bg-sunken/70">
                <span className={`min-w-0 flex-1 truncate font-mono text-[13px] ${c.dropped ? "text-muted line-through" : ""}`}>{c.name}</span>
                {c.excluded_from_ai && <span className="badge badge-bad">excluded</span>}
                <span className="shrink-0 font-mono text-xs text-muted">{c.data_type}</span>
              </li>
            ))}
          </ul>
        </PanelSection>
      )}

      <PanelSection title={`Referenced by${linked.length ? ` (${linked.length})` : ""}`}>
        {linked.length === 0 ? (
          <p className="text-muted">No other context references this asset yet.</p>
        ) : (
          <div className="space-y-3">
            {groups.map(([relation, items]) => (
              <div key={relation}>
                <div className="mb-1 text-xs text-muted">{RELATION_LABEL[relation] ?? relation}</div>
                <ul className="space-y-1">
                  {items.map((l) => (
                    <li key={`${l.key}:${l.relation}`} className="flex min-w-0 items-center gap-2">
                      <span className="min-w-0 flex-1 truncate" title={l.key}>
                        <ItemLink itemKey={l.key} />
                      </span>
                      {l.state !== "published" && <StateBadge state={l.state} />}
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        )}
      </PanelSection>

      <PanelSection title="Details">
        <DescList
          labelWidth="6.5rem"
          rows={[
            ["Connection", a.connection && <span className="font-mono">{a.connection}</span>],
            ["First seen", <span title={fullTime(a.first_seen_at)}>{timeAgo(a.first_seen_at)}</span>],
            ["Last crawled", <span title={fullTime(a.last_seen_at)}>{timeAgo(a.last_seen_at)}</span>],
            ...stats.map(([k, v]): [string, ReactNode] => [
              k,
              <span className="break-all font-mono text-xs leading-5">{typeof v === "object" ? JSON.stringify(v) : String(v)}</span>,
            ]),
          ]}
        />
      </PanelSection>
    </aside>
  );
}
