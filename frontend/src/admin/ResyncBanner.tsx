// Parser-change notices (tech-design §5.2): when a template is edited, approved or updated with the app, the
// emails it read may now read differently. The banner names the template, when, and the date range, and offers
// the re-sync of exactly that range (which still asks for confirmation) or Dismiss. It is the one thing on the
// admin pages asking for action, so it carries the red margin rule.
import { Link } from "react-router";

import { describeError } from "../api/client";
import { type ResyncNotice, useDismissNotice, useResyncNotices } from "../api/resync";
import { formatDate, formatInstantDate } from "../dates";

const VERB: Record<string, string> = { edited: "was edited", approved: "was approved", shipped_updated: "was updated with the app" };

export function ResyncBanner() {
  const notices = useResyncNotices();
  const dismiss = useDismissNotice();
  if (!notices.data || notices.data.length === 0) return null;
  return (
    <section className="resync-banner" aria-labelledby="resync-banner-h">
      <h2 id="resync-banner-h" className="visually-hidden">Parser changes</h2>
      <ul>
        {notices.data.map((n: ResyncNotice) => (
          <li key={n.id} className="resync-banner__item">
            <p>
              {n.template_name} {VERB[n.reason] ?? "changed"} on {formatInstantDate(n.created_at)}. Emails from{" "}
              {formatDate(n.range_from)} to {formatDate(n.range_to)} may now read differently.
            </p>
            <span className="resync-banner__actions">
              <Link className="button button--primary" to={`/admin/resync?from=${n.range_from}&to=${n.range_to}`}>
                Re-sync that range
              </Link>
              <button className="button button--quiet" type="button" disabled={dismiss.isPending}
                onClick={() => dismiss.mutate(n.id)}>Dismiss</button>
            </span>
          </li>
        ))}
      </ul>
      {dismiss.isError && <p className="form-error" role="alert">{describeError(dismiss.error)}</p>}
    </section>
  );
}
