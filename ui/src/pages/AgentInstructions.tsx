import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { endpoints } from "@/api/endpoints";
import { ApiError } from "@/api/client";
import type { ItemDetail } from "@/api/types";
import { CodeEditor } from "@/components/Code";
import { useToast } from "@/components/Toast";
import { EmptyState, ErrorBox, Loading, Notice, PageHeader, SectionHeading, Segmented, StatusDot, ICONS } from "@/components/ui";
import { pageIcon } from "@/components/nav";
import { errMsg } from "@/lib/format";

const STARTER = `# How to answer

- Lead with the answer in one or two sentences, then the figures.
- Name the time zone and currency of every figure.
- If the question is ambiguous, ask one clarifying question before querying.
`;

const isCurator = (role: string | undefined) => role === "curator" || role === "admin";

export function AgentInstructions() {
  const me = useQuery({ queryKey: ["me"], queryFn: endpoints.me, retry: false });
  const info = useQuery({ queryKey: ["agent-instructions"], queryFn: endpoints.agentInstructions });
  const key = info.data?.key;
  const item = useQuery({
    queryKey: ["item", key],
    queryFn: () => endpoints.getItem(key ?? ""),
    enabled: Boolean(key),
    retry: (n, e) => !(e instanceof ApiError && e.status === 404) && n < 2,
  });
  const missing = item.error instanceof ApiError && item.error.status === 404;
  const edit = isCurator(me.data?.role);

  const header = (
    <PageHeader
      title="Agent instructions"
      sub="How agents should work and answer in this workspace. Agents get them after Satchel's own workflow, which they cannot override."
    />
  );
  if (info.isPending || me.isPending || (key && item.isPending)) {
    return (
      <>
        {header}
        <Loading />
      </>
    );
  }
  const failed = info.error ?? (missing ? null : item.error);
  if (failed || !info.data) {
    return (
      <>
        {header}
        <ErrorBox error={failed ?? "Could not load the agent instructions."} />
      </>
    );
  }
  return (
    <>
      {header}
      <Body key={item.data?.current_seq ?? "new"} info={info.data} item={missing ? null : (item.data ?? null)} edit={edit} />
    </>
  );
}

function Body(props: { info: { base: string; key: string; max_chars: number }; item: ItemDetail | null; edit: boolean }) {
  const { info, item, edit } = props;
  const [creating, setCreating] = useState(false);
  const published = useQuery({
    queryKey: ["item-version", info.key, item?.published_seq],
    queryFn: () => endpoints.getVersion(info.key, item?.published_seq ?? 0),
    enabled: item?.published_seq != null,
  });

  if (!item && !creating) {
    return (
      <EmptyState
        title="No agent instructions."
        muted="Agents follow Satchel's workflow only."
        icon={pageIcon("/agent")}
        action={
          edit ? (
            <button type="button" className="btn btn-primary" onClick={() => setCreating(true)}>
              Write instructions
            </button>
          ) : undefined
        }
      >
        {edit ? `Write how agents should answer here, or commit ${info.key} to the context repo.` : "A curator can add them."}
      </EmptyState>
    );
  }

  const publishedBody = item?.published_seq != null ? (published.data?.body ?? null) : null;
  const git = item?.managed_by === "git";
  const deprecated = item?.state === "deprecated";
  const canEdit = edit && !git && !deprecated;

  return (
    <div className="grid gap-9 xl:grid-cols-[minmax(0,1fr)_minmax(0,1fr)] xl:gap-6">
      <section aria-labelledby="ai-edit-h" className="min-w-0">
        <SectionHeading id="ai-edit-h" icon={ICONS.edit} title="Instructions" actions={item && <ItemState item={item} />} />
        {git && <Notice>Managed in the context repo ({info.key}). Edit it there.</Notice>}
        {deprecated && <Notice tone="warn">Deprecated: agents no longer receive these instructions.</Notice>}
        {canEdit ? (
          <Editor info={info} item={item} />
        ) : (
          <div className="card p-4">
            {item?.body ? <Text>{item.body}</Text> : <p className="text-sm text-muted">None.</p>}
          </div>
        )}
      </section>
      <Received info={info} item={item} publishedBody={publishedBody} loading={published.isPending && item?.published_seq != null} />
    </div>
  );
}

