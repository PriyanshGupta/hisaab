import { type Diagnostic, checkLines, describeCheck } from "./diagnostic";

const base: Diagnostic = {
  outcome: "passed", template_name: "Example", pages_processed: 4, rows_extracted: 118, rows_skipped: 9,
  rows_unparseable: 0, declared: { opening: 12450000, closing: 9830000 },
  computed: { credits: 4500000, debits: 7120000, closing: 9830000 }, gap_paise: 0, unparseable_rows: [],
};
const row = (page: number) => ({ page, raw_cells: ["14/08/26", "NEFT CR", "", "2,000.00"], reason: "row has no date" });

test("passed says the rows add up to the closing balance", () => {
  expect(describeCheck(base, "bank")).toBe("Parsed 118 rows. They add up to the statement's closing balance of ₹98,300.00.");
});

test("failed says how far off, and which way", () => {
  const less = { ...base, outcome: "failed", gap_paise: 200000, rows_unparseable: 2, unparseable_rows: [row(3), row(3)] };
  expect(describeCheck(less, "bank")).toBe(
    "Parsed 118 rows, but they come to ₹2,000.00 less than the statement's own closing balance. " +
    "2 rows couldn't be read (page 3).");
  expect(describeCheck({ ...base, outcome: "failed", gap_paise: -5050 }, "bank")).toBe(
    "Parsed 118 rows, but they come to ₹50.50 more than the statement's own closing balance.");
});

test("unavailable says the rows could not be checked, never nothing", () => {
  const none = { ...base, outcome: "unavailable", declared: { opening: null, closing: null }, gap_paise: null,
    rows_extracted: 1, rows_unparseable: 1, unparseable_rows: [row(2)] };
  expect(describeCheck(none, "bank")).toBe(
    "Parsed 1 row. This statement doesn't print an opening and closing balance Hisaab can read, " +
    "so the rows couldn't be checked against it. 1 row couldn't be read (page 2).");
});

test("pages are listed in order, once each", () => {
  const many = { ...base, rows_unparseable: 4, unparseable_rows: [row(7), row(3), row(4), row(3)] };
  expect(describeCheck(many, "bank")).toMatch(/4 rows couldn't be read \(pages 3, 4 and 7\)\.$/);
});

test("a card statement speaks of the amount due", () => {
  expect(describeCheck(base, "credit_card")).toBe("Parsed 118 rows. They add up to the statement's amount due of ₹98,300.00.");
});

test("the sum: bank adds money in, card adds spending", () => {
  expect(checkLines(base, "bank")).toEqual([
    { label: "Opening balance", paise: 12450000 },
    { label: "Money in", paise: 4500000, sign: "+" },
    { label: "Money out", paise: 7120000, sign: "−" },
    { label: "The rows come to", paise: 9830000, total: true },
    { label: "The statement says", paise: 9830000 },
  ]);
  expect(checkLines({ ...base, outcome: "failed", gap_paise: 200000 }, "credit_card").map((l) => l.label)).toEqual([
    "Owed at the start", "Spent", "Paid and refunded", "The rows come to", "The statement says", "Gap"]);
  expect(checkLines({ ...base, outcome: "unavailable" }, "bank")).toEqual([]);
});
