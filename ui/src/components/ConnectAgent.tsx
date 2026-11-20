import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { endpoints } from "@/api/endpoints";
import type { ConnectInfo } from "@/api/types";
import { CopyField } from "@/components/CopyField";
import { docsUrl } from "@/components/nav";
import { ErrorBox, Icon, ICONS, Notice, Sheet, Tabs, useSheetParam } from "@/components/ui";
import { useRole } from "@/lib/role";

type Client = "claude" | "code" | "cursor" | "other";
const CLIENTS: { id: Client; label: string }[] = [
  { id: "claude", label: "Claude" },
  { id: "code", label: "Claude Code" },
  { id: "cursor", label: "Cursor" },
  { id: "other", label: "Other clients" },
];
const TOKEN = "satchel_pat_...";

/**
 * How to add Satchel to an agent, written from what this server knows: its public URL, whether
 * clients can sign in through the identity provider, and whether claude.ai can reach it. Where
 * sign-in can't work, it says why and gives the token route instead, with the command filled in.
 */
export function ConnectSteps() {
  const info = useQuery({ queryKey: ["connect"], queryFn: endpoints.connect, staleTime: Infinity });
  const { me } = useRole();
  const [client, setClient] = useState<Client>("claude");
  if (info.isError) return <ErrorBox error={info.error} />;
  if (!info.data || !me.data) return <p className="text-sm text-muted">Loading...</p>;
  const c = info.data;
  const admin = me.data.role === "admin";
  const token = tokenCommand(me.data.workspace_slug, me.data.email, client === "cursor" ? "cursor" : client === "code" ? "claude-code" : "agent");
  return (
    <div className="space-y-4">
      {admin && <UrlMismatch url={c.mcp_url} />}
      <Tabs tabs={CLIENTS} value={client} onChange={setClient} />
      {client === "claude" && <ClaudeSteps c={c} admin={admin} />}
      {client === "code" &&
        (c.oauth ? (
          <Steps>
            <li>
              Run this in a terminal:
              <CopyField label="Command" value={`claude mcp add --transport http satchel ${c.mcp_url}`} />
            </li>
            <li>
              In Claude Code, run <code className="text-ink">/mcp</code>, pick satchel and choose Authenticate.
            </li>
            <li>Sign in{c.provider ? ` with ${c.provider}` : ""} in the browser tab that opens, then approve.</li>
          </Steps>
        ) : (
          <>
            <NoSignIn admin={admin} />
            <Steps>
              <TokenStep command={token} admin={admin} />
              <li>
                Add Satchel with the token:
                <CopyField label="Command" value={`claude mcp add --transport http satchel ${c.mcp_url} --header "Authorization: Bearer ${TOKEN}"`} />
              </li>
            </Steps>
          </>
        ))}
      {client === "cursor" && (
        <>
          <p className="text-sm text-muted">Cursor can't sign in to Satchel yet (its redirect isn't a web address), so it uses a personal access token.</p>
          <Steps>
            <TokenStep command={token} admin={admin} />
            <li>
              Add this to <code className="text-ink">.cursor/mcp.json</code> (or Cursor Settings &gt; MCP), with the token in place of {TOKEN}:
              <CopyField
                label="mcp.json"
                value={JSON.stringify({ mcpServers: { satchel: { url: c.mcp_url, headers: { Authorization: `Bearer ${TOKEN}` } } } }, null, 2)}
              />
            </li>
          </Steps>
        </>
      )}
      {client === "other" && (
        <div className="space-y-3 text-sm text-muted">
          <p>Satchel speaks MCP over Streamable HTTP at this URL.</p>
          <CopyField label="MCP URL" value={c.mcp_url} />
          <p>
            {c.oauth
              ? `A client that supports MCP OAuth finds the sign-in on its own: give it the URL and sign in${c.provider ? ` with ${c.provider}` : ""}. Otherwise use a personal access token as a bearer header:`
              : "This server has no sign-in for agents, so send a personal access token as a bearer header:"}
          </p>
          <CopyField label="Header" value={`Authorization: Bearer ${TOKEN}`} />
          <Steps>
            <TokenStep command={token} admin={admin} />
          </Steps>
        </div>
      )}
      <a className="btn-pill" href={docsUrl("integrations/claude")} target="_blank" rel="noreferrer">
        Setup guide
        <Icon d={ICONS.external} size={12} strokeWidth={2.2} />
      </a>
    </div>
  );
}

function ClaudeSteps({ c, admin }: { c: ConnectInfo; admin: boolean }) {
  if (!c.public)
    return (
      <Notice tone="warn">
        Claude Desktop and claude.ai reach Satchel from Anthropic's servers, so they need a public https address, and this server is at{" "}
        <code>{c.mcp_url}</code>.{" "}
        {admin ? "Set SATCHEL_PUBLIC_URL to the server's public https URL, or use Claude Code meanwhile." : "Use Claude Code, or ask an admin to give Satchel a public https URL."}
      </Notice>
    );
  if (!c.oauth) return <NoSignIn admin={admin} desktop />;
  return (
    <Steps>
      <li>
        In Claude, open <span className="text-ink">Settings &gt; Connectors &gt; Add custom connector</span>.
      </li>
      <li>
        Name it Satchel and paste this URL. Leave the OAuth fields empty.
        <CopyField label="Connector URL" value={c.mcp_url} />
      </li>
      <li>Click Connect and sign in{c.provider ? ` with ${c.provider}` : ""}, with the account you use here.</li>
    </Steps>
  );
}

function NoSignIn({ admin, desktop }: { admin: boolean; desktop?: boolean }) {
  return (
    <Notice tone="warn">
      This server has no identity provider, so agents can't sign in{desktop ? " and Claude's connectors can't be used" : ""}.{" "}
      {admin ? "Set SATCHEL_OIDC_ISSUER and its client to turn sign-in on" : "An admin can turn sign-in on"}
      {desktop ? ". Meanwhile, use Claude Code or Cursor with a token." : ". Meanwhile, use a personal access token."}
    </Notice>
  );
}

function TokenStep({ command, admin }: { command: string; admin: boolean }) {
  return (
    <li>
      {admin ? "Make a personal access token on the server (it is printed once):" : "Ask whoever runs Satchel for a personal access token. They make it on the server with:"}
      <CopyField label="Command" value={command} />
    </li>
  );
}

function Steps({ children }: { children: React.ReactNode }) {
  return <ol className="list-decimal space-y-3 pl-5 text-sm text-muted [&_.label]:mt-1.5">{children}</ol>;
}

/** Agents must use SATCHEL_PUBLIC_URL: sign-ins and tokens are bound to it. */
function UrlMismatch({ url }: { url: string }) {
  const want = new URL(url).origin;
  if (want === window.location.origin) return null;
  return (
    <Notice tone="warn">
      You opened Satchel at <code>{window.location.origin}</code>, but SATCHEL_PUBLIC_URL is <code>{want}</code>. Agents must use the public URL: sign-ins and tokens are bound to it.
    </Notice>
  );
}

function tokenCommand(workspace: string, email: string | null | undefined, name: string) {
  return `satchel token create --workspace ${workspace} --user ${email ?? "you@example.com"} --name ${name}`;
}

/** `?sheet=connect` on any page that renders it: the steps in a panel. */
export function ConnectSheet() {
  const [sheet, , close] = useSheetParam();
  return (
    <Sheet open={sheet === "connect"} onClose={close} title="Connect an agent" wide>
      <ConnectSteps />
    </Sheet>
  );
}
