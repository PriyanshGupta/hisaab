// Recurring charges (PRD §7.8; slice 21): charges that come back at a steady rhythm, as their own section.
// Each row says who, how often, from which account, what it costs now (and what it used to), when it was
// last charged and when the next one is due, and roughly what a year of it costs. No chart: a list of
// facts is the honest form for a handful of standing charges (dataviz).
import { type RecurringCharge, useRecurring } from "../api/report";
import { formatDate } from "../dates";
import { formatPaise } from "../money";
import { indiaToday } from "./period";

const RHYTHMS: [number, number, string][] = [
  [6, 8, "Every week"], [13, 15, "Every 2 weeks"], [27, 33, "Every month"], [57, 63, "Every 2 months"],
  [85, 95, "Every 3 months"], [175, 190, "Every 6 months"], [355, 375, "Every year"],
];

function rhythm(days: number): string {
  return RHYTHMS.find(([lo, hi]) => lo <= days && days <= hi)?.[2] ?? `Every ${days} days`;
}

/** "5 Oct" this year, "5 Oct 2025" otherwise. */
function short(iso: string): string {
  const full = formatDate(iso);
  return iso.slice(0, 4) === indiaToday().slice(0, 4) ? full.slice(0, -5) : full;
}

/** About a year's cost, in whole rupees: an estimate, so no paise. */
const aboutRupees = (paise: number) => formatPaise(Math.round(paise / 100) * 100).replace(/\.00$/, "");

export function Recurring() {
  const recurring = useRecurring();
  if (!recurring.data) return null;
  return (
    <section className="sheet sheet--ruled moved recurring" aria-labelledby="recurring-h">
      <div className="moved__head">
        <h2 id="recurring-h">Recurring charges</h2>
        <p className="muted">Charges that come back at a steady rhythm</p>
      </div>
      {recurring.data.length === 0
        ? <p className="muted">No recurring charges yet. They show up after three charges at a steady rhythm.</p>
        : <ul className="recurring__list">{recurring.data.map((r) => <Row key={`${r.payee}-${r.latest_paise}-${r.last_on}`} r={r} />)}</ul>}
    </section>
  );
}

function Row({ r }: { r: RecurringCharge }) {
  const how = [rhythm(r.typical_gap_days), r.account ? `on ${r.account}` : null, r.category].filter(Boolean).join(", ");
  const changed = r.latest_paise !== r.first_paise;
  return (
    <li className="frow recurring__row">
      <span>
        <span className="recurring__payee">{r.payee}</span>
        <span className="frow__sub">{how}</span>
        <span className="frow__sub">Last on {short(r.last_on)}, next around {short(r.next_on)}</span>
      </span>
      <span className="recurring__money">
        <span className="num">
          {formatPaise(r.latest_paise)}
          {changed && `, ${r.latest_paise > r.first_paise ? "up" : "down"} from ${formatPaise(r.first_paise)}`}
        </span>
        <span className="frow__sub">about {aboutRupees(r.yearly_paise)} a year</span>
      </span>
    </li>
  );
}
