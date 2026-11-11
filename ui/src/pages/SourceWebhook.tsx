import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { endpoints } from "@/api/endpoints";
import type { SourceOut, WebhookSecretOut } from "@/api/types";
import { useToast } from "@/components/Toast";
import { ErrorBox, Modal, Notice, StatusDot } from "@/components/ui";
import { errMsg } from "@/lib/format";

function CopyField({ label, value, secret }: { label: string; value: string; secret?: boolean }) {
  const toast = useToast();
  return (
    <div>
      <span className="label">{label}</span>
      <div className="flex min-w-0 items-center gap-2">
        <code className={`min-w-0 flex-1 rounded-md border border-line bg-sunken px-2 py-1.5 text-xs [overflow-wrap:anywhere] ${secret ? "select-all" : ""}`}>{value}</code>
        <button
          type="button"
          className="btn btn-sm shrink-0"
          onClick={() =>
            void navigator.clipboard.writeText(value).then(
              () => toast("ok", `${label} copied`),
              () => toast("err", "Copy failed: select the text and copy it"),
            )
          }
        >
          Copy
        </button>
      </div>
    </div>
  );
}

/** Admin panel: set up, rotate or remove the source's push webhook. The secret is shown once. */
export function WebhookPanel({ s }: { s: SourceOut }) {
  const [shown, setShown] = useState<WebhookSecretOut | null>(null);
  const [confirm, setConfirm] = useState(false);
  const qc = useQueryClient();
  const toast = useToast();
  const set = useMutation({
    mutationFn: () => endpoints.setSourceWebhook(s.id),
    onSuccess: (r) => {
      setShown(r);
      void qc.invalidateQueries({ queryKey: ["sources"] });
    },
  });
  const remove = useMutation({
    mutationFn: () => endpoints.removeSourceWebhook(s.id),
    onSuccess: () => {
      setShown(null);
      setConfirm(false);
      toast("ok", `Removed the push webhook of ${s.name}`);
      void qc.invalidateQueries({ queryKey: ["sources"] });
    },
    onError: (e) => toast("err", errMsg(e)),
  });
  const git = Boolean(s.config.git_url);
  const follows = s.config.git_ref ? String(s.config.git_ref) : "the default branch";

  return (
    <section aria-label="Push webhook" className="space-y-3 border-t border-line pt-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="label-caps">Push webhook</h3>
        {s.webhook_url ? <StatusDot tone="good">set up</StatusDot> : <StatusDot tone="muted">off</StatusDot>}
      </div>
      <p className="max-w-3xl text-xs text-muted">
        {git
          ? `Your git host calls Satchel on every push and the source syncs right away. Pushes to other branches than ${follows} are ignored.`
          : "Anything that can send an HTTP request (CI after uploading the artifacts, a bucket notification relay) can trigger a sync with the secret in an X-Gitlab-Token header."}
      </p>
      {s.webhook_url && !shown && <CopyField label="URL" value={s.webhook_url} />}
      {shown && (
        <div className="space-y-3 rounded-lg border border-line bg-surface p-3">
          <Notice tone="warn">Copy the secret now. It is stored encrypted and not shown again; rotate it if you lose it.</Notice>
          <CopyField label="URL" value={shown.url} />
          <CopyField label="Secret" value={shown.secret} secret />
          <div className="grid gap-3 text-xs sm:grid-cols-2">
            <div>
              <h4 className="label-caps mb-1">GitHub</h4>
              <p className="text-muted">
                Repository → Settings → Webhooks → Add webhook. Payload URL: the URL above. Content type: <span className="font-mono">application/json</span>. Secret: the secret above. Events: Just the push event.
              </p>
            </div>
            <div>
              <h4 className="label-caps mb-1">GitLab</h4>
              <p className="text-muted">Project → Settings → Webhooks → Add new webhook. URL: the URL above. Secret token: the secret above. Trigger: Push events.</p>
            </div>
          </div>
          {shown.rotated && <p className="text-xs text-muted">The previous secret no longer works: update it on your git host.</p>}
        </div>
      )}
      {set.isError && <ErrorBox error={set.error} />}
      <div className="flex flex-wrap gap-2">
        <button type="button" className="btn btn-sm" disabled={set.isPending} onClick={() => set.mutate()}>
          {set.isPending ? "Generating..." : s.webhook_url ? "Rotate secret" : "Set up webhook"}
        </button>
        {s.webhook_url && (
          <button type="button" className="btn btn-sm btn-ghost text-bad" onClick={() => setConfirm(true)}>
            Remove webhook
          </button>
        )}
      </div>
      <Modal
        open={confirm}
        onClose={() => setConfirm(false)}
        title="Remove the push webhook?"
        footer={
          <>
            <button type="button" className="btn" onClick={() => setConfirm(false)}>
              Cancel
            </button>
            <button type="button" className="btn btn-danger" disabled={remove.isPending} onClick={() => remove.mutate()}>
              {remove.isPending ? "Removing..." : "Remove"}
            </button>
          </>
        }
      >
        <p className="text-sm">Deliveries are refused from now on. Delete the webhook on your git host too. Setting it up again makes a new secret.</p>
      </Modal>
    </section>
  );
}
