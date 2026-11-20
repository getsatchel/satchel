import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { endpoints } from "@/api/endpoints";
import type { InviteOut, Member, MemberRole, SignInPolicy } from "@/api/types";
import { CopyField } from "@/components/CopyField";
import { useToast } from "@/components/Toast";
import { EmptyState, ErrorBox, ICONS, Icon, Loading, Modal, Notice, SearchInput, SectionHeading, Segmented, Sheet, StatusDot, TableCard } from "@/components/ui";
import { errMsg, fullTime, timeAgo } from "@/lib/format";
import { useRole } from "@/lib/role";

/** The sign-in page with the workspace chosen and the email pre-filled at the identity provider. */
function useInviteLink() {
  const slug = useRole().me.data?.workspace_slug;
  return (email: string) => `${window.location.origin}/auth/login?${new URLSearchParams({ ...(slug ? { workspace: slug } : {}), login_hint: email })}`;
}

/** Lowest to highest, as the server ranks them. */
const ROLES: { id: MemberRole; label: string; does: string }[] = [
  { id: "explorer", label: "Explorer", does: "Asks agents questions and reads published context." },
  { id: "analyst", label: "Analyst", does: "Also proposes context changes and runs evals." },
  { id: "curator", label: "Curator", does: "Also reviews suggestions, edits drafts and publishes releases." },
  { id: "admin", label: "Admin", does: "Also manages members, sources, connections and settings." },
];

type Show = "all" | Member["status"];
const SHOW_LABEL: Record<Show, string> = { all: "All", active: "Active", invited: "Invited", deactivated: "Deactivated" };
const EMAIL = /^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$/;

const who = (m: Member) => m.name ?? m.email ?? "(no email)";
const splitList = (s: string) => s.split(/[\s,;]+/).map((x) => x.trim()).filter(Boolean);

