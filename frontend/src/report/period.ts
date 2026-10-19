// Reporting periods as plain "YYYY-MM-DD" strings, both ends inclusive (tech-design §8.3: day, week,
// month, custom; always transaction_date; month boundaries in Asia/Kolkata). Arithmetic runs on UTC
// midnights so no local time zone can shift a day. Weeks run Monday to Sunday.
import { formatDate } from "../dates";

export type Unit = "day" | "week" | "month" | "custom";
export type Period = { unit: Unit; start: string; end: string };

const MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
  "October", "November", "December"];
const ISO = /^\d{4}-\d{2}-\d{2}$/;

const utc = (iso: string) => new Date(`${iso}T00:00:00Z`);
const iso = (d: Date) => d.toISOString().slice(0, 10);
const addDays = (day: string, n: number) => { const d = utc(day); d.setUTCDate(d.getUTCDate() + n); return iso(d); };
const valid = (s: string | null): s is string => s !== null && ISO.test(s) && iso(utc(s)) === s;

const indiaIsoDate = new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Kolkata", year: "numeric",
  month: "2-digit", day: "2-digit" });
export const indiaToday = (now: Date = new Date()) => indiaIsoDate.format(now);

export function periodAt(unit: Exclude<Unit, "custom">, day: string): Period {
  if (unit === "day") return { unit, start: day, end: day };
  if (unit === "week") {
    const start = addDays(day, -((utc(day).getUTCDay() + 6) % 7)); // back to Monday
    return { unit, start, end: addDays(start, 6) };
  }
  const d = utc(day);
  const last = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 0));
  return { unit, start: `${day.slice(0, 7)}-01`, end: iso(last) };
}

export function shift(p: Period, dir: -1 | 1): Period {
  if (p.unit === "custom") return p;
  if (p.unit === "month") {
    const d = utc(p.start);
    return periodAt("month", iso(new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + dir, 1))));
  }
  return periodAt(p.unit, addDays(p.start, dir * (p.unit === "week" ? 7 : 1)));
}

/** "5 Oct" + " 2026" pieces of formatDate, dropping what both ends share. */
export function periodTitle(p: Period): string {
  if (p.unit === "month") return `${MONTH_NAMES[Number(p.start.slice(5, 7)) - 1]} ${p.start.slice(0, 4)}`;
  if (p.start === p.end) return formatDate(p.start);
  const [a, b] = [formatDate(p.start), formatDate(p.end)];
  if (p.start.slice(0, 4) !== p.end.slice(0, 4)) return `${a} – ${b}`;
  if (p.start.slice(0, 7) !== p.end.slice(0, 7)) return `${a.slice(0, -5)} – ${b}`;
  return `${Number(p.start.slice(8))} – ${b}`;
}

export function toParams(p: Period): Record<string, string> {
  return p.unit === "custom" ? { unit: p.unit, from: p.start, to: p.end } : { unit: p.unit, from: p.start };
}

export function fromParams(params: URLSearchParams, today: string): Period {
  const unit = params.get("unit");
  const from = params.get("from");
  const to = params.get("to");
  if (unit === "custom" && valid(from) && valid(to) && from <= to) return { unit, start: from, end: to };
  if ((unit === "day" || unit === "week" || unit === "month") && valid(from)) return periodAt(unit, from);
  return periodAt("month", today);
}
