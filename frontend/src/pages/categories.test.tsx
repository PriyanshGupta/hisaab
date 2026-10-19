import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { mockApi } from "../test/mockApi";
import { renderApp } from "../test/renderApp";

const ME = { status: 200, body: { id: "u-1", username: "asha", display_name: "Asha", role: "admin" } };
const cat = (id: string, name: string, archived_at: string | null = null) => ({ id, name, archived_at });
const FOOD = cat("c-food", "Food & Dining");
const GROC = cat("c-groc", "Groceries");
const GIFTS = cat("c-gifts", "Gifts");
const OLD = cat("c-old", "Lending", "2026-10-01T00:00:00.000Z");
const CATEGORIES = { status: 200, body: [FOOD, GROC, GIFTS, OLD] };
const WALLET = { id: "w-1", type: "cash", display_name: "Wallet", institution: null, last4: null,
  opening_balance_paise: 0, opening_balance_date: "2026-07-01", archived_at: null,
  created_at: "2026-10-01T00:00:00.000Z", identifiers: [] };

const txn = (over: Record<string, unknown> = {}) => ({
  id: "t-1", kind: "expense", amount_paise: 25000, transaction_date: "2026-10-05", transaction_time: null,
  from_account_id: "w-1", to_account_id: null, payee_raw: "Swiggy", payee_id: "p-1", payee_name: "Swiggy",
  category_id: null, category_source: null, status: "confirmed", provenance: "manual", edits: [], ...over,
});

function setup(path: string, extra: Record<string, { status: number; body?: unknown }> = {}) {
  const api = mockApi({
    "GET /api/auth/me": ME, "GET /api/categories": CATEGORIES,
    "GET /api/accounts": { status: 200, body: [WALLET] },
    "GET /api/payees": { status: 200, body: [{ id: "p-1", name: "Swiggy", type: "merchant", category_id: null }] },
    ...extra,
  });
  renderApp(path);
  return { api, user: userEvent.setup() };
}

// --- Categories screen --------------------------------------------------------------

test("lists active categories; archived ones only on request", async () => {
  const { user } = setup("/categories");
  expect(await screen.findByRole("listitem", { name: "Food & Dining" })).toBeInTheDocument();
  expect(screen.queryByRole("listitem", { name: "Lending" })).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Show archived (1)" }));
  expect(within(screen.getByRole("listitem", { name: "Lending" })).getByRole("button", { name: "Restore" }))
    .toBeInTheDocument();
});

test("adding a category", async () => {
  const { api, user } = setup("/categories", { "POST /api/categories": { status: 201, body: cat("c-new", "Pets") } });
  await user.type(await screen.findByLabelText("New category"), "Pets");
  await user.click(screen.getByRole("button", { name: "Add category" }));
  expect(api.bodiesFor("POST /api/categories")).toEqual([{ name: "Pets" }]);
});

