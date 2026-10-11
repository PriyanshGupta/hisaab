// Settings (PRD §6.9): for now, the user's own Gmail connection (PRD §4.3).
// Connect sends the browser to Google; Google sends it back here with
// ?gmail=connected|denied|error. Sync now runs a background job and reports
// what it stored when it ends.
import { useState } from "react";
import { useSearchParams } from "react-router";

import { describeError } from "../api/client";
import { type GmailStatus, type Job, useConnectGmail, useGmailStatus, useJob, useSyncNow } from "../api/gmail";
import { formatInstantDate } from "../dates";
import { goTo } from "../navigate";
import "../styles/settings.css";

const RETURN_MESSAGES: Record<string, string> = {
  connected: "Gmail connected. The last 48 hours are syncing now.",
  denied: "Nothing was connected: access wasn't granted on Google's screen.",
  error: "Gmail couldn't be connected. Try again.",
};

export function SettingsPage() {
  const [params] = useSearchParams();
  const returned = RETURN_MESSAGES[params.get("gmail") ?? ""];
  const status = useGmailStatus();

  return (
    <>
      <header className="page-head"><h1 className="page-title">Settings</h1></header>
      <section className="settings-section" aria-labelledby="gmail-heading">
        <h2 id="gmail-heading" className="settings-section__title">Gmail</h2>
        {returned && <p className="settings-note">{returned}</p>}
        {status.isPending && <p className="muted">Loading…</p>}
        {status.isError && <p className="form-error" role="alert">{describeError(status.error)}</p>}
        {status.data && <GmailConnection status={status.data} />}
      </section>
    </>
  );
}

function GmailConnection({ status }: { status: GmailStatus }) {
  if (!status.configured) {
    return (
      <p className="muted">
        Gmail isn't set up on this server yet. The Google OAuth client and an encryption key need adding to
        the server's .env file.
      </p>
    );
  }
  if (status.status === "connected") return <Connected status={status} />;
  if (status.status === "needs_reauth") {
    return (
      <>
        <p className="form-error" role="alert">
          Gmail access for {status.email_address} has expired or was revoked. Reconnect to resume syncing;
          nothing synced so far is lost.
        </p>
        <ConnectButton label="Reconnect Gmail" />
      </>
    );
  }
  return (
    <>
      <p className="muted">Connect your Gmail so bank alerts arrive here on their own. Access is read-only.</p>
      <ConnectButton label="Connect Gmail" />
    </>
  );
}

export function ConnectButton({ label }: { label: string }) {
  const connect = useConnectGmail();
  return (
    <>
      <button className="button button--primary" type="button" disabled={connect.isPending}
        onClick={() => connect.mutate(undefined, { onSuccess: (url) => goTo(url) })}>
        {label}
      </button>
      {connect.isError && <p className="form-error" role="alert">{describeError(connect.error)}</p>}
    </>
  );
}

function Connected({ status }: { status: GmailStatus }) {
  const sync = useSyncNow();
  const [jobId, setJobId] = useState<string | null>(null);
  const job = useJob(jobId);
  const running = sync.isPending || (jobId !== null && (job.data === undefined || !isDone(job.data)));

  return (
    <>
      <p>
        Connected as <strong>{status.email_address}</strong>.{" "}
        <span className="muted">
          {status.last_successful_sync_at
            ? `Last synced ${formatInstantDate(status.last_successful_sync_at)}.`
            : "Not synced yet."}
        </span>
      </p>
      <button className="button button--primary" type="button" disabled={running}
        onClick={() => sync.mutate(undefined, { onSuccess: setJobId })}>
        Sync now
      </button>
      {running && jobId !== null && <p className="muted">Syncing…</p>}
      {sync.isError && <p className="form-error" role="alert">{describeError(sync.error)}</p>}
      {job.isError && <p className="form-error" role="alert">{describeError(job.error)}</p>}
      {job.data && isDone(job.data) && <SyncOutcome job={job.data} />}
    </>
  );
}

const isDone = (job: Job) => job.state !== "queued" && job.state !== "running";
const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

type SyncResult = { transactions_new?: number; needs_review?: number; duplicates?: number; already_had?: number;
  failed?: number; pending_llm?: number };

/** PRD §4.4: counts, not a spinner that vanishes. */
function SyncOutcome({ job }: { job: Job }) {
  if (job.state !== "succeeded") {
    return <p className="form-error" role="alert">{job.error ?? "The sync didn't finish."}</p>;
  }
  const r = (job.result ?? {}) as SyncResult;
  const [failed, waiting, already] = [r.failed ?? 0, r.pending_llm ?? 0, r.already_had ?? 0];
  const parts: string[] = [];
  if (failed > 0) parts.push(`${plural(failed, "email", "emails")} couldn't be fetched`);
  if (waiting > 0) parts.push(`${failed > 0 ? waiting : plural(waiting, "email", "emails")} ${waiting === 1 ? "is" : "are"} waiting to be read`);
  return (
    <p className="settings-note">
      {`${plural(r.transactions_new ?? 0, "new transaction", "new transactions")}, `}
      {`${r.needs_review ?? 0} ${(r.needs_review ?? 0) === 1 ? "needs" : "need"} review`}
      {(r.duplicates ?? 0) > 0 && `, ${plural(r.duplicates ?? 0, "duplicate", "duplicates")} skipped`}
      {"."}
      {already > 0 && parts.length === 0 && ` ${plural(already, "email was", "emails were")} already here.`}
      {parts.length > 0 && ` ${parts.join(" and ")}; the next sync retries them.`}
    </p>
  );
}
