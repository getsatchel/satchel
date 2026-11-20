import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { endpoints } from "@/api/endpoints";
import type { AssetNode } from "@/api/types";
import { ConnectSheet, ConnectSteps } from "@/components/ConnectAgent";
import { CardFooter, CardHeader, ErrorBox, Icon, ICONS, PageHeader, PillButton, SectionHeading, StatCell, StatGrid, StatusDot, type Tone } from "@/components/ui";
import { fullTime, timeAgo } from "@/lib/format";
import { useRole } from "@/lib/role";

const DASH = "–";
const statusTone = (status: string): Tone => (status === "answered" ? "good" : status === "error" ? "bad" : "muted");

/**
 * Home for explorers and analysts. They ask questions in their agent, not here, so the page
 * says how to connect one, shows their recent questions, and what the agent knows about.
 */
export function Home() {
  const { me, can } = useRole();
  const userId = me.data?.user_id ?? undefined;
  const mine = useQuery({
    queryKey: ["turns", "home", userId],
    queryFn: () => endpoints.listTurns({ user_id: userId, limit: 5 }),
    enabled: me.data !== undefined,
  });
  // A member without a display name has their email as the name: greet only a real name.
  const name = me.data?.name;
  const first = name && !name.includes("@") ? name.split(/\s+/)[0] : undefined;
  const asked = (mine.data?.total ?? 0) > 0;
  const connect = <ConnectAgent compact={asked} />;
  return (
    <>
      <PageHeader
        title={first ? `Hi ${first}` : "Home"}
        sub={`Ask data questions in Claude or another agent. Satchel gives it ${me.data?.workspace_name ?? "this workspace"}'s definitions, so answers use the numbers your team agreed on.`}
      />
      <div className="space-y-9">
        {!asked && mine.isSuccess && connect}
        <section aria-labelledby="home-questions">
          <SectionHeading id="home-questions" icon={ICONS.pulse} title="Your recent questions" count={mine.data?.total} />
          <div className="card overflow-hidden">
            {mine.isError && (
              <div className="p-4">
                <ErrorBox error={mine.error} />
              </div>
            )}
            {mine.isPending && <p className="px-4 py-6 text-sm text-muted sm:px-5">Loading...</p>}
            {mine.data && mine.data.turns.length === 0 && (
              <p className="px-4 py-6 text-sm text-muted sm:px-5">Nothing yet. Questions you ask your agent appear here with their answers.</p>
            )}
            {mine.data && mine.data.turns.length > 0 && (
              <ul className="divide-y divide-line">
                {mine.data.turns.map((t) => (
                  <li key={t.id}>
                    <Link
                      to={t.turn_count > 1 ? `/sessions/${t.session_id}?turn=${t.seq}` : `/sessions/${t.session_id}`}
                      className="group flex items-start gap-3 px-4 py-3.5 hover:bg-sunken/60 sm:px-5"
                    >
                      <div className="min-w-0 flex-1">
                        <div className="line-clamp-2 text-sm font-medium [overflow-wrap:anywhere]">{t.question ?? "(no question recorded)"}</div>
                        <div className="mt-1 flex flex-wrap items-center gap-x-3 text-xs text-muted">
                          <StatusDot tone={statusTone(t.status)}>{t.status}</StatusDot>
                          <span title={fullTime(t.started_at)}>{timeAgo(t.started_at)}</span>
                          {t.client && <span>{t.client}</span>}
                        </div>
                      </div>
                      <Icon d={ICONS.chevron} size={14} className="mt-1 text-muted/60 group-hover:text-ink" />
                    </Link>
                  </li>
                ))}
              </ul>
            )}
            <CardFooter>
              <PillButton to={can("analyst") ? "/sessions?who=me" : "/sessions"}>All your questions</PillButton>
              {can("analyst") && <PillButton to="/sessions">What others asked</PillButton>}
            </CardFooter>
          </div>
        </section>
        <WhatItKnows />
        {asked && connect}
      </div>
      <ConnectSheet />
    </>
  );
}

/** How to add Satchel to an agent, from this server's own settings. */
function ConnectAgent({ compact }: { compact: boolean }) {
  return (
    <section aria-labelledby="home-connect">
      <SectionHeading id="home-connect" icon={ICONS.external} title={compact ? "Connect another agent" : "Connect your agent"} />
      <div className="card overflow-hidden">
        <CardHeader>Add Satchel to your agent once, then ask data questions in any chat.</CardHeader>
        <div className="px-4 py-4 sm:px-5">
          <ConnectSteps />
        </div>
      </div>
    </section>
  );
}

function count(nodes: AssetNode[]) {
  let tables = 0;
  let documented = 0;
  const walk = (n: AssetNode) => {
    if ((n.kind === "table" || n.kind === "view") && !n.dropped && !n.excluded_from_ai) {
      tables++;
      if (n.has_doc) documented++;
    }
    n.children.forEach(walk);
  };
  nodes.forEach(walk);
  return { tables, documented };
}

/** Published context by kind: what the agent can draw on when it answers. */
function WhatItKnows() {
  const items = (type: string) => ({
    queryKey: ["items", { type, state: "published", limit: 1 }],
    queryFn: () => endpoints.listItems({ type, state: "published", limit: 1 }),
  });
  const glossary = useQuery(items("glossary_term"));
  const golden = useQuery(items("golden_query"));
  const guides = useQuery(items("guide"));
  const assets = useQuery({ queryKey: ["assets", "", ""], queryFn: () => endpoints.listAssets({ connection: "", q: "" }) });
  const n = (q: { data?: { total: number } }) => (q.data ? q.data.total.toLocaleString() : DASH);
  const cov = assets.data ? count(assets.data.assets) : null;
  return (
    <section aria-labelledby="home-knows">
      <SectionHeading id="home-knows" icon={ICONS.heart} title="What your agent knows here" />
      <div className="card overflow-hidden">
        <StatGrid>
          <StatCell label="Glossary terms" tone="accent" glyph="diamond" value={n(glossary)} info="Business terms with an agreed definition. Agents use them instead of guessing what a word means." />
          <StatCell label="Golden queries" tone="good" value={n(golden)} info="Questions with a vetted SQL answer. Agents reuse them for the same or similar questions." />
          <StatCell label="Guides" tone="info" glyph="square" value={n(guides)} info="Notes on how to analyse this data: which tables to use, known pitfalls." />
          <StatCell
            label="Documented tables"
            tone="good"
            glyph="ring"
            value={cov ? cov.documented.toLocaleString() : DASH}
            secondary={cov ? `of ${cov.tables}` : null}
            info="Tables agents can query that have a description of their columns and caveats."
          />
        </StatGrid>
        <CardFooter>
          <PillButton to="/library?type=glossary_term">Browse glossary</PillButton>
          <PillButton to="/library">Library</PillButton>
          <PillButton to="/catalog">Catalog</PillButton>
        </CardFooter>
      </div>
    </section>
  );
}