export function MembersSection() {
  const q = useQuery({ queryKey: ["members"], queryFn: endpoints.listMembers });
  const [sp, setSp] = useSearchParams();
  const show = (["active", "invited", "deactivated"] as const).find((s) => s === sp.get("show")) ?? "all";
  const [search, setSearch] = useState("");
  const [inviting, setInviting] = useState(false);
  const [confirm, setConfirm] = useState<{ m: Member; kind: "deactivate" | "uninvite" } | null>(null);
  const qc = useQueryClient();
  const toast = useToast();
  const done = () => void qc.invalidateQueries({ queryKey: ["members"] });

  const update = useMutation({
    mutationFn: ({ m, patch }: { m: Member; patch: { role?: MemberRole; active?: boolean } }) => endpoints.updateMember(m.id, patch),
    onSuccess: (m, { patch }) => {
      toast("ok", patch.role ? `${who(m)} is now ${aRole(m.role)}` : patch.active ? `Reactivated ${who(m)}` : `Deactivated ${who(m)}`);
      setConfirm(null);
      done();
    },
    onError: (e) => toast("err", errMsg(e)),
  });
  const uninvite = useMutation({
    mutationFn: (m: Member) => endpoints.cancelInvite(m.id),
    onSuccess: (_, m) => {
      toast("ok", `Removed the invite for ${who(m)}`);
      setConfirm(null);
      done();
    },
    onError: (e) => toast("err", errMsg(e)),
  });

  if (q.isPending) return <Loading />;
  if (q.isError) return <ErrorBox error={q.error} />;
  const all = q.data.members;
  const count = (s: Show) => (s === "all" ? all.length : all.filter((m) => m.status === s).length);
  const needle = search.trim().toLowerCase();
  const rows = all.filter(
    (m) => (show === "all" || m.status === show) && (!needle || `${m.name ?? ""} ${m.email ?? ""}`.toLowerCase().includes(needle)),
  );
  const invite = (
    <button type="button" className="btn btn-primary btn-sm" onClick={() => setInviting(true)}>
      <Icon d={ICONS.plus} size={14} /> Invite
    </button>
  );

  return (
    <>
      <section aria-labelledby="members-h">
        <SectionHeading id="members-h" icon={ICONS.user} title="Members" count={all.length} actions={invite} />
        <div className="space-y-3">
          {!q.data.oidc_configured && (
            <Notice tone="warn">
              The server has no identity provider (<span className="font-mono">SATCHEL_OIDC_ISSUER</span>), so invited people can't sign in to the UI. They can still use a token from{" "}
              <span className="font-mono">satchel token create</span>.
            </Notice>
          )}
          <div className="flex flex-wrap items-center gap-2">
            <Segmented
              label="Show"
              value={show}
              onChange={(v) =>
                setSp(
                  (prev) => {
                    const n = new URLSearchParams(prev);
                    if (v === "all") n.delete("show");
                    else n.set("show", v);
                    return n;
                  },
                  { replace: true },
                )
              }
              options={(["all", "active", "invited", "deactivated"] as const).map((s) => ({
                id: s,
                label: `${SHOW_LABEL[s]} ${count(s)}`,
              }))}
            />
            <SearchInput value={search} onChange={setSearch} placeholder="Name or email" label="Search members" className="w-full sm:w-64" />
          </div>
          {rows.length === 0 ? (
            <EmptyState title={needle || show !== "all" ? "No members match." : "No members yet."} icon={ICONS.user}>
              {needle || show !== "all" ? "Clear the search or show all members." : "Invite people by email."}
            </EmptyState>
          ) : (
            <MembersTable
              rows={rows}
              busy={update.isPending ? update.variables?.m.id : undefined}
              onRole={(m, role) => update.mutate({ m, patch: { role } })}
              onReactivate={(m) => update.mutate({ m, patch: { active: true } })}
              onDeactivate={(m) => setConfirm({ m, kind: "deactivate" })}
              onUninvite={(m) => setConfirm({ m, kind: "uninvite" })}
            />
          )}
        </div>
      </section>
      <SignInSection policy={q.data.sign_in} />
      {inviting && <InviteDialog provider={q.data.provider ?? null} createsAccounts={q.data.creates_accounts ?? false} onClose={() => setInviting(false)} />}
      <Modal
        open={confirm !== null}
        onClose={() => setConfirm(null)}
        title={confirm ? (confirm.kind === "deactivate" ? `Deactivate ${who(confirm.m)}?` : `Remove the invite for ${who(confirm.m)}?`) : ""}
        footer={
          <>
            <button type="button" className="btn" onClick={() => setConfirm(null)}>
              Cancel
            </button>
            {confirm?.kind === "deactivate" ? (
              <button type="button" className="btn btn-danger" disabled={update.isPending} onClick={() => update.mutate({ m: confirm.m, patch: { active: false } })}>
                {update.isPending ? "Deactivating..." : "Deactivate"}
              </button>
            ) : (
              <button type="button" className="btn btn-danger" disabled={uninvite.isPending} onClick={() => confirm && uninvite.mutate(confirm.m)}>
                {uninvite.isPending ? "Removing..." : "Remove"}
              </button>
            )}
          </>
        }
      >
        <p className="text-sm">
          {confirm?.kind === "deactivate"
            ? "Their tokens, sessions and sign-ins stop working until you reactivate them. Their sessions, feedback and context changes stay."
            : "They can't sign in unless they are invited again."}
        </p>
      </Modal>
    </>
  );
}

const aRole = (r: MemberRole) => `${r === "admin" || r === "explorer" || r === "analyst" ? "an" : "a"} ${r}`;

