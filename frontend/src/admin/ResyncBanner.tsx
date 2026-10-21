// Parser-change notices (tech-design §5.2): when a template is edited, approved or updated with the app, the
// emails it read may now read differently. The banner names the template, when, and the date range, and offers
// a re-sync of exactly that range for every inbox (templates are shared, so everyone's mail is read again), asked
// in place with how many transactions may change, or Dismiss. It is the one thing on the admin pages asking for
// action, so it carries the red margin rule. (A person's own date-range sync is in Settings.)
import { useState } from "react";

import { describeError } from "../api/client";
import { type ResyncNotice, useDismissNotice, useResyncNotices, useResyncPreview, useStartResync } from "../api/resync";
import { formatDate, formatInstantDate } from "../dates";
import { Icon } from "../ui/Icon";

const VERB: Record<string, string> = { edited: "was edited", approved: "was approved", shipped_updated: "was updated with the app" };
const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

export function ResyncBanner() {
  const notices = useResyncNotices();
  const dismiss = useDismissNotice();
  const preview = useResyncPreview();
  const run = useStartResync();
  const [asking, setAsking] = useState<{ id: string; transactions: number } | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const open = notices.data ?? [];
  if (open.length === 0 && !done) return null;

  const ask = (n: ResyncNotice) => {
    setDone(null);
    preview.mutate({ start: n.range_from, end: n.range_to, user_id: null },
      { onSuccess: (p) => setAsking({ id: n.id, transactions: p.transactions }) });
  };
  const confirm = (n: ResyncNotice) => run.mutate({ start: n.range_from, end: n.range_to, user_id: null }, {
    onSuccess: (count) => {
      setAsking(null);
      setDone(count === 0 ? "No connected inbox to re-sync."
        : `Re-sync started for ${plural(count, "inbox", "inboxes")}. Changes appear as each one finishes.`);
    } });

  return (
    <section className="resync-banner" aria-labelledby="resync-banner-h">
      <h2 id="resync-banner-h" className="visually-hidden">Parser changes</h2>
      {done && <p className="settings-note resync-banner__done" role="status">{done}</p>}
      <ul>
        {open.map((n: ResyncNotice) => (
          <li key={n.id} className="resync-banner__item">
            <Icon name="refresh" size={18} className="resync-banner__icon" />
            <p>
              {n.template_name} {VERB[n.reason] ?? "changed"} on {formatInstantDate(n.created_at)}. Emails from{" "}
              {formatDate(n.range_from)} to {formatDate(n.range_to)} may now read differently.
            </p>
            {asking?.id === n.id ? (
              <div className="resync-banner__ask" role="group" aria-labelledby={`resync-ask-${n.id}`}>
                <p id={`resync-ask-${n.id}`} className="resync-banner__question">
                  Re-sync {formatDate(n.range_from)} to {formatDate(n.range_to)} for every inbox?
                </p>
                <p>{plural(asking.transactions, "transaction", "transactions")} may change. A backup is taken first.</p>
                <span className="resync-banner__actions">
                  <button className="button button--primary button--small" type="button" disabled={run.isPending}
                    onClick={() => confirm(n)}>Re-sync</button>
                  <button className="button button--secondary button--small" type="button"
                    onClick={() => setAsking(null)}>Cancel</button>
                </span>
              </div>
            ) : (
              <span className="resync-banner__actions">
                <button className="button button--primary button--small" type="button" disabled={preview.isPending}
                  onClick={() => ask(n)}>Re-sync that range</button>
                <button className="button button--secondary button--small" type="button" disabled={dismiss.isPending}
                  onClick={() => dismiss.mutate(n.id)}>Dismiss</button>
              </span>
            )}
          </li>
        ))}
      </ul>
      {[dismiss, preview, run].map((m, i) => m.isError && (
        <p key={i} className="form-error" role="alert">{describeError(m.error)}</p>
      ))}
    </section>
  );
}
