// Sender review (PRD §6.10, tech-design §5.3; slice 23). Admin only.
//
// The weekly sweep looks for bank-like words in mail from senders Hisaab doesn't fetch yet. Each one it finds
// waits here with what the sweep saw, per inbox: subjects and Gmail's short previews, never whole emails. That
// excerpt is the page's one bold element, set like a slice of an inbox, because it is what tells a bank from
// a newsletter. Senders are shared by everyone: approving one fetches its mail for every inbox, its last three
// months included, which is why each candidate says whose inbox it came from. Ignoring keeps it out of every
// future sweep.
import { useState } from "react";

import { describeError } from "../api/client";
import {
  type CandidateSender,
  type FoundIn,
  useApproveSender,
  useIgnoreSender,
  useRunSweep,
  useSenderQueue,
} from "../api/senders";
import { formatInstantDate } from "../dates";
import "../styles/senders.css";

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

export function SendersPage() {
  const queue = useSenderQueue();
  const sweep = useRunSweep();
  const [done, setDone] = useState<string | null>(null);

  const runSweep = () => sweep.mutate(undefined, {
    onSuccess: (n) => setDone(n === 0
      ? "No inbox is connected, so there is nothing to sweep."
      : `Sweeping ${plural(n, "inbox", "inboxes")}. New senders appear here when it finishes.`),
  });

  return (
    <div className="senders">
      <header className="page-head">
        <h1 className="page-title">Senders</h1>
        <button className="button button--quiet" type="button" onClick={runSweep} disabled={sweep.isPending}>
          Run sweep now
        </button>
      </header>
      <p className="senders__lead">
        Once a week Hisaab looks for bank-like emails from senders it doesn't read yet. Approving a sender reads
        its mail for everyone from now on, and fetches its last three months.
        {queue.data && (queue.data.last_sweep_at
          ? ` Last sweep ${formatInstantDate(queue.data.last_sweep_at)}.`
          : " Not swept yet.")}
      </p>
      {done && <p className="senders__done" role="status">{done}</p>}
      {sweep.isError && <p className="form-error" role="alert">{describeError(sweep.error)}</p>}
      {queue.isError && <p className="form-error" role="alert">{describeError(queue.error)}</p>}
      {queue.data && (queue.data.candidates.length === 0
        ? <div className="empty">
            <p className="empty__lead">No new senders.</p>
            <p className="empty__body">The weekly sweep looks for bank-like emails from senders Hisaab doesn't know yet.</p>
          </div>
        : queue.data.candidates.map((c) => <SenderSheet key={c.id} c={c} onApproved={setDone} />))}
      {queue.data && queue.data.ignored.length > 0 && (
        <section className="senders__ignored" aria-labelledby="ignored-h">
          <h2 id="ignored-h">Ignored</h2>
          <p className="muted">The sweep never shows these again.</p>
          <ul>
            {queue.data.ignored.map((x) => (
              <li key={x.id}>
                <span className="senders__address">{x.address}</span>
                {x.reviewed_at && <span className="muted"> since {formatInstantDate(x.reviewed_at)}</span>}
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

function SenderSheet({ c, onApproved }: { c: CandidateSender; onApproved: (sentence: string) => void }) {
  const approve = useApproveSender();
  const ignore = useIgnoreSender();
  const headId = `sender-${c.id}`;
  const busy = approve.isPending || ignore.isPending;
  return (
    <section className="sheet sheet--ruled sender" aria-labelledby={headId}>
      <h2 id={headId} className="sender__address">{c.address}</h2>
      {c.found_in.map((f) => <Inbox key={f.user} f={f} />)}
      <div className="actions sender__actions">
        <button className="button button--primary" type="button" disabled={busy}
          onClick={() => approve.mutate(c.id, { onSuccess: (r) => onApproved(
            `Approved ${r.address}. Its last three months of mail are being fetched for ${plural(r.backfills, "inbox", "inboxes")}.`) })}>
          Approve
        </button>
        <button className="button button--quiet" type="button" disabled={busy} onClick={() => ignore.mutate(c.id)}>
          Ignore
        </button>
      </div>
      {(approve.isError || ignore.isError) && (
        <p className="form-error" role="alert">{describeError(approve.error ?? ignore.error)}</p>
      )}
    </section>
  );
}

function Inbox({ f }: { f: FoundIn }) {
  return (
    <div className="sender__inbox">
      <p className="sender__where">
        Found in {f.user}'s inbox, seen in {plural(f.match_count, "matching email", "matching emails")}
      </p>
      <ul className="sender__mail">
        {f.examples.map((e, i) => (
          <li key={i}>
            <span className="sender__subject">{e.subject || "(no subject)"}</span>
            <span className="sender__when">{formatInstantDate(e.received_at)}</span>
            <span className="sender__preview">{e.snippet}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
