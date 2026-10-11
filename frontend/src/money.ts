// Money in the browser: integer paise only, never floating point (PRD §10.3).
//
// "0.29 * 100" is 28.999999999999996 in JavaScript, so amounts typed as rupees
// are converted by splitting the *string* at the decimal point and combining
// two integers. Formatting does the reverse with integer division. Everything
// stays exact up to Number.MAX_SAFE_INTEGER paise (about ₹90 lakh crore).

export type ParseResult = { ok: true; paise: number } | { ok: false; error: string };

const AMOUNT = /^(-)?(\d+)(?:\.(\d*))?$/;
const MAX_RUPEE_DIGITS = 13; // ₹99,99,99,99,99,999.99, far beyond any household balance

/** "₹1,23,456.78" -> 12345678. Commas and ₹ are optional; at most 2 decimal places. */
export function parseRupees(input: string, options: { allowNegative?: boolean } = {}): ParseResult {
  const compact = input.replace(/[₹,\s]/g, "");
  if (compact === "") return { ok: false, error: "Enter an amount." };
  const match = AMOUNT.exec(compact);
  if (!match) return { ok: false, error: "Enter an amount like 47,000 or 47,000.50." };
  const [, minus, rupees = "0", fraction = ""] = match;
  if (fraction.length > 2) return { ok: false, error: "Use at most 2 decimal places." };
  if (rupees.replace(/^0+/, "").length > MAX_RUPEE_DIGITS) return { ok: false, error: "That amount is too large." };
  if (minus && !options.allowNegative) return { ok: false, error: "Enter a positive amount." };
  const paise = Number(rupees) * 100 + Number(fraction.padEnd(2, "0"));
  return { ok: true, paise: minus && paise !== 0 ? -paise : paise };
}

const groupIndian = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 });

function split(paise: number): { sign: string; rupees: string; fraction: string } {
  const abs = Math.abs(paise);
  return {
    sign: paise < 0 ? "−" : "",
    rupees: groupIndian.format(Math.trunc(abs / 100)),
    fraction: String(abs % 100).padStart(2, "0"),
  };
}

/** 12345678 -> "₹1,23,456.78" (Indian digit grouping; a true minus sign for negatives). */
export function formatPaise(paise: number): string {
  const { sign, rupees, fraction } = split(paise);
  return `${sign}₹${rupees}.${fraction}`;
}

/** 4700050 -> "47,000.50", for pre-filling an amount input; parseRupees reads it back. */
export function paiseToInput(paise: number): string {
  const { rupees, fraction } = split(paise);
  return `${paise < 0 ? "-" : ""}${rupees}.${fraction}`;
}
