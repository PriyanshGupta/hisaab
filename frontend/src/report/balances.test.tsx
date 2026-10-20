import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { mockApi, type Reply } from "../test/mockApi";
import { renderApp } from "../test/renderApp";

const ME = { status: 200, body: { id: "u1", username: "asha", display_name: "Asha", role: "member" } };
const SETUP_DONE = { status: 200, body: { step: "done", backfill_start: "2026-07-11", job: null } };
const NO_REVIEW = { status: 200, body: { items: [], categorized: [], sorted_accounts: [], sorted_payees: [] } };
const REPORT = { status: 200, body: { start: "2026-10-01", end: "2026-10-31", spending_paise: 0, cash_out_paise: 0,
  cash_out_unknown_paise: 0, money_in_paise: 0, income: [], transfers: [], reversed: [],
  categories: [{ category_id: null, name: "Not categorized yet", amount_paise: 0, count: 0, payees: [] }] } };

const gap = (over: Record<string, unknown> = {}) => ({
  observation_id: "o-1", observed_on: "2026-10-15", observed_paise: 8_000_000, expected_paise: 8_420_000,
  gap_paise: -420_000, since: "2026-08-12", since_source: "statement", observed_source: "statement", state: "open",
  ...over });
const SAVINGS = { account_id: "a-1", name: "HDFC Savings", type: "bank",
  verified: { balance_paise: 8_420_000, on: "2026-08-12", source: "statement" }, balance_paise: 8_000_000,
  transactions_since: 34, gaps: [] as unknown[] };
const CARD = { account_id: "c-1", name: "Millenia", type: "credit_card",
  verified: { balance_paise: 2_900_000, on: "2026-10-15", source: "statement" }, balance_paise: 3_100_000,
  transactions_since: 2, gaps: [] as unknown[] };
const WALLET = { account_id: "w-1", name: "Wallet", type: "cash", verified: null, balance_paise: null,
  transactions_since: 0, gaps: [] as unknown[] };

beforeEach(() => { vi.useFakeTimers({ toFake: ["Date"] }); vi.setSystemTime(new Date("2026-10-19T06:00:00Z")); });
afterEach(() => { vi.useRealTimers(); });

function open(balances: unknown[], routes: Record<string, Reply> = {}) {
  const api = mockApi({ "GET /api/auth/me": ME, "GET /api/setup": SETUP_DONE, "GET /api/report": REPORT,
    "GET /api/review": NO_REVIEW, "GET /api/balances": { status: 200, body: balances },
    "GET /api/trends": { status: 200, body: { months: [], history_from: null, partial: [], categories: [] } },
    "GET /api/recurring": { status: 200, body: [] }, ...routes });
  renderApp("/");
  return { api, user: userEvent.setup({ advanceTimers: vi.advanceTimersByTime }) };
}

test("each account's balance is the verified figure plus movements since, never one bare number", async () => {
  open([SAVINGS, CARD, WALLET]);
  const panel = await screen.findByRole("region", { name: "Balances" });
  const savings = within(panel).getByRole("group", { name: "HDFC Savings" });
  expect(within(savings).getByText("₹80,000.00")).toBeInTheDocument();
  expect(within(savings).getByText("as of 12 Aug from the statement, plus 34 transactions since")).toBeInTheDocument();
  const card = within(panel).getByRole("group", { name: "Millenia" });
  expect(within(card).getByText("₹31,000.00 owed")).toBeInTheDocument();
  expect(within(panel).getByRole("group", { name: "Wallet" })).toHaveTextContent("No starting balance yet");
  expect(panel.textContent).not.toMatch(/total/i);
});

test("an opening balance says it is the opening balance", async () => {
  open([{ ...SAVINGS, verified: { balance_paise: 1_000_000, on: "2026-07-01", source: "opening" }, transactions_since: 1 }]);
  expect(await screen.findByText("from your opening balance on 1 Jul, plus 1 transaction since")).toBeInTheDocument();
});

