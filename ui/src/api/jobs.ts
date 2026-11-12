import { api } from "./client";
import type { CrawlOut, JobOut, SyncReportOut } from "./types";

const POLL_MS = 1000;
const MAX_WAIT_MS = 60 * 60 * 1000;

/** A queued job came back (202) rather than the finished report. */
export function isJob(x: unknown): x is JobOut {
  return typeof x === "object" && x !== null && "kind" in x && "status" in x && "attempts" in x;
}

/** Poll GET /v1/jobs/{id} until the job is done (its result) or failed (throws its error). */
export async function waitForJob(job: JobOut): Promise<Record<string, unknown>> {
  const deadline = Date.now() + MAX_WAIT_MS;
  let j = job;
  while (j.status === "queued" || j.status === "running") {
    if (Date.now() > deadline) throw new Error("still running after an hour; check back later");
    await new Promise((r) => setTimeout(r, POLL_MS));
    j = await api.get<JobOut>(`/v1/jobs/${j.id}`);
  }
  if (j.status === "failed") throw new Error(j.error || "the job failed");
  return j.result ?? {};
}

/** A queued source.sync job's report; throws the sync's error when it failed. */
export async function syncReportOf(job: JobOut): Promise<SyncReportOut> {
  const r = await waitForJob(job);
  if (r.outcome === "failed" || r.outcome === "gone" || !r.report) {
    throw new Error(String(r.error || (r.outcome === "gone" ? "the source was removed" : "sync failed")));
  }
  return r.report as SyncReportOut;
}

/** A queued connection.crawl job's report. */
export async function crawlReportOf(job: JobOut): Promise<CrawlOut> {
  return (await waitForJob(job)) as unknown as CrawlOut;
}
