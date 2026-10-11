import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { todayIso } from "../dates";
import { mockApi } from "../test/mockApi";
import { renderApp } from "../test/renderApp";

const ME = { status: 200, body: { id: "u-1", username: "asha", display_name: "Asha", role: "admin" } };
const account = (id: string, type: string, name: string, last4: string | null = null) => ({
  id, type, display_name: name, institution: null, last4, opening_balance_paise: 0,
  opening_balance_date: "2026-07-01", archived_at: null, created_at: "2026-10-01T00:00:00.000Z", identifiers: [],
});
const WALLET = account("w-1", "cash", "Wallet");
const BANK = account("b-1", "bank", "HDFC Savings", "5512");
const CARD = account("c-1", "credit_card", "Regalia card", "8830");
const ACCOUNTS = { status: 200, body: [WALLET, BANK, CARD] };

const txn = (over: Record<string, unknown> = {}) => ({
  id: "t-1", kind: "expense", amount_paise: 40000, transaction_date: "2026-10-05", transaction_time: null,
  from_account_id: "w-1", to_account_id: null, payee_raw: "Tea stall", status: "confirmed", provenance: "manual",
  edits: [], ...over,
});

function setup(path: string, extra: Record<string, { status: number; body?: unknown }> = {}) {
  const api = mockApi({ "GET /api/auth/me": ME, "GET /api/accounts": ACCOUNTS, ...extra });
  renderApp(path);
  return { api, user: userEvent.setup() };
}

// --- /add ------------------------------------------------------------------------

test("an expense is saved as integer paise from the chosen account, then the form is ready for the next", async () => {
  const { api, user } = setup("/add", { "POST /api/transactions": { status: 201, body: txn() } });
  const form = await screen.findByRole("form", { name: "Add a transaction" });
  await user.selectOptions(within(form).getByLabelText("Paid from"), "w-1");
  await user.type(within(form).getByLabelText("Amount"), "400");
  await user.type(within(form).getByLabelText("Paid to"), "Tea stall");
  await user.click(within(form).getByRole("button", { name: "Save" }));
  expect(await screen.findByRole("status")).toHaveTextContent("Saved ₹400.00 to Tea stall.");
  expect(api.bodiesFor("POST /api/transactions")).toEqual([{
    kind: "expense", amount_paise: 40000, transaction_date: todayIso(), from_account_id: "w-1", payee: "Tea stall",
  }]);
  expect(within(form).getByLabelText("Amount")).toHaveValue("");
  expect(within(form).getByLabelText("Paid from")).toHaveValue("w-1"); // kept for the next entry
});

test("a transfer picks two accounts and has no payee", async () => {
  const { api, user } = setup("/add", { "POST /api/transactions": { status: 201, body: txn({ kind: "transfer" }) } });
  const form = await screen.findByRole("form", { name: "Add a transaction" });
  await user.click(within(form).getByRole("radio", { name: "Transfer" }));
  expect(within(form).queryByLabelText("Paid to")).not.toBeInTheDocument();
  await user.selectOptions(within(form).getByLabelText("From"), "b-1");
  await user.selectOptions(within(form).getByLabelText("To"), "w-1");
  await user.type(within(form).getByLabelText("Amount"), "5,000");
  await user.click(within(form).getByRole("button", { name: "Save" }));
  await screen.findByRole("status");
  const [sent] = api.bodiesFor("POST /api/transactions") as Record<string, unknown>[];
  expect([sent!.kind, sent!.from_account_id, sent!.to_account_id, sent!.amount_paise, "payee" in sent!])
    .toEqual(["transfer", "b-1", "w-1", 500000, false]);
});

test("income can only land in a bank account or cash", async () => {
  const { user } = setup("/add");
  const form = await screen.findByRole("form", { name: "Add a transaction" });
  await user.click(within(form).getByRole("radio", { name: "Income" }));
  const options = within(within(form).getByLabelText("Received into")).getAllByRole("option").map((o) => o.textContent);
  expect(options).toEqual(["Choose an account", "Wallet", "HDFC Savings"]);
});