function MembersTable(props: {
  rows: Member[];
  busy: string | undefined;
  onRole: (m: Member, role: MemberRole) => void;
  onReactivate: (m: Member) => void;
  onDeactivate: (m: Member) => void;
  onUninvite: (m: Member) => void;
}) {
  return (
    <TableCard>
      <thead className="border-b border-line">
        <tr>
          <th className="th w-full">Member</th>
          <th className="th w-px whitespace-nowrap">Role</th>
          <th className="th hidden w-px whitespace-nowrap md:table-cell">Last active</th>
          <th className="th hidden w-px sm:table-cell">
            <span className="sr-only">Actions</span>
          </th>
        </tr>
      </thead>
      <tbody className="divide-y divide-line">
        {props.rows.map((m) => {
          const off = m.status === "deactivated";
          const roleLocked = m.is_you ? "You can't change your own role" : off ? "Reactivate the member to change their role" : undefined;
          return (
            <tr key={m.id} className={off ? "text-muted" : undefined}>
              <td className="td min-w-[11rem]">
                <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                  <span className={`font-medium [overflow-wrap:anywhere] ${off ? "" : "text-ink"}`}>{who(m)}</span>
                  {m.is_you && <span className="badge">you</span>}
                  {m.status !== "active" && <StatusDot tone={m.status === "invited" ? "accent" : "muted"}>{m.status}</StatusDot>}
                </div>
                {m.name && m.email && <div className="mt-0.5 text-xs text-muted [overflow-wrap:anywhere]">{m.email}</div>}
                <div className="mt-0.5 text-xs text-muted md:hidden">{m.last_active_at ? `Active ${timeAgo(m.last_active_at)}` : m.status === "invited" ? `Invited ${timeAgo(m.created_at)}` : null}</div>
                {/* Phones: the action sits under the name, where the table's right edge can't hide it. */}
                <div className="-ml-2 mt-1 sm:hidden">
                  <RowAction m={m} {...props} />
                </div>
              </td>
              <td className="td whitespace-nowrap">
                <select
                  className="input h-9 w-auto pr-7 sm:h-8"
                  aria-label={`Role for ${who(m)}`}
                  title={roleLocked}
                  value={m.role}
                  disabled={Boolean(roleLocked) || props.busy === m.id}
                  onChange={(e) => props.onRole(m, e.target.value as MemberRole)}
                >
                  {ROLES.map((r) => (
                    <option key={r.id} value={r.id}>
                      {r.label}
                    </option>
                  ))}
                </select>
              </td>
              <td className="td hidden whitespace-nowrap text-muted md:table-cell" title={m.last_active_at ? fullTime(m.last_active_at) : undefined}>
                {m.last_active_at ? timeAgo(m.last_active_at) : "–"}
              </td>
              <td className="td hidden whitespace-nowrap text-right sm:table-cell">
                <RowAction m={m} {...props} />
              </td>
            </tr>
          );
        })}
      </tbody>
    </TableCard>
  );
}

type Actions = { busy: string | undefined; onReactivate: (m: Member) => void; onDeactivate: (m: Member) => void; onUninvite: (m: Member) => void };

function RowAction({ m, ...p }: Actions & { m: Member }) {
  const link = useInviteLink();
  const toast = useToast();
  if (m.is_you) return null;
  if (m.status === "deactivated")
    return (
      <button type="button" className="btn btn-ghost btn-sm" disabled={p.busy === m.id} onClick={() => p.onReactivate(m)}>
        Reactivate
      </button>
    );
  if (m.status === "invited")
    return (
      <span className="inline-flex gap-1">
        {m.email && (
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            aria-label={`Copy the invite link for ${who(m)}`}
            onClick={() =>
              void navigator.clipboard.writeText(link(m.email!)).then(
                () => toast("ok", `Copied the invite link for ${who(m)}`),
                () => toast("err", "Copy failed"),
              )
            }
          >
            Copy link
          </button>
        )}
        <button type="button" className="btn btn-ghost btn-sm text-bad" onClick={() => p.onUninvite(m)} aria-label={`Remove the invite for ${who(m)}`}>
          Remove
        </button>
      </span>
    );
  return (
    <button type="button" className="btn btn-ghost btn-sm text-bad" onClick={() => p.onDeactivate(m)} aria-label={`Deactivate ${who(m)}`}>
      Deactivate
    </button>
  );
}

