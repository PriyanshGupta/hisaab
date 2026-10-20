// A statement's diagnostic (tech-design §4.8) for the person: one sentence, and the statement's own
// arithmetic as a ledger sum. The same record, in full, sits behind the preview's details toggle.
// Never inferred: the check compares the rows with the balances the statement itself prints, and
// when it prints none the sentence says so rather than staying silent (user, 2026-10-19).
import { formatPaise } from "../money";

export type UnparseableRow = { page: number; raw_cells: string[]; reason: string };
export type Diagnostic = {
  outcome: string; // passed | failed | unavailable
  template_name: string | null;
  pages_processed: number;
  rows_extracted: number;
  rows_skipped: number;
  rows_unparseable: number;
  declared: { opening: number | null; closing: number | null };
  computed: { credits: number; debits: number; closing: number | null };
  gap_paise: number | null; // declared closing − computed closing: positive means the rows come to less
  unparseable_rows: UnparseableRow[];
  tier?: number; // 3: read by the model (no template fitted)
  model?: string;
  institution?: string | null;
  template_tried?: { template: string; reason: string }; // a template recognised it but didn't fit
  declared_dropped?: string[]; // tier 3: balances Claude reported that aren't printed in the statement
};
export type Kind = "bank" | "credit_card";
export type SumLine = { label: string; paise: number; sign?: "+" | "−"; total?: boolean };

const rows = (n: number) => `${n} ${n === 1 ? "row" : "rows"}`;

function listPages(pages: number[]): string {
  const sorted = [...new Set(pages)].sort((a, b) => a - b);
  if (sorted.length === 1) return `page ${sorted[0]}`;
  return `pages ${sorted.slice(0, -1).join(", ")} and ${sorted[sorted.length - 1]}`;
}

export function describeCheck(d: Diagnostic, kind: Kind): string {
  const closingName = kind === "credit_card" ? "amount due" : "closing balance";
  let sentence: string;
  if (d.outcome === "passed") {
    sentence = `Parsed ${rows(d.rows_extracted)}. ${d.rows_extracted === 1 ? "It adds" : "They add"} up to the ` +
      `statement's ${closingName} of ${formatPaise(d.declared.closing ?? 0)}.`;
  } else if (d.outcome === "failed") {
    const gap = d.gap_paise ?? 0;
    sentence = `Parsed ${rows(d.rows_extracted)}, but they come to ${formatPaise(Math.abs(gap))} ` +
      `${gap > 0 ? "less" : "more"} than the statement's own ${closingName}.`;
  } else if (d.declared_dropped?.length) {
    sentence = `Parsed ${rows(d.rows_extracted)}. Claude reported an opening and closing balance that Hisaab ` +
      "couldn't find printed in the statement, so the rows couldn't be checked against them.";
  } else {
    sentence = `Parsed ${rows(d.rows_extracted)}. This statement doesn't print an opening and closing balance ` +
      "Hisaab can read, so the rows couldn't be checked against it.";
  }
  if (d.rows_unparseable > 0) {
    sentence += ` ${rows(d.rows_unparseable)} couldn't be read (${listPages(d.unparseable_rows.map((r) => r.page))}).`;
  }
  return sentence;
}

/** The check as a column sum; empty when the statement prints no balances to sum against. */
export function checkLines(d: Diagnostic, kind: Kind): SumLine[] {
  const { opening, closing } = d.declared;
  if (d.outcome === "unavailable" || opening === null || closing === null || d.computed.closing === null) return [];
  const card = kind === "credit_card"; // debt is positive: spending adds, payments take away
  const lines: SumLine[] = [
    { label: card ? "Owed at the start" : "Opening balance", paise: opening },
    { label: card ? "Spent" : "Money in", paise: card ? d.computed.debits : d.computed.credits, sign: "+" },
    { label: card ? "Paid and refunded" : "Money out", paise: card ? d.computed.credits : d.computed.debits, sign: "−" },
    { label: "The rows come to", paise: d.computed.closing, total: true },
    { label: "The statement says", paise: closing },
  ];
  if (d.outcome === "failed") lines.push({ label: "Gap", paise: Math.abs(d.gap_paise ?? 0) });
  return lines;
}

/** How the statement was read, for the preview's file line. */
export function readBy(d: Diagnostic, templateName: string | null): string {
  if (d.tier !== 3) return `read as “${templateName ?? "unknown layout"}”`;
  if (d.template_tried) return `read by Claude because the “${d.template_tried.template}” layout didn't fit (${d.template_tried.reason})`;
  return "read by Claude because no known layout matched";
}