test("a missing account or amount is caught before sending", async () => {
  const { api, user } = setup("/add");
  const form = await screen.findByRole("form", { name: "Add a transaction" });
  await user.click(within(form).getByRole("button", { name: "Save" }));
  expect(within(form).getByText("Choose the account it was paid from.")).toBeInTheDocument();
  expect(within(form).getByText("Enter an amount.")).toBeInTheDocument();
  expect(api.calls).not.toContain("POST /api/transactions");
});

test("with no accounts yet, Add points to Accounts", async () => {
  mockApi({ "GET /api/auth/me": ME, "GET /api/accounts": { status: 200, body: [] } });
  renderApp("/add");
  expect(await screen.findByRole("link", { name: "Add an account first" })).toHaveAttribute("href", "/accounts");
});

// --- /transactions ---------------------------------------------------------------

test("the list shows entries as a ledger, money in marked Cr", async () => {
  setup("/transactions", {
    "GET /api/transactions": { status: 200, body: [
      txn(),
      txn({ id: "t-2", kind: "income", amount_paise: 1200000, from_account_id: null, to_account_id: "b-1",
            payee_raw: "Salary", transaction_date: "2026-10-01" }),
    ] },
  });
  const rows = await screen.findAllByRole("row");
  const texts = rows.slice(1).map((r) => r.textContent);
  expect(texts[0]).toContain("Tea stall");
  expect(texts[0]).toContain("Wallet");
  expect(texts[0]).toContain("₹400.00");
  expect(texts[1]).toContain("₹12,000.00Cr");
  expect(texts[1]).toContain("HDFC Savings");
});

test("correcting an amount keeps the entry's value visible and can be undone", async () => {
  const { api, user } = setup("/transactions", {
    "GET /api/transactions": { status: 200, body: [txn()] },
    "PATCH /api/transactions/t-1": { status: 200, body: txn({ amount_paise: 45000,
      edits: [{ field: "amount", prior_value: "40000", new_value: "45000" }] }) },
  });
  await user.click(await screen.findByRole("button", { name: /Tea stall/ }));
  const detail = screen.getByRole("form", { name: "Correct this transaction" });
  await user.clear(within(detail).getByLabelText("Amount"));
  await user.type(within(detail).getByLabelText("Amount"), "450");
  api.set("GET /api/transactions", { status: 200, body: [txn({ amount_paise: 45000,
    edits: [{ field: "amount", prior_value: "40000", new_value: "45000" }] })] });
  await user.click(within(detail).getByRole("button", { name: "Save correction" }));
  expect(api.bodiesFor("PATCH /api/transactions/t-1")).toEqual([{ amount_paise: 45000 }]);
  expect(await screen.findByText("Edited. The entry said ₹400.00.")).toBeInTheDocument();

  api.set("POST /api/transactions/t-1/revert", { status: 200, body: txn() });
  api.set("GET /api/transactions", { status: 200, body: [txn()] });
  await user.click(screen.getByRole("button", { name: "Undo amount" }));
  expect(api.bodiesFor("POST /api/transactions/t-1/revert")).toEqual([{ fields: ["amount"] }]);
});

test("deleting a manual entry asks first", async () => {
  const { api, user } = setup("/transactions", {
    "GET /api/transactions": { status: 200, body: [txn()] },
    "DELETE /api/transactions/t-1": { status: 204 },
  });
  await user.click(await screen.findByRole("button", { name: /Tea stall/ }));
  await user.click(screen.getByRole("button", { name: "Delete entry" }));
  expect(api.calls).not.toContain("DELETE /api/transactions/t-1");
  api.set("GET /api/transactions", { status: 200, body: [] });
  await user.click(screen.getByRole("button", { name: "Delete it" }));
  expect(await screen.findByText("No transactions yet.")).toBeInTheDocument();
  expect(api.calls).toContain("DELETE /api/transactions/t-1");
});

test("the nav offers Transactions and Add", async () => {
  setup("/transactions", { "GET /api/transactions": { status: 200, body: [] } });
  expect((await screen.findAllByRole("link", { name: "Transactions" })).length).toBeGreaterThan(0);
  expect(screen.getAllByRole("link", { name: "Add" }).length).toBeGreaterThan(0);
});
