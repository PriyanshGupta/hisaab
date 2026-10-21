// How a category moved against the period before, for the report's "vs Sep" column: a short mark for the eye
// ("+₹1,205", "−₹300", "new", "same") and the sentence a screen reader says instead. Whole rupees: the exact
// figure is the column beside it. Under a rupee either way counts as the same.
import { formatPaise } from "../money";

const rupees = (paise: number) => formatPaise(Math.round(Math.abs(paise) / 100) * 100).replace(/\.00$/, "");

export function describeChange(now: number, before: number, against: string): { mark: string; said: string } {
  const diff = now - before;
  if (before === 0 && now > 0) return { mark: "new", said: `none in ${against}` };
  if (Math.abs(diff) < 100) return { mark: "same", said: `the same as ${against}` };
  return { mark: `${diff > 0 ? "+" : "−"}${rupees(diff)}`, said: `${rupees(diff)} ${diff > 0 ? "more" : "less"} than ${against}` };
}
