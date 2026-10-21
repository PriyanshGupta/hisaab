// Shared by both trend views: month names and the scale's numbers.
const MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
export const monthShort = (ym: string) => MONTH_NAMES[Number(ym.slice(5, 7)) - 1] ?? ym;
export const monthLong = (ym: string) => `${monthShort(ym)} ${ym.slice(0, 4)}`;

const STEPS = [1, 1.5, 2, 3, 4, 5, 6, 8, 10];

/** The smallest "nice" number at or above `v` (in paise): 27,000 → 30,000; 18,240 → 20,000. */
export function nice(v: number): number {
  if (v <= 0) return 0;
  const power = 10 ** Math.floor(Math.log10(v));
  return (STEPS.find((s) => s * power >= v) ?? 10) * power;
}

/** ₹30K, ₹1.5K, ₹2.4L: a scale label, never a money figure the person reads as exact. */
export function compact(paise: number): string {
  const rupees = Math.abs(paise) / 100;
  const sign = paise < 0 ? "−" : "";
  const [div, unit] = rupees >= 1e7 ? [1e7, "Cr"] : rupees >= 1e5 ? [1e5, "L"] : rupees >= 1e3 ? [1e3, "K"] : [1, ""];
  const n = rupees / div;
  return `${sign}₹${Number.isInteger(n) ? n : n.toFixed(1).replace(/\.0$/, "")}${unit}`;
}