/** As the agent receives it: wrapped, so long instructions read without sideways scrolling. */
function Text({ children }: { children: string }) {
  return <pre className="max-h-96 overflow-auto whitespace-pre-wrap break-words rounded border border-line bg-sunken p-2.5 text-xs leading-relaxed">{children}</pre>;
}

function ItemState({ item }: { item: ItemDetail }) {
  if (item.state === "deprecated") return <StatusDot tone="muted">deprecated</StatusDot>;
  if (item.published_seq == null) return <StatusDot tone="warn">draft</StatusDot>;
  if (item.has_changes) return <StatusDot tone="warn">unpublished changes</StatusDot>;
  return <StatusDot tone="good">published v{item.published_seq}</StatusDot>;
}

function Editor({ info, item }: { info: { key: string; max_chars: number }; item: ItemDetail | null }) {
  const saved = item?.body ?? "";
  const [text, setText] = useState(item ? saved : STARTER);
  const [error, setError] = useState<string | null>(null);
  const qc = useQueryClient();
  const toast = useToast();
  const over = text.length > info.max_chars;
  const dirty = item ? text !== saved : true;
  const save = useMutation({
    mutationFn: () =>
      item ? endpoints.updateItem(info.key, text, {}) : endpoints.createItem({ key: info.key, type: "agent_instructions", body: text, meta: {}, scope_kind: "workspace" }),
    onSuccess: () => {
      setError(null);
      toast("ok", "Saved as a draft");
      void qc.invalidateQueries({ queryKey: ["item", info.key] });
    },
    onError: (e) => setError(errMsg(e)),
  });
  const unpublished = item && (item.published_seq == null || item.has_changes);
  return (
    <div className="space-y-3">
      <CodeEditor label="Agent instructions" language="markdown" value={text} onChange={setText} height={360} />
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <button type="button" className="btn btn-primary" disabled={!dirty || over || !text.trim() || save.isPending} onClick={() => save.mutate()}>
          {save.isPending ? "Saving..." : "Save draft"}
        </button>
        <span className={`text-xs tabular-nums ${over ? "text-bad" : "text-muted"}`} aria-live="polite">
          {text.length.toLocaleString()} / {info.max_chars.toLocaleString()} characters
        </span>
      </div>
      {unpublished && !dirty && (
        <p className="text-xs text-muted">
          Agents get this draft once it is previewed and published in{" "}
          <Link to="/changes" className="text-accent hover:underline">
            Changes
          </Link>
          .
        </p>
      )}
      {error && <ErrorBox error={error} />}
    </div>
  );
}

function Received(props: { info: { base: string; key: string }; item: ItemDetail | null; publishedBody: string | null; loading: boolean }) {
  const { info, item } = props;
  const hasDraft = Boolean(item?.body) && (item?.published_seq == null || item?.has_changes);
  const [view, setView] = useState<"published" | "draft">(item?.published_seq == null ? "draft" : "published");
  const shown = view === "draft" ? (item?.body ?? null) : props.publishedBody;
  const seq = view === "draft" ? item?.current_seq : item?.published_seq;
  return (
    <section aria-labelledby="ai-recv-h" className="min-w-0">
      <SectionHeading
        id="ai-recv-h"
        icon={ICONS.doc}
        title="What agents receive"
      />
      <div className="card space-y-4 p-4">
        {hasDraft && item?.published_seq != null && (
          <Segmented
            label="Version"
            value={view}
            onChange={setView}
            options={[
              { id: "published", label: "Published" },
              { id: "draft", label: "Draft" },
            ]}
          />
        )}
        <details>
          <summary className="cursor-pointer text-sm font-medium">Satchel's workflow</summary>
          <p className="mb-2 mt-1 text-xs text-muted">Sent to every agent first. Workspace instructions cannot override it.</p>
          <Text>{info.base}</Text>
        </details>
        <div>
          <p className="mb-1 text-sm font-medium">This workspace</p>
          {props.loading && view === "published" ? (
            <Loading />
          ) : shown ? (
            <>
              <p className="mb-2 text-xs text-muted">
                {view === "draft" && item?.published_seq == null ? "Not published: agents do not get it yet." : view === "draft" ? "The draft, as agents will get it once published." : "Returned by start_analysis and first in every context pack."}
              </p>
              <Text>{`[ctx:${info.key}@v${seq ?? 1}]\n${shown.trim()}`}</Text>
            </>
          ) : (
            <p className="text-sm text-muted">Nothing published.</p>
          )}
        </div>
      </div>
    </section>
  );
}