test("a clashing name shows the server's explanation", async () => {
  const { user } = setup("/categories", {
    "POST /api/categories": { status: 409, body: { detail: "You have an archived category called Lending. Restore it instead." } },
  });
  await user.type(await screen.findByLabelText("New category"), "lending");
  await user.click(screen.getByRole("button", { name: "Add category" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Restore it instead.");
});

test("renaming and archiving", async () => {
  const { api, user } = setup("/categories", {
    "PATCH /api/categories/c-gifts": { status: 200, body: cat("c-gifts", "Gifts & Festivals") },
    "POST /api/categories/c-groc/archive": { status: 200, body: cat("c-groc", "Groceries", "2026-10-11T00:00:00.000Z") },
  });
  const gifts = await screen.findByRole("listitem", { name: "Gifts" });
  await user.click(within(gifts).getByRole("button", { name: "Rename" }));
  const field = within(gifts).getByLabelText("Name for Gifts");
  await user.clear(field);
  await user.type(field, "Gifts & Festivals");
  await user.click(within(gifts).getByRole("button", { name: "Save" }));
  expect(api.bodiesFor("PATCH /api/categories/c-gifts")).toEqual([{ name: "Gifts & Festivals" }]);

  await user.click(within(screen.getByRole("listitem", { name: "Groceries" })).getByRole("button", { name: "Archive" }));
  expect(api.calls).toContain("POST /api/categories/c-groc/archive");
});

// --- categories on transactions -------------------------------------------------------

test("each transaction shows its category, or Uncategorized", async () => {
  setup("/transactions", { "GET /api/transactions": { status: 200, body: [
    txn(), txn({ id: "t-2", payee_name: "Big Bazaar", payee_raw: "Big Bazaar", category_id: "c-groc", category_source: "rule" }),
  ] } });
  const rows = (await screen.findAllByRole("row")).slice(1);
  expect(rows[0]!.textContent).toContain("Uncategorized");
  expect(rows[1]!.textContent).toContain("Groceries");
});

test("filing every Swiggy transaction under a category sets the payee rule", async () => {
  const { api, user } = setup("/transactions", {
    "GET /api/transactions": { status: 200, body: [txn()] },
    "PUT /api/payees/p-1/category": { status: 200, body: { id: "p-1", name: "Swiggy", type: "merchant", category_id: "c-food" } },
  });
  await user.click(await screen.findByRole("button", { name: /Swiggy/ }));
  await user.selectOptions(screen.getByLabelText("Category"), "c-food");
  await user.click(screen.getByRole("button", { name: "Use for all Swiggy" }));
  expect(api.bodiesFor("PUT /api/payees/p-1/category")).toEqual([{ category_id: "c-food" }]);
  expect(await screen.findByRole("status")).toHaveTextContent("Saved: every Swiggy payment is now Food & Dining.");
});

test("a one-off category is an override that can be undone", async () => {
  const { api, user } = setup("/transactions", {
    "GET /api/transactions": { status: 200, body: [txn({ category_id: "c-food", category_source: "rule" })] },
    "PATCH /api/transactions/t-1": { status: 200, body: txn() },
  });
  await user.click(await screen.findByRole("button", { name: /Swiggy/ }));
  expect(screen.getByText("Food & Dining, from your rule for Swiggy.")).toBeInTheDocument();
  await user.selectOptions(screen.getByLabelText("Category"), "c-gifts");
  api.set("GET /api/transactions", { status: 200, body: [txn({ category_id: "c-gifts", category_source: "override",
    edits: [{ field: "category_override_id", prior_value: null, new_value: "c-gifts" }] })] });
  await user.click(screen.getByRole("button", { name: "Use for this one" }));
  expect(api.bodiesFor("PATCH /api/transactions/t-1")).toEqual([{ category_override_id: "c-gifts" }]);
  expect(await screen.findByRole("status")).toHaveTextContent("Saved: this payment is now Gifts.");
  expect(await screen.findByText("Gifts, set for this transaction only.")).toBeInTheDocument();

  api.set("POST /api/transactions/t-1/revert", { status: 200, body: txn() });
  await user.click(screen.getByRole("button", { name: "Undo category" }));
  expect(api.bodiesFor("POST /api/transactions/t-1/revert")).toEqual([{ fields: ["category_override_id"] }]);
});

test("an archived category reads as uncategorized and says why", async () => {
  const { user } = setup("/transactions", {
    "GET /api/transactions": { status: 200, body: [txn({ category_id: null, category_source: "rule" })] },
  });
  await user.click(await screen.findByRole("button", { name: /Swiggy/ }));
  expect(screen.getByText("Uncategorized: the category your rule for Swiggy uses is archived.")).toBeInTheDocument();
});

// --- Add ----------------------------------------------------------------------------

test("Add offers active categories and known payees, and sends the pick", async () => {
  const { api, user } = setup("/add", { "POST /api/transactions": { status: 201, body: txn() } });
  const form = await screen.findByRole("form", { name: "Add a transaction" });
  const options = within(within(form).getByLabelText("Category")).getAllByRole("option").map((o) => o.textContent);
  expect(options).toEqual(["No category", "Food & Dining", "Groceries", "Gifts"]); // archived Lending left out
  await waitFor(() => expect(document.querySelector("datalist option[value='Swiggy']")).not.toBeNull());
  await user.selectOptions(within(form).getByLabelText("Paid from"), "w-1");
  await user.type(within(form).getByLabelText("Amount"), "250");
  await user.type(within(form).getByLabelText("Paid to"), "Swiggy");
  await user.selectOptions(within(form).getByLabelText("Category"), "c-food");
  await user.click(within(form).getByRole("button", { name: "Save" }));
  await screen.findByRole("status");
  const [sent] = api.bodiesFor("POST /api/transactions") as Record<string, unknown>[];
  expect(sent!.category_id).toBe("c-food");
});

test("the nav offers Categories", async () => {
  setup("/categories");
  expect((await screen.findAllByRole("link", { name: "Categories" })).length).toBeGreaterThan(0);
});
