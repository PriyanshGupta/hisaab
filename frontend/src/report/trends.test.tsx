import { cleanup, fireEvent, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { mockApi } from "../test/mockApi";
import { renderApp } from "../test/renderApp";

const ME = { status: 200, body: { id: "u1", username: "asha", display_name: "Asha", role: "member" } };
const SETUP_DONE = { status: 200, body: { step: "done", backfill_start: "2026-07-11", job: null } };
const NO_REVIEW = { status: 200, body: { items: [], categorized: [], sorted_accounts: [], sorted_payees: [] } };
const REPORT = { status: 200, body: { start: "2026-10-01", end: "2026-10-31", spending_paise: 0, cash_out_paise: 0,
  cash_out_unknown_paise: 0, money_in_paise: 0, income: [], transfers: [], reversed: [],
  categories: [{ category_id: null, name: "Not categorized yet", amount_paise: 0, count: 0, payees: [] }] } };
const MONTHS = ["2025-11", "2025-12", "2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06", "2026-07",
  "2026-08", "2026-09", "2026-10"];
const before = Array(8).fill(null) as null[];
const TRENDS = { months: MONTHS, history_from: "2026-07-12", partial: ["2026-07", "2026-10"], categories: [
  { category_id: "c-rent", name: "Rent", months: [...before, 2_400_000, 2_400_000, 2_400_000, 2_400_000], total_paise: 9_600_000 },
  { category_id: "c-food", name: "Food", months: [...before, null, null, 1_824_000, 900_000], total_paise: 2_724_000 },
  { category_id: "c-gift", name: "Gifts", months: [...before, null, null, null, 50_000], total_paise: 50_000 },
  { category_id: "c-shop", name: "Shopping", months: [...before, 500_000, -120_000, 0, 0], total_paise: 380_000 },
  { category_id: "c-ret", name: "Returns", months: [...before, 0, -150_000, 0, 0], total_paise: -150_000 },
  { category_id: null, name: "Not categorized yet", months: [...before, 10_000, 0, 0, 0], total_paise: 10_000 },
] };
const STREAMING = { payee: "Streamflix", category: "Entertainment", account: "Rewards Card", count: 4, typical_gap_days: 30,
  latest_paise: 55_500, first_paise: 55_500, last_on: "2026-10-05", next_on: "2026-11-04", yearly_paise: 666_000 };
const STORAGE = { ...STREAMING, payee: "Cloud Drive", category: null, latest_paise: 19_300, first_paise: 17_500,
  yearly_paise: 77_200, typical_gap_days: 91, last_on: "2026-09-30", next_on: "2026-12-30" };

beforeEach(() => { vi.useFakeTimers({ toFake: ["Date"] }); vi.setSystemTime(new Date("2026-10-19T06:00:00Z")); });
afterEach(() => { vi.useRealTimers(); });

const VIEW_KEY = "hisaab.report.trendsView";

/** The chart-per-category view (the tests written for it); `openOne` is the default, one chart for everything. */
function open(trends: unknown = TRENDS, recurring: unknown[] = [STREAMING, STORAGE]) {
  localStorage.setItem(VIEW_KEY, "each");
  render(trends, recurring);
}

function openOne(trends: unknown = TRENDS, recurring: unknown[] = [STREAMING, STORAGE]) {
  localStorage.removeItem(VIEW_KEY);
  render(trends, recurring);
}

function render(trends: unknown, recurring: unknown[]) {
  mockApi({ "GET /api/auth/me": ME, "GET /api/setup": SETUP_DONE, "GET /api/report": REPORT, "GET /api/review": NO_REVIEW,
    "GET /api/balances": { status: 200, body: [] }, "GET /api/trends": { status: 200, body: trends },
    "GET /api/recurring": { status: 200, body: recurring } });
  renderApp("/");
}

const panel = async (name: string) => within(await screen.findByRole("figure", { name }));

test("one chart per category, in order, each with its own scale, history start noted once", async () => {
  open();
  const section = await screen.findByRole("region", { name: "Last 12 months, by category" });
  expect(within(section).getAllByRole("figure").map((f) => f.getAttribute("aria-label")))
    .toEqual(["Rent", "Food", "Gifts", "Shopping", "Returns", "Not categorized yet"]);
  expect(within(section).getAllByText(/History starts Jul 2026/)).toHaveLength(1);
  expect((await panel("Rent")).getByText("₹30K")).toBeInTheDocument(); // its own top value
  expect((await panel("Food")).getByText("₹20K")).toBeInTheDocument();
});

test("two points draw a line; one point draws a dot and no line", async () => {
  open();
  const food = await panel("Food");
  expect(food.getByTestId("trend-line")).toHaveAttribute("data-points", "2");
  const gifts = await panel("Gifts");
  expect(gifts.queryByTestId("trend-line")).not.toBeInTheDocument();
  expect(gifts.getAllByTestId("trend-point")).toHaveLength(1);
});

test("the running month is hollow and says so far; the header shows the latest full month", async () => {
  open();
  const food = await panel("Food");
  const points = food.getAllByTestId("trend-point");
  expect(points.at(-1)).toHaveAttribute("data-partial", "true");
  expect(food.getByText("₹18,240.00", { selector: ".trend__value" })).toBeInTheDocument();
  expect(food.getByText("Sep", { selector: ".trend__when" })).toBeInTheDocument();
  fireEvent.keyDown(food.getByRole("img"), { key: "End" });
  expect(food.getByRole("status")).toHaveTextContent("₹9,000.00Oct, so far");
});

test("a refunds-only category scales below zero and keeps the zero line", async () => {
  open();
  const returns = await panel("Returns");
  expect(returns.getByText("−₹1.5K")).toBeInTheDocument(); // the scale reaches the negative month
  const zero = Number(returns.getByTestId("trend-zero").getAttribute("y1"));
  const lowest = Math.max(...returns.getAllByTestId("trend-point").map((p) => Number(p.getAttribute("cy"))));
  expect(lowest).toBeGreaterThan(zero); // drawn below the zero line, inside the plot
  expect(lowest).toBeLessThanOrEqual(82);
});

test("a tap on a phone keeps the reading on screen", async () => {
  open();
  const food = await panel("Food");
  const chart = food.getByRole("img");
  fireEvent.pointerDown(chart, { pointerType: "touch", clientX: 10 });
  fireEvent.pointerLeave(chart, { pointerType: "touch" });
  expect(food.getByRole("status")).toBeInTheDocument();
});

test("every chart has its values as a table", async () => {
  open();
  const table = (await panel("Food")).getByRole("table", { name: "Food by month" });
  expect(within(table).getByText("Sep 2026")).toBeInTheDocument();
  expect(within(table).getAllByText("No data yet").length).toBe(10);
});

test("recurring charges: rhythm, amount, change, next date and a yearly estimate", async () => {
  open();
  const section = await screen.findByRole("region", { name: "Recurring charges" });
  const [streaming, storage] = within(section).getAllByRole("listitem") as [HTMLElement, HTMLElement];
  expect(streaming).toHaveTextContent("Streamflix");
  expect(streaming).toHaveTextContent("Every month, on Rewards Card, Entertainment");
  expect(streaming).toHaveTextContent("Last on 5 Oct, next around 4 Nov");
  expect(streaming).toHaveTextContent("about ₹6,660 a year");
  expect(storage).toHaveTextContent("Every 3 months");
  expect(storage).toHaveTextContent("₹193.00, up from ₹175.00");
});

test("with no recurring charges and no spending, both sections say so", async () => {
  open({ months: MONTHS, history_from: null, partial: ["2026-10"], categories: [] }, []);
  expect(await screen.findByText("No recurring charges yet. They show up after three charges at a steady rhythm."))
    .toBeInTheDocument();
  expect(screen.getByText("No spending yet. Each category gets a chart once money goes out.")).toBeInTheDocument();
});


// ---- one chart for every category (user, 2026-10-21): the default; a legend names each colour ----

const allChart = async () => within(await screen.findByRole("figure", { name: "All categories" }));
const line = (chart: ReturnType<typeof within>, name: string) =>
  chart.getByTestId("trend-all-line-" + name);
const LETTERS = "ABCDEFGHIJ";
const MANY = { ...TRENDS, categories: [...LETTERS].map((l, i) => ({ category_id: "c-" + l, name: "Cat " + l,
  months: [...before, 1000 * (10 - i), 1000 * (10 - i), 1000 * (10 - i), 500], total_paise: 1000 * (10 - i) * 3 + 500 })) };

test("by default one chart draws every category as its own line, keyed in a legend", async () => {
  openOne();
  const section = await screen.findByRole("region", { name: "Last 12 months, by category" });
  expect(within(section).queryByRole("figure", { name: "Rent" })).not.toBeInTheDocument();
  const chart = await allChart();
  const rows = within(chart.getByRole("list", { name: "Categories" })).getAllByRole("button");
  expect(rows.map((r) => r.querySelector(".trend-all__name")?.textContent))
    .toEqual(["Rent", "Food", "Gifts", "Shopping", "Returns", "Not categorized yet"]);
  for (const r of rows) {  // each legend key wears its own line's colour slot
    const name = r.querySelector(".trend-all__name")?.textContent ?? "";
    expect(r.querySelector(".trend-all__key")?.getAttribute("data-slot")).toBe(line(chart, name).getAttribute("data-slot"));
  }
  expect(new Set(rows.map((r) => r.querySelector(".trend-all__key")?.getAttribute("data-slot"))).size).toBe(6);
  expect(within(section).getByText(/One scale for every category/)).toBeInTheDocument();
});

test("the legend reads the latest full month, and the running month is drawn dashed", async () => {
  openOne();
  const chart = await allChart();
  expect(chart.getByTestId("trend-all-month")).toHaveTextContent("Sep 2026");
  const rent = chart.getByRole("button", { name: /Rent/ });
  expect(rent).toHaveTextContent("₹24,000.00");
  expect(chart.getByRole("button", { name: /Gifts/ })).toHaveTextContent("No data");
  expect(chart.getByTestId("trend-all-partial-Food")).toBeInTheDocument();  // Oct, so far
});

test("hovering a legend entry lights its line and dims the rest; a click pins it", async () => {
  openOne();
  const chart = await allChart();
  const food = chart.getByRole("button", { name: /Food/ });
  fireEvent.pointerEnter(food);
  expect(line(chart, "Food")).toHaveClass("trend-all__line--lit");
  expect(line(chart, "Rent")).toHaveClass("trend-all__line--dim");
  fireEvent.pointerLeave(food);
  expect(line(chart, "Rent")).not.toHaveClass("trend-all__line--dim");
  fireEvent.click(food);
  expect(food).toHaveAttribute("aria-pressed", "true");
  expect(line(chart, "Rent")).toHaveClass("trend-all__line--dim");
  fireEvent.click(food);
  expect(food).toHaveAttribute("aria-pressed", "false");
  expect(line(chart, "Rent")).not.toHaveClass("trend-all__line--dim");
});

test("the arrow keys move the reading month by month", async () => {
  openOne();
  const chart = await allChart();
  const plot = chart.getByRole("img");
  fireEvent.focus(plot);
  expect(chart.getByTestId("trend-all-month")).toHaveTextContent("Oct 2026, so far");
  fireEvent.keyDown(plot, { key: "ArrowLeft" });
  expect(chart.getByTestId("trend-all-month")).toHaveTextContent("Sep 2026");
  expect(chart.getByRole("button", { name: /Food/ })).toHaveTextContent("₹18,240.00");
});

test("past eight categories, the rest share one grey line named for what it holds", async () => {
  openOne(MANY);
  const chart = await allChart();
  const names = within(chart.getByRole("list", { name: "Categories" })).getAllByRole("button")
    .map((r) => r.querySelector(".trend-all__name")?.textContent);
  expect(names).toEqual([..."ABCDEFGH"].map((l) => "Cat " + l).concat("Everything else"));
  const rest = chart.getByRole("button", { name: /Everything else/ });
  expect(rest).toHaveTextContent("Cat I, Cat J");
  expect(rest).toHaveTextContent("₹30.00");  // Sep: 20 + 10 rupees
  expect(rest.querySelector(".trend-all__key")).toHaveAttribute("data-slot", "rest");
});

test("the toggle shows a chart per category, and the choice is remembered", async () => {
  openOne();
  fireEvent.click(await screen.findByRole("button", { name: "Chart per category" }));
  expect(await screen.findByRole("figure", { name: "Rent" })).toBeInTheDocument();
  expect(screen.queryByRole("figure", { name: "All categories" })).not.toBeInTheDocument();
  expect(screen.getByText(/Each chart has its own scale/)).toBeInTheDocument();
  cleanup();
  render(TRENDS, [STREAMING, STORAGE]);  // a reload
  expect(await screen.findByRole("figure", { name: "Rent" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Chart per category" })).toHaveAttribute("aria-pressed", "true");
});

test("the one chart has every value as a table too", async () => {
  openOne();
  const table = (await allChart()).getByRole("table", { name: "Every category by month" });
  expect(within(table).getByRole("columnheader", { name: "Rent" })).toBeInTheDocument();
  expect(within(table).getAllByRole("row")).toHaveLength(13);  // a header row and 12 months
});