function InviteDialog({ onClose, provider, createsAccounts }: { onClose: () => void; provider: string | null; createsAccounts: boolean }) {
  const [done, setDone] = useState<InviteOut | null>(null);
  const [text, setText] = useState("");
  const [role, setRole] = useState<MemberRole>("analyst");
  const [error, setError] = useState<string | null>(null);
  const [checked, setChecked] = useState(false);
  const qc = useQueryClient();
  const toast = useToast();
  const emails = splitList(text);
  const bad = emails.filter((e) => !EMAIL.test(e));
  const save = useMutation({
    mutationFn: () => endpoints.inviteMembers({ emails, role }),
    onSuccess: (r) => {
      const n = r.invited.length;
      const parts = [
        n === 1 ? `Invited ${r.invited[0]?.email ?? "1 person"} as ${aRole(role)}` : n > 1 ? `Invited ${n} people as ${role}s` : null,
        r.existing.length ? `${r.existing.join(", ")} ${r.existing.length === 1 ? "is already a member" : "are already members"}` : null,
      ];
      void qc.invalidateQueries({ queryKey: ["members"] });
      // Show the links to send (and what the provider did); nothing to show when nobody new was
      // invited. The result screen says who was invited, so a toast only adds who already was.
      if (n || r.accounts?.length) {
        setDone(r);
        if (parts[1]) toast("err", parts[1]);
      } else {
        toast(n ? "ok" : "err", parts.filter(Boolean).join(". "));
        onClose();
      }
    },
    onError: (e) => setError(errMsg(e)),
  });
  if (done) return <InviteDone r={done} provider={provider} onClose={onClose} />;
  const idp = provider ?? "your identity provider";
  return (
    <Sheet
      open
      onClose={onClose}
      dirty={text.trim() !== ""}
      title="Invite members"
      footer={
        <>
          <button type="button" className="btn" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" form="invite-form" className="btn btn-primary" disabled={save.isPending || emails.length === 0 || bad.length > 0}>
            {save.isPending ? "Inviting..." : emails.length > 1 ? `Invite ${emails.length}` : "Invite"}
          </button>
        </>
      }
    >
      <form
        id="invite-form"
        className="space-y-4"
        onSubmit={(ev) => {
          ev.preventDefault();
          setError(null);
          save.mutate();
        }}
      >
        <label className="block">
          <span className="label">Emails</span>
          <textarea className="input min-h-[5rem] w-full py-2" value={text} onChange={(e) => setText(e.target.value)} onBlur={() => setChecked(true)} placeholder="alice@example.com, bob@example.com" spellCheck={false} autoComplete="off" />
          {checked && bad.length > 0 ? (
            <span role="alert" className="mt-1 block text-xs text-bad [overflow-wrap:anywhere]">
              Not an email address: {bad.join(", ")}
            </span>
          ) : (
            <span className="mt-1 block text-xs text-muted">Separate addresses with commas or new lines.</span>
          )}
        </label>
        <div>
          <span className="label">Role</span>
          <Segmented label="Role" options={ROLES.map((r) => ({ id: r.id, label: r.label }))} value={role} onChange={setRole} />
          <span className="mt-1 block text-xs text-muted">{ROLES.find((r) => r.id === role)?.does}</span>
        </div>
        <p className="text-xs text-muted">
          {createsAccounts
            ? `${idp} emails each person a temporary password. Then they sign in here with that email.`
            : `They sign in with ${idp} using this email, so they need an account there. You get a link to send them.`}
        </p>
        {error && <ErrorBox error={error} />}
      </form>
    </Sheet>
  );
}

const ACCOUNT_NOTE = {
  created: (p: string) => `${p} emailed them a temporary password.`,
  exists: (p: string) => `They already have a ${p} account.`,
  failed: (p: string) => `Couldn't create their ${p} account.`,
} as const;

