import { fireEvent, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import type { Report } from "../api/report";
import { mockApi } from "../test/mockApi";
import { renderApp } from "../test/renderApp";

const ME = { status: 200, body: { id: "u1", username: "asha", display_name: "Asha Kulkarni", role: "member" } };
const SETUP_DONE = { status: 200, body: { step: "done", backfill_start: "2026-07-11", job: null } };
const NO_REVIEW = { status: 200, body: { items: [] as unknown[], categorized: [], sorted_accounts: [], sorted_payees: [] } };

const leaf = (id: string, amount: number, extra: Partial<Report["categories"][0]["payees"][0]["transactions"][0]> = {}) => ({
  id, transaction_date: "2026-10-05", amount_paise: amount, kind: amount < 0 ? "refund" : "expense",
  account_label: "Card", payee_raw: null, provenance: "email" as const, needs_review: false, ...extra });

const REPORT: Report = {
  start: "2026-10-01", end: "2026-10-31",
  spending_paise: 1_500_00 + 25_000_00 - 500_00 + 3_000_00, cash_out_paise: 31_000_00, cash_out_unknown_paise: 0,
  money_in_paise: 90_000_00,
  categories: [
    { category_id: "c-food", name: "Food", amount_paise: 25_000_00 - 500_00, count: 3, payees: [
      { key: "p-cafe", name: "Cafe", amount_paise: 24_500_00, transactions: [leaf("t1", 25_000_00), leaf("t2", -500_00)] }] },
    { category_id: "c-home", name: "Home", amount_paise: 1_500_00, count: 1, payees: [
      { key: "p-shop", name: "Shop", amount_paise: 1_500_00, transactions: [leaf("t3", 1_500_00, { provenance: "manual", account_label: "Wallet" })] }] },
    { category_id: null, name: "Not categorized yet", amount_paise: 3_000_00, count: 2, payees: [] },
  ],
  income: [{ key: "p-emp", name: "Employer", amount_paise: 90_000_00, transactions: [] }],
  transfers: [{ id: "x1", transaction_date: "2026-10-10", amount_paise: 28_000_00, kind: "card_bill", from_label: "Bank", to_label: "Card" }],
  reversed: [],
};

beforeEach(() => { vi.useFakeTimers({ toFake: ["Date"] }); vi.setSystemTime(new Date("2026-10-19T06:00:00Z")); });
afterEach(() => { vi.useRealTimers(); });

function open(report: Report = REPORT, review = NO_REVIEW) {
  const api = mockApi({ "GET /api/auth/me": ME, "GET /api/setup": SETUP_DONE,
    "GET /api/report": { status: 200, body: report }, "GET /api/review": review,
    "GET /api/balances": { status: 200, body: [] } });
  return { api, ...renderApp("/") };
}

test("opens on this month, asking for the 1st to the 31st", async () => {
  const { api } = open();
  expect(await screen.findByRole("heading", { level: 1, name: "October 2026" })).toBeInTheDocument();
  expect(api.urls.some((u) => u.includes("/api/report?start=2026-10-01&end=2026-10-31"))).toBe(true);
});

test("the toggle changes only the total; every category stays put", async () => {
  open();
  const hero = await screen.findByTestId("hero-total");
  expect(hero).toHaveTextContent("₹29,000.00");
  const before = screen.getByRole("list", { name: "Spending by category" }).textContent;
  await userEvent.click(screen.getByRole("button", { name: "Cash out" }));
  expect(hero).toHaveTextContent("₹31,000.00");
  expect(screen.getByRole("list", { name: "Spending by category" }).textContent).toBe(before);
});

test("the uncategorized line is there and leads to the review queue", async () => {
  open();
  const row = (await screen.findByText("Not categorized yet")).closest("li")!;
  expect(within(row).getByText("₹3,000.00")).toBeInTheDocument();
  expect(within(row).getByRole("link", { name: "Sort 2 payments" })).toHaveAttribute("href", "/review?lane=categories");
});

test("an empty month shows zero and the uncategorized line", async () => {
  open({ ...REPORT, spending_paise: 0, cash_out_paise: 0, money_in_paise: 0, income: [], transfers: [],
    categories: [{ category_id: null, name: "Not categorized yet", amount_paise: 0, count: 0, payees: [] }] });
  expect(await screen.findByTestId("hero-total")).toHaveTextContent("₹0.00");
  const row = screen.getByText("Not categorized yet").closest("li")!;
  expect(within(row).getByText("All sorted")).toBeInTheDocument();
});

test("drills from category to payee to transaction, refund shown as a minus", async () => {
  open();
  await userEvent.click(await screen.findByRole("button", { name: /Food/ }));
  await userEvent.click(screen.getByRole("button", { name: /Cafe/ }));
  const txns = screen.getByRole("list", { name: "Cafe payments" });
  expect(within(txns).getByText("−₹500.00")).toBeInTheDocument();
  expect(within(txns).getAllByText("From an email").length).toBe(2);
});

test("a card bill payment is listed under money moved, not as spending", async () => {
  open();
  const moved = await screen.findByRole("region", { name: "Money in and moved" });
  expect(within(moved).getByText("Card bill paid")).toBeInTheDocument();
  expect(within(moved).getByText("₹28,000.00")).toBeInTheDocument();
  expect(screen.getByRole("list", { name: "Spending by category" })).not.toHaveTextContent("28,000");
});

test("the pending badge shows what the review queue has left and opens it", async () => {
  const item = { id: "t9", tags: ["uncategorized"] };
  open(REPORT, { status: 200, body: { ...NO_REVIEW.body, items: [item, { id: "t8", tags: ["payee"] }] } });
  const pill = await screen.findByRole("link", { name: "1 waiting for review" }); // the optional Payees lane isn't counted
  expect(pill).toHaveAttribute("href", "/review");
});

test("stepping back asks for September", async () => {
  const { api } = open();
  await userEvent.click(await screen.findByRole("button", { name: "Previous month" }));
  expect(await screen.findByRole("heading", { level: 1, name: "September 2026" })).toBeInTheDocument();
  expect(api.urls.some((u) => u.includes("start=2026-09-01&end=2026-09-30"))).toBe(true);
});

test("switching to week asks for Monday to Sunday", async () => {
  const { api } = open();
  await userEvent.click(await screen.findByRole("button", { name: "Week" }));
  expect(await screen.findByRole("heading", { level: 1, name: "19 – 25 Oct 2026" })).toBeInTheDocument();
  expect(api.urls.some((u) => u.includes("start=2026-10-19&end=2026-10-25"))).toBe(true);
});

function openAt(path: string) {
  const api = mockApi({ "GET /api/auth/me": ME, "GET /api/setup": SETUP_DONE,
    "GET /api/report": { status: 200, body: REPORT }, "GET /api/review": NO_REVIEW });
  return { api, ...renderApp(path) };
}

test("typing a custom range never leaves custom mode or asks for a half-typed date", async () => {
  const { api } = openAt("/?unit=custom&from=2026-09-01&to=2026-09-15");
  const to = await screen.findByLabelText("to");
  fireEvent.change(to, { target: { value: "2026-08-01" } }); // before "from": not a range yet
  fireEvent.change(screen.getByLabelText("From"), { target: { value: "0002-09-01" } }); // a year mid-typing
  expect(screen.getByRole("heading", { level: 1, name: "1 – 15 Sep 2026" })).toBeInTheDocument();
  expect(screen.getByLabelText("to")).toBeInTheDocument();
  expect(api.urls.some((u) => u.includes("end=2026-08-01") || u.includes("start=0002"))).toBe(false);
  fireEvent.change(screen.getByLabelText("From"), { target: { value: "2026-09-01" } }); // the year finished
  fireEvent.change(screen.getByLabelText("to"), { target: { value: "2026-09-20" } });
  expect(await screen.findByRole("heading", { level: 1, name: "1 – 20 Sep 2026" })).toBeInTheDocument();
  expect(api.urls.some((u) => u.includes("start=2026-09-01&end=2026-09-20"))).toBe(true);
});

test("cash out says how much has no account yet, and where to assign it", async () => {
  open({ ...REPORT, cash_out_unknown_paise: 600_00 });
  await userEvent.click(await screen.findByRole("button", { name: "Cash out" }));
  expect(screen.getByText(/₹600\.00 has no account yet, so it isn’t in cash out/)).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Assign accounts" })).toHaveAttribute("href", "/review?lane=accounts");
});
