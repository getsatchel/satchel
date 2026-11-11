import { useEffect, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { endpoints } from "@/api/endpoints";
import { EmptyState, ErrorBox, Feedback, FilterSelect, GitBadge, ICONS, KeyLines, Loading, PageHeader, SearchInput, SectionHeading, SourceBadge, StateBadge, StatusDot, TableCard, TypeBadge } from "@/components/ui";
import { ITEM_TYPE_LABEL, timeAgo } from "@/lib/format";

const STATES = ["draft", "published", "proposed", "deprecated"];
const SCOPES = ["workspace", "domain", "connection", "asset"];

export function Library() {
  const [sp, setSp] = useSearchParams();
  const nav = useNavigate();
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
      <SectionHeading icon={ICONS.book} title={filtered ? "Matching items" : "All items"} count={q.data?.total} />

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
      {q.data && q.data.items.length > 0 && (
        <TableCard>
            <thead className="border-b border-line">
              <tr>
                <th className="th w-full">Key</th>
                <th className="th hidden w-px lg:table-cell">Type</th>
                <th className="th hidden w-px sm:table-cell">State</th>
                <th className="th hidden w-px xl:table-cell">Scope</th>
                <th className="th hidden w-px lg:table-cell">Version</th>
                <th className="th hidden w-px text-right sm:table-cell" title="Times served to agents in the last 30 days">Served 30d</th>
                <th className="th hidden w-px text-right sm:table-cell">Feedback</th>
                <th className="th hidden w-px xl:table-cell">Updated</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {q.data.items.map((i) => (
                <tr
                  key={i.key}
                  className="cursor-pointer hover:bg-sunken/70"
                  onClick={() => nav(`/library/item/${i.key.split("/").map(encodeURIComponent).join("/")}`)}
                >
                  <td className="td">
                    <span className="flex items-start gap-2">
                      <Link
                        className="min-w-0 text-ink hover:text-accent"
                        to={`/library/item/${i.key.split("/").map(encodeURIComponent).join("/")}`}
                        onClick={(e) => e.stopPropagation()}
                      >
                        <KeyLines itemKey={i.key} />
                      </Link>
                      {i.managed_by === "git" && <GitBadge />}
                      {i.managed_by === "source" && <SourceBadge />}
                    </span>
                    {(i.state !== "published" || i.has_changes) && (
                      <span className="mt-1 flex items-center gap-2 sm:hidden">
                        {i.state !== "published" && <StateBadge state={i.state} />}
                        {i.has_changes && <StatusDot tone="warn">changed</StatusDot>}
                      </span>
                    )}
                  </td>
                  <td className="td hidden whitespace-nowrap lg:table-cell">
                    <TypeBadge type={i.type} />
                  </td>
                  <td className="td hidden whitespace-nowrap sm:table-cell">
                    <span className="flex items-center gap-2">
                      <StateBadge state={i.state} />
                      {i.has_changes && (
                        <span className="badge badge-warn" title="Has unpublished changes">
                          changed
                        </span>
                      )}
                    </span>
                  </td>
                  <td className="td hidden text-muted xl:table-cell">{i.scope_kind}</td>
                  <td className="td mono hidden whitespace-nowrap text-muted lg:table-cell">
                    {i.published_seq != null ? `v${i.published_seq}` : "-"}
                    {i.current_seq != null && i.current_seq !== i.published_seq && (
                      <span className="text-warn"> → v{i.current_seq}</span>
                    )}
                  </td>
                  <td className="td hidden text-right tabular-nums sm:table-cell">{i.usage.served_30d}</td>
                  <td className="td hidden text-right sm:table-cell">
                    <Feedback up={i.usage.thumbs_up} down={i.usage.thumbs_down} />
                  </td>
                  <td className="td hidden whitespace-nowrap text-muted xl:table-cell">{timeAgo(i.updated_at)}</td>
                </tr>
              ))}
            </tbody>
        </TableCard>
      )}
    </>
  );
}