/** After inviting: the link to send each person, and whether the provider made them an account. */
function InviteDone({ r, provider, onClose }: { r: InviteOut; provider: string | null; onClose: () => void }) {
  const link = useInviteLink();
  const p = provider ?? "The identity provider";
  const accounts = new Map((r.accounts ?? []).map((a) => [a.email, a]));
  const emails = [...new Set([...r.invited.map((m) => m.email ?? ""), ...accounts.keys()])].filter(Boolean);
  const failed = (r.accounts ?? []).some((a) => a.status === "failed");
  return (
    <Sheet
      open
      onClose={onClose}
      title={r.invited.length === 1 ? "Invited" : `Invited ${r.invited.length}`}
      footer={
        <button type="button" className="btn btn-primary" onClick={onClose}>
          Done
        </button>
      }
    >
      <div className="space-y-4">
        <p className="text-sm text-muted">Send each person their link. It opens the sign-in page with their email filled in where {provider ?? "the provider"} supports it.</p>
        {emails.map((e) => {
          const a = accounts.get(e);
          return (
            <div key={e} className="space-y-1">
              <CopyField label={e} value={link(e)} />
              {a && (
                <p className={`text-xs ${a.status === "failed" ? "text-bad" : "text-muted"} [overflow-wrap:anywhere]`}>
                  {ACCOUNT_NOTE[a.status](a.status === "created" ? p : provider ?? "provider")}
                  {a.detail ? ` ${a.detail}` : ""}
                </p>
              )}
            </div>
          );
        })}
        {failed && <Notice tone="warn">Create the account yourself, or invite the same email again to retry.</Notice>}
      </div>
    </Sheet>
  );
}

function SignInSection({ policy }: { policy: SignInPolicy }) {
  const [provision, setProvision] = useState(policy.provision);
  const [domains, setDomains] = useState(policy.allowed_email_domains.join(", "));
  const qc = useQueryClient();
  const toast = useToast();
  const next: SignInPolicy = { provision, allowed_email_domains: provision === "explorer" ? splitList(domains) : [] };
  const dirty = next.provision !== policy.provision || next.allowed_email_domains.join() !== policy.allowed_email_domains.join();
  const save = useMutation({
    mutationFn: () => endpoints.setSignIn(next),
    onSuccess: (p) => {
      setDomains(p.allowed_email_domains.join(", "));
      toast("ok", "Saved the sign-in policy");
      void qc.invalidateQueries({ queryKey: ["members"] });
    },
    onError: (e) => toast("err", errMsg(e)),
  });
  return (
    <section aria-labelledby="signin-h" className="mt-9">
      <SectionHeading id="signin-h" icon={ICONS.link} title="Sign-in" />
      <form
        className="card space-y-4 p-4"
        onSubmit={(ev) => {
          ev.preventDefault();
          save.mutate();
        }}
      >
        <div>
          <span className="label">People who weren't invited</span>
          <Segmented
            label="People who weren't invited"
            value={provision}
            onChange={setProvision}
            options={[
              { id: "reject", label: "Refused" },
              { id: "explorer", label: "Join as explorers" },
            ]}
          />
          <span className="mt-1 block text-xs text-muted">
            {provision === "reject" ? "Only members can sign in." : "Anyone your identity provider verifies joins as an explorer on first sign-in."}
          </span>
        </div>
        {provision === "explorer" && (
          <label className="block">
            <span className="label">Email domains (optional)</span>
            <input className="input mono w-full sm:max-w-md" value={domains} onChange={(e) => setDomains(e.target.value)} placeholder="example.com" spellCheck={false} autoComplete="off" />
            <span className="mt-1 block text-xs text-muted">Leave empty to allow any domain.</span>
          </label>
        )}
        <div>
          <button type="submit" className="btn btn-sm" disabled={!dirty || save.isPending}>
            {save.isPending ? "Saving..." : "Save"}
          </button>
        </div>
      </form>
    </section>
  );
}
