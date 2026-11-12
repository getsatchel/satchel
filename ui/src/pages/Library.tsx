import { useEffect, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { endpoints } from "@/api/endpoints";
import type { ItemSummary } from "@/api/types";
import { EmptyState, ErrorBox, FilterSelect, GitBadge, ICONS, KeyLines, Loading, PageHeader, SearchInput, SectionHeading, SourceBadge, StateBadge, TableCard, TypeBadge } from "@/components/ui";
import { ITEM_TYPE_LABEL, timeAgo } from "@/lib/format";

const STATES = ["draft", "published", "proposed", "deprecated"];
const SCOPES = ["workspace", "domain", "connection", "asset"];

export function Library() {
  const [sp, setSp] = useSearchParams();
  const f = {
    type: sp.get("type") ?? "",
    state: sp.get("state") ?? "",
    scope: sp.get("scope") ?? "",
    managed_by: sp.get("managed_by") ?? "",
    q: sp.get("q") ?? "",
  };
  const [text, setText] = useState(f.q);
  useEffect(() => {
    const t = setTimeout(() => set("q", text), 250);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [text]);

  function set(k: string, v: string) {
    setSp(
      (prev) => {
        const n = new URLSearchParams(prev);
        if (v) n.set(k, v);
        else n.delete(k);
        return n;
      },
      { replace: true },
    );
  }

  const q = useQuery({
    queryKey: ["items", f],
    queryFn: () => endpoints.listItems(f),
    placeholderData: (p) => p,
  });
  const filtered = Object.values(f).some(Boolean);

  return (
    <>
      <PageHeader
        title="Library"
        sub="Everything agents can be served: guides, glossary, golden queries, asset docs and more."
        actions={
          <Link to="/library/new" className="btn btn-primary">
            New item
          </Link>
        }
      />
      <div className="mb-3 flex flex-wrap items-center gap-2" role="search">
        <SearchInput placeholder="Search key and body" label="Search items" value={text} onChange={setText} />
        <div className="grid w-full grid-cols-[repeat(auto-fill,minmax(9.5rem,1fr))] gap-2 sm:flex sm:w-auto sm:flex-wrap">
          <FilterSelect label="Type" value={f.type} onChange={(v) => set("type", v)} options={Object.keys(ITEM_TYPE_LABEL)} />
          <FilterSelect label="State" value={f.state} onChange={(v) => set("state", v)} options={STATES} />
          <FilterSelect label="Scope" value={f.scope} onChange={(v) => set("scope", v)} options={SCOPES} />
          <FilterSelect label="Managed by" value={f.managed_by} onChange={(v) => set("managed_by", v)} options={["ui", "git", "source"]} />
        </div>
        {filtered && (
          <button
            className="btn btn-ghost btn-sm"
            onClick={() => {
              setText("");
              setSp({}, { replace: true });
            }}
          >
            Clear
          </button>
        )}
      </div>
      <SectionHeading
        icon={ICONS.book}
        title={filtered ? "Matching items" : "All items"}
        count={q.data?.total}
        actions={q.data && <SharedOrigin items={q.data.items} />}
      />

      {q.isPending && <Loading />}
      {q.isError && <ErrorBox error={q.error} />}
      {q.data && q.data.items.length === 0 && (
        <EmptyState
          title={filtered ? "No items match these filters" : "The library is empty"}
          action={
            !filtered && (
              <Link to="/library/new" className="btn btn-primary">
                Create the first item
              </Link>
            )
          }
        >
          {filtered
            ? "Try clearing a filter."
            : "Import a context repo with `satchel sync import <folder> --workspace <slug>`, or create items here."}
        </EmptyState>
      )}
      {q.data && q.data.items.length > 0 && <ItemTable items={q.data.items} />}
    </>
  );
}


const itemPath = (key: string) => `/library/item/${key.split("/").map(encodeURIComponent).join("/")}`;

const ORIGIN_LABEL: Record<string, string> = { git: "managed in git", source: "synced from sources", ui: "edited in the UI" };

/** Where most listed items are managed, and whether that is all of them. */
function usualOrigin(items: ItemSummary[]): { origin: ItemSummary["managed_by"]; all: boolean } | null {
  const counts = new Map<ItemSummary["managed_by"], number>();
  for (const i of items) counts.set(i.managed_by, (counts.get(i.managed_by) ?? 0) + 1);
  const top = [...counts.entries()].sort((x, y) => y[1] - x[1])[0];
  return top ? { origin: top[0], all: top[1] === items.length } : null;
}

/** Say once where most items come from, so only the others need a badge. */
function SharedOrigin({ items }: { items: ItemSummary[] }) {
  const u = usualOrigin(items);
  if (!u || (u.all && u.origin === "ui")) return null;
  return (
    <span className="text-xs text-muted">
      {u.all ? "All" : "Unmarked items are"} {ORIGIN_LABEL[u.origin] ?? u.origin}
    </span>
  );
}

/**
 * One row per item. Rows show only what differs: an origin badge on items managed somewhere
 * other than most, a state badge when an item is not published, and "changed" when a published
 * item has unpublished edits. Feedback lives on the
 * item's page.
 */
function ItemTable({ items }: { items: ItemSummary[] }) {
  const nav = useNavigate();
  const usual = usualOrigin(items)?.origin;
  const scopes = new Set(items.map((i) => i.scope_kind));
  return (
    <TableCard>
      <thead className="border-b border-line">
        <tr>
          <th className="th w-full">Key</th>
          <th className="th hidden w-px md:table-cell">Type</th>
          {scopes.size > 1 && <th className="th hidden w-px xl:table-cell">Scope</th>}
          <th className="th hidden w-px text-right sm:table-cell" title="Times served to agents in the last 30 days">
            Served 30d
          </th>
          <th className="th hidden w-px lg:table-cell">Updated</th>
        </tr>
      </thead>
      <tbody className="divide-y divide-line">
        {items.map((i) => {
          const ahead = i.current_seq != null && i.current_seq !== i.published_seq;
          return (
            <tr key={i.key} className="cursor-pointer hover:bg-sunken/70" onClick={() => nav(itemPath(i.key))}>
              <td className="td">
                <span className="flex flex-wrap items-start gap-x-2 gap-y-1">
                  <Link className="min-w-0 text-ink hover:text-accent" to={itemPath(i.key)} onClick={(e) => e.stopPropagation()}>
                    <KeyLines itemKey={i.key} />
                  </Link>
                  {i.managed_by !== usual && i.managed_by === "git" && <GitBadge />}
                  {i.managed_by !== usual && i.managed_by === "source" && <SourceBadge />}
                  {i.managed_by !== usual && i.managed_by === "ui" && <span className="badge">UI</span>}
                  {i.state !== "published" && <StateBadge state={i.state} />}
                  {i.has_changes && i.published_seq != null && (
                    <span className="badge badge-warn" title="Published, with unpublished changes">
                      changed{ahead && <span className="mono"> v{i.published_seq} → v{i.current_seq}</span>}
                    </span>
                  )}
                </span>
              </td>
              <td className="td hidden whitespace-nowrap md:table-cell">
                <TypeBadge type={i.type} />
              </td>
              {scopes.size > 1 && <td className="td hidden text-muted xl:table-cell">{i.scope_kind}</td>}
              <td className="td hidden text-right tabular-nums sm:table-cell">{i.usage.served_30d}</td>
              <td className="td hidden whitespace-nowrap text-muted lg:table-cell">{timeAgo(i.updated_at)}</td>
            </tr>
          );
        })}
      </tbody>
    </TableCard>
  );
}
