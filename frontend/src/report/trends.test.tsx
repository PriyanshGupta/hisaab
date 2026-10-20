import { fireEvent, screen, within } from "@testing-library/react";
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

function open(trends: unknown = TRENDS, recurring: unknown[] = [STREAMING, STORAGE]) {
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
  expect(food.getByRole("status")).toHaveTextContent("₹9,000.00Oct 2026, so far");
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
