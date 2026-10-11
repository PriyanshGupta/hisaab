// Calendar dates are plain "YYYY-MM-DD" strings everywhere (tech-design §3.1):
// local dates with no time zone. These helpers never go through Date parsing
// of the string, which would shift the day across time zones.

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "2026-07-11" -> "11 Jul 2026" */
export function formatDate(iso: string): string {
  const [year, month, day] = iso.slice(0, 10).split("-");
  return `${Number(day)} ${MONTHS[Number(month) - 1]} ${year}`;
}

/** Today's local date as "YYYY-MM-DD". */
export function todayIso(now: Date = new Date()): string {
  return toIso(now);
}

/**
 * The default opening-balance date: three months back, where the Gmail backfill
 * starts. An anchor dated today with history before it makes balances wrong
 * from day one (PRD §4.11).
 */
export function threeMonthsAgoIso(now: Date = new Date()): string {
  const d = new Date(now.getFullYear(), now.getMonth() - 3, now.getDate());
  return toIso(d);
}

function toIso(d: Date): string {
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${d.getFullYear()}-${mm}-${dd}`;
}