test("a gap is a banner naming its account, with the direction in words; two accounts, two banners", async () => {
  open([{ ...SAVINGS, gaps: [gap()] }, { ...CARD, gaps: [gap({ observation_id: "o-2", gap_paise: -1_800_000,
    observed_paise: 2_900_000, expected_paise: 4_700_000 })] }]);
  const banners = await screen.findAllByRole("status", { name: /Balance check/ });
  expect(banners).toHaveLength(2);
  const [savings, card] = banners as [HTMLElement, HTMLElement];
  expect(savings).toHaveTextContent("Balance check — HDFC Savings: ₹4,200.00 unexplained since 12 Aug");
  expect(savings).toHaveTextContent("The statement on 15 Oct says ₹4,200.00 less than Hisaab has.");
  expect(card).toHaveTextContent("The statement on 15 Oct says you owe ₹18,000.00 less than Hisaab has.");
  expect(within(savings).getByRole("link", { name: "See transactions" }))
    .toHaveAttribute("href", "/transactions?from=2026-08-12&to=2026-10-15");
});

test("accepting asks first, naming the amount and that it counts in no total", async () => {
  const { api, user } = open([{ ...SAVINGS, gaps: [gap()] }],
    { "POST /api/balances/checks/o-1/accept": { status: 200, body: { ...SAVINGS, gaps: [] } } });
  const banner = await screen.findByRole("status", { name: /Balance check/ });
  await user.click(within(banner).getByRole("button", { name: "Accept as an adjustment" }));
  expect(banner).toHaveTextContent("This writes a ₹4,200.00 adjustment dated 15 Oct. It counts in no total: " +
    "not spending, not cash out, not money in.");
  expect(api.calls).not.toContain("POST /api/balances/checks/o-1/accept");
  api.set("GET /api/balances", { status: 200, body: [SAVINGS] });
  await user.click(within(banner).getByRole("button", { name: "Write the adjustment" }));
  await waitFor(() => expect(api.calls).toContain("POST /api/balances/checks/o-1/accept"));
  await waitFor(() => expect(screen.queryByRole("status", { name: /Balance check/ })).not.toBeInTheDocument());
});

test("a gap that is gone offers to close it as investigated", async () => {
  const { api, user } = open([{ ...SAVINGS, gaps: [gap({ state: "fixed", gap_paise: 0 })] }],
    { "POST /api/balances/checks/o-1/close": { status: 200, body: SAVINGS } });
  const banner = await screen.findByRole("status", { name: /Balance check/ });
  expect(banner).toHaveTextContent("HDFC Savings: the gap since 12 Aug is gone.");
  expect(within(banner).queryByRole("button", { name: "Accept as an adjustment" })).not.toBeInTheDocument();
  await user.click(within(banner).getByRole("button", { name: "Close as investigated" }));
  await waitFor(() => expect(api.calls).toContain("POST /api/balances/checks/o-1/close"));
});

test("entering today's balance posts paise and a date, and shows the server's sentence on refusal", async () => {
  const { api, user } = open([CARD], {
    "POST /api/accounts/c-1/checkpoints": { status: 422, body: { detail: "For a card, enter what you owe as a positive amount." } } });
  const row = await screen.findByRole("group", { name: "Millenia" });
  await user.click(within(row).getByRole("button", { name: "Enter today's balance" }));
  expect(within(row).getByText(/enter what you owe/)).toBeInTheDocument();
  await user.type(within(row).getByLabelText("Amount owed"), "28,500.50");
  await user.click(within(row).getByRole("button", { name: "Save balance" }));
  await waitFor(() => expect(api.bodiesFor("POST /api/accounts/c-1/checkpoints")).toEqual(
    [{ balance_paise: 2_850_050, on: "2026-10-19" }]));
  expect(await within(row).findByRole("alert")).toHaveTextContent("positive amount");
});
