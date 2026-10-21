import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";

import { mockApi, type Reply } from "../test/mockApi";
import { renderApp } from "../test/renderApp";

const ME = { status: 200, body: { id: "u-1", username: "asha", display_name: "Asha", role: "admin" } };
const ACCOUNTS = { status: 200, body: [
  { id: "a-1", type: "bank", display_name: "HDFC Savings", institution: null, last4: "5512", opening_balance_paise: null,
    opening_balance_date: null, archived_at: null, created_at: "2026-10-11T00:00:00.000Z", identifiers: [] },
  { id: "a-2", type: "bank", display_name: "SBI", institution: null, last4: "7001", opening_balance_paise: null,
    opening_balance_date: null, archived_at: null, created_at: "2026-10-11T00:00:00.000Z", identifiers: [] },
] };
const CATEGORIES = { status: 200, body: [
  { id: "c-food", name: "Food", archived_at: null }, { id: "c-travel", name: "Travel", archived_at: null },
  { id: "c-old", name: "Old", archived_at: "2026-10-01T00:00:00.000Z" },
] };
const PAYEES = { status: 200, body: [{ id: "p-swiggy", name: "Swiggy", type: "merchant", category_id: null }] };
const NO_BATCH = { status: 200, body: { batch_id: null, asked: 0, applied: [], transactions: 0, left: 0, called: false } };

let n = 0;
const item = (over: Record<string, unknown>) => ({
  id: `t-${++n}`, transaction_date: "2026-10-05", amount_paise: 25000, kind: "expense", payee_raw: null, payee_id: null,
  payee_name: null, category_id: null, account_label: "HDFC Savings", identifier: null, account_leg: "from",
  tags: ["uncategorized"], reason: null, status: "confirmed", ...over,
});
const unassigned = (identifier: string, over: Record<string, unknown> = {}) => item({
  identifier, account_label: `ending ${identifier}`, tags: ["account", "uncategorized"],
  reason: `account ${identifier} isn't one of yours yet`, status: "needs_review", ...over });
const unnamed = (raw: string, over: Record<string, unknown> = {}) => item({
  payee_raw: raw, tags: ["payee", "uncategorized"], reason: "payee not recognised", status: "needs_review", ...over });
const uncategorized = (payee: string, name: string, over: Record<string, unknown> = {}) => item({
  payee_id: payee, payee_name: name, payee_raw: name.toUpperCase(), ...over });

function setup(items: unknown[], extra: Record<string, Reply> = {}, path = "/review") {
  const api = mockApi({
    "GET /api/auth/me": ME, "GET /api/review": { status: 200, body: { items, categorized: [], sorted_accounts: [], sorted_payees: [] } }, "GET /api/accounts": ACCOUNTS,
    "GET /api/categories": CATEGORIES, "GET /api/payees": PAYEES, "GET /api/review/auto-categorize/latest": NO_BATCH,
    ...extra,
  });
  const { router } = renderApp(path);
  return { api, router, user: userEvent.setup() };
}

const rows = () => within(screen.getByRole("list", { name: /to sort/ })).getAllByRole("listitem");

test("the accounts lane groups by identifier; one answer resolves the group and the cursor moves on", async () => {
  const items = [unassigned("4821"), unassigned("4821", { amount_paise: 1000 }), unassigned("3321")];
  const { api, user } = setup(items, {
    "POST /api/review/identifier": { status: 200, body: { resolved: 2 } } });
  expect(await screen.findByRole("tab", { name: /Accounts 3/ })).toHaveAttribute("aria-selected", "true");
  const [first] = rows();
  expect(within(first!).getByText("Ending 4821")).toBeInTheDocument();
  expect(within(first!).getByText("2 transactions")).toBeInTheDocument();
  await user.click(within(first!).getByRole("button", { name: "Which account?" }));
  await user.type(screen.getByRole("combobox", { name: "Account for ending 4821" }), "HDF");
  api.set("GET /api/review", { status: 200, body: { items: [items[2]], categorized: [], sorted_payees: [],
    sorted_accounts: [{ key: "a-1", label: "Ending 5512, ending 4821", account_id: "a-1", answer: "HDFC Savings",
      items: [items[0], items[1]] }] } });
  await user.keyboard("{Enter}");
  expect(api.bodiesFor("POST /api/review/identifier")).toEqual([{ identifier: "4821", account_id: "a-1" }]);
  expect(await screen.findByRole("status")).toHaveTextContent("Ending 4821: 2 transactions now on HDFC Savings.");
  await waitFor(() => expect(rows()).toHaveLength(1));
  expect(rows()[0]).toHaveAttribute("aria-current", "true");
  const done = within(screen.getByRole("list", { name: "Sorted" })).getByRole("listitem");  // moved down, not gone
  expect(within(done).getByText("Ending 5512, ending 4821")).toBeInTheDocument();
  expect(within(done).getByText("on HDFC Savings")).toBeInTheDocument();
});

test("an identifier can become a new account, its last 4 digits filled in", async () => {
  const { user } = setup([unassigned("4821")]);
  await user.click(await screen.findByRole("button", { name: "Which account?" }));
  await user.click(screen.getByRole("option", { name: "Add an account ending 4821" }));
  const form = screen.getByRole("form", { name: "Add account" });
  expect(within(form).getByLabelText("Last 4 digits")).toHaveValue("4821");
});

test("naming a raw payee string, as admin, says it covers every transaction with that text", async () => {
  const raw = "UPI/DR/111111/TEST PAYEE@okexample";
  const { api, user } = setup([unnamed(raw), unnamed(raw, { amount_paise: 300 }), unnamed(raw, { amount_paise: 9 })], {
    "POST /api/review/name-payee": { status: 200, body: { updated: 3, payee_id: "p-new", pattern: true } } });
  await user.click(await screen.findByRole("tab", { name: /Payees 3/ }));
  await user.click(screen.getByRole("button", { name: "Name it" }));
  expect(screen.getByText("All 3 with this text, now and later")).toBeInTheDocument();
  await user.type(screen.getByRole("combobox", { name: `Payee for ${raw}` }), "Test Payee");
  await user.click(screen.getByRole("option", { name: "Create “Test Payee”" }));
  expect(api.bodiesFor("POST /api/review/name-payee")).toEqual([{ payee_raw: raw, name: "Test Payee" }]);
  expect(await screen.findByRole("status")).toHaveTextContent("Named Test Payee: 3 transactions.");
});

test("categorizing a payee is one rule, and the confirmation counts the overrides kept", async () => {
  const items = Array.from({ length: 23 }, (_, i) => uncategorized("p-swiggy", "Swiggy", { amount_paise: 100 + i }));
  const { api, user } = setup(items, {
    "POST /api/review/payee-category": { status: 200, body: { updated: 23, overrides_kept: 1 } } });
  await user.click(await screen.findByRole("tab", { name: /Categories 23/ }));
  await user.click(screen.getByRole("button", { name: "Choose a category" }));
  expect(screen.queryByRole("option", { name: "Old" })).not.toBeInTheDocument(); // archived
  await user.click(screen.getByRole("option", { name: "Food" }));
  expect(api.bodiesFor("POST /api/review/payee-category")).toEqual([{ payee_id: "p-swiggy", category_id: "c-food" }]);
  expect(await screen.findByRole("status")).toHaveTextContent("23 transactions updated, 1 override kept.");
});

test("Cancel closes the category box without answering, and the row is as it was", async () => {
  const { api, user } = setup([uncategorized("p-swiggy", "Swiggy")]);
  await user.click(await screen.findByRole("tab", { name: /Categories 1/ }));
  await user.click(screen.getByRole("button", { name: "Choose a category" }));
  expect(screen.getByRole("combobox", { name: "Category for Swiggy" })).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Cancel" }));
  expect(screen.queryByRole("combobox", { name: "Category for Swiggy" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Choose a category" })).toBeInTheDocument();
  expect(api.calls.filter((c) => c.startsWith("POST"))).toEqual([]);
});

test("the keyboard alone clears a lane: arrows move, Enter answers, typing filters, Esc closes", async () => {
  const { api, user } = setup([unassigned("4821"), unassigned("3321")], {
    "POST /api/review/identifier": { status: 200, body: { resolved: 1 } } });
  await screen.findByText("Ending 4821");
  screen.getByRole("list", { name: /to sort/ }).focus();
  await user.keyboard("{ArrowDown}");
  expect(rows()[1]).toHaveAttribute("aria-current", "true");
  await user.keyboard("{Enter}");
  const box = screen.getByRole("combobox", { name: "Account for ending 4821" }); // equal counts sort by label: 3321 first
  expect(box).toHaveFocus();
  await user.keyboard("{Escape}");
  expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
  await user.keyboard("{Enter}");
  await user.keyboard("SB{Enter}");
  expect(api.bodiesFor("POST /api/review/identifier")).toEqual([{ identifier: "4821", account_id: "a-2" }]);
});

test("each transaction in a group has its own actions", async () => {
  const one = unassigned("4821", { id: "t-x", amount_paise: 3_000_000, payee_name: "Swiggy" });
  const { api, user } = setup([one], {
    "POST /api/transactions/t-x/acknowledge": { status: 200, body: {} },
    "PATCH /api/transactions/t-x": { status: 200, body: {} } });
  await user.click(await screen.findByRole("button", { name: "Show the transactions in ending 4821" }));
  const line = screen.getByRole("listitem", { name: /₹30,000/ });
  await user.click(within(line).getByRole("button", { name: "Looks right" }));
  expect(api.calls).toContain("POST /api/transactions/t-x/acknowledge");
  await user.click(within(line).getByRole("button", { name: "Category for this one" }));
  await user.click(screen.getByRole("option", { name: "Travel" }));
  await user.click(within(line).getByRole("button", { name: "Mark as transfer" }));
  await user.click(screen.getByRole("option", { name: "SBI" }));
  expect(api.bodiesFor("PATCH /api/transactions/t-x")).toEqual([
    { category_override_id: "c-travel" }, { kind: "transfer", to_account_id: "a-2" }]);
});

test("auto-categorized payees appear as ordinary sorted rows, marked, changeable, and the batch can be undone", async () => {
  const items = [uncategorized("p-swiggy", "Swiggy"), uncategorized("p-uber", "Uber")];
  const result = { batch_id: "b-1", asked: 2, called: true, transactions: 187, left: 12,
    applied: [{ payee_id: "p-swiggy", payee_name: "Swiggy", category_id: "c-food", confidence_percent: 95, transactions: 1 }] };
  const { api, user } = setup(items, {
    "POST /api/review/auto-categorize": { status: 200, body: result },
    "POST /api/review/payee-category": { status: 200, body: { updated: 1, overrides_kept: 0 } },
    "DELETE /api/review/auto-categorize/b-1": { status: 200, body: { removed: 1 } } });
  await user.click(await screen.findByRole("tab", { name: /Categories 2/ }));
  api.set("GET /api/review/auto-categorize/latest", { status: 200, body: { ...result, called: false } });
  api.set("GET /api/review", { status: 200, body: { items: [items[1]], sorted_accounts: [], sorted_payees: [], categorized: [
    { payee_id: "p-swiggy", payee_name: "Swiggy", category_id: "c-food", origin: "llm", items: [items[0]] }] } });
  await user.click(screen.getByRole("button", { name: "Auto-categorize" }));
  expect(await screen.findByText(
    "Categorized 1 payee covering 187 transactions. 12 are left for you — we couldn't categorize these.")).toBeInTheDocument();
  expect(screen.queryByRole("table")).not.toBeInTheDocument(); // one row style for everything
  expect(screen.queryByText(/95/)).not.toBeInTheDocument(); // model confidence isn't for the person (2026-10-19)
  const done = await within(await screen.findByRole("list", { name: "Sorted" })).findByRole("listitem");
  expect(within(done).getByText("Swiggy")).toBeInTheDocument();
  expect(within(done).getByText("Food")).toBeInTheDocument();
  expect(within(done).getByText("auto-categorized")).toBeInTheDocument();
  await user.click(within(done).getByRole("button", { name: "Change category" }));
  await user.click(screen.getByRole("option", { name: "Travel" }));
  expect(api.bodiesFor("POST /api/review/payee-category")).toEqual([{ payee_id: "p-swiggy", category_id: "c-travel" }]);
  await user.click(screen.getByRole("button", { name: "Undo auto-categorize" }));
  expect(api.calls).toContain("DELETE /api/review/auto-categorize/b-1");
});

test("a second auto-categorize with nothing new to ask says so", async () => {
  const { user } = setup([uncategorized("p-swiggy", "Swiggy")], {
    "POST /api/review/auto-categorize": { status: 200, body: { ...NO_BATCH.body, left: 1 } } });
  await user.click(await screen.findByRole("tab", { name: /Categories 1/ }));
  await user.click(screen.getByRole("button", { name: "Auto-categorize" }));
  expect(await screen.findByText(/Nothing new to categorize/)).toBeInTheDocument();
});

test("an empty lane says there is nothing to sort", async () => {
  setup([]);
  expect(await screen.findByText("Nothing to sort here.")).toBeInTheDocument();
});

test("the first-run landing opens on the categories lane", async () => {
  setup([uncategorized("p-swiggy", "Swiggy")], {}, "/review?lane=categories");
  expect(await screen.findByRole("tab", { name: /Categories 1/ })).toHaveAttribute("aria-selected", "true");
});

test("the list takes focus on arrival and after switching lanes by key, and keeps the cursor in view", async () => {
  const scrolled = vi.fn();
  Element.prototype.scrollIntoView = scrolled;
  const { user } = setup([unassigned("4821"), unassigned("3321"), uncategorized("p-swiggy", "Swiggy")]);
  await screen.findByText("Ending 4821");
  expect(screen.getByRole("list", { name: /to sort/ })).toHaveFocus();
  await user.keyboard("{ArrowDown}");
  expect(scrolled).toHaveBeenCalledWith({ block: "nearest" });
  await user.keyboard("2");
  expect(screen.getByRole("tab", { name: /Categories 3/ })).toHaveAttribute("aria-selected", "true");
  expect(screen.getByRole("list", { name: /to sort/ })).toHaveFocus();
});

test("transactions whose email named no payee are categorized one by one, from their own group", async () => {
  const lone = item({ id: "t-lone", tags: ["uncategorized"], payee_raw: null, amount_paise: 4200 });
  const { api, user } = setup([lone], { "PATCH /api/transactions/t-lone": { status: 200, body: {} } });
  await user.click(await screen.findByRole("tab", { name: /Categories 1/ }));
  const [row] = rows();
  expect(within(row!).getByText("Unknown payee")).toBeInTheDocument();
  await user.click(within(row!).getByRole("button", { name: "Choose a category" }));
  expect(screen.getByText("This one only: its email named no payee")).toBeInTheDocument();
  await user.click(screen.getByRole("option", { name: "Food" }));
  await waitFor(() => expect(api.bodiesFor("PATCH /api/transactions/t-lone")).toHaveLength(1));
  expect(api.bodiesFor("PATCH /api/transactions/t-lone")).toEqual([{ category_override_id: "c-food" }]);
});

test("a payee name can be saved exactly as shown, without typing it", async () => {
  const { api, user } = setup([unnamed("Swish")], {
    "POST /api/review/name-payee": { status: 200, body: { updated: 1, payee_id: "p-new", pattern: true } } });
  await user.click(await screen.findByRole("tab", { name: /Payees 1/ }));
  await user.click(screen.getByRole("button", { name: "Name it" }));
  await user.keyboard("{Enter}"); // the suggestion is first: Enter saves it
  expect(api.bodiesFor("POST /api/review/name-payee")).toEqual([{ payee_raw: "Swish", name: "Swish" }]);
});

test("transactions with no payee in the email are one closed group, named like the rest", async () => {
  const a = item({ id: "t-a", tags: ["payee", "uncategorized"], reason: "no payee in the email", status: "needs_review" });
  const b = item({ id: "t-b", tags: ["payee", "uncategorized"], reason: "no payee in the email", status: "needs_review",
    amount_paise: 300 });
  const { api, user } = setup([a, b], {
    "POST /api/review/name-payee": { status: 200, body: { updated: 2, payee_id: "p-m", pattern: false } } });
  await user.click(await screen.findByRole("tab", { name: /Payees 2/ }));
  const [row] = rows();
  expect(within(row!).getByText("Unknown payee")).toBeInTheDocument();
  expect(within(row!).queryByRole("list")).not.toBeInTheDocument(); // closed until asked
  await user.click(within(row!).getByRole("button", { name: "Name it" }));
  await user.keyboard("Milkman{Enter}");
  expect(api.bodiesFor("POST /api/review/name-payee")).toEqual([{ transaction_ids: ["t-a", "t-b"], name: "Milkman" }]);
});

test("transactions with no account in the email are one closed group given an account by edit", async () => {
  const a = item({ id: "t-c", account_label: null, tags: ["account", "uncategorized"], status: "needs_review" });
  const { api, user } = setup([a], { "POST /api/review/assign-account": { status: 200, body: { updated: 1 } } });
  await screen.findByText("Unknown account");
  const [row] = rows();
  await user.click(within(row!).getByRole("button", { name: "Which account?" }));
  await user.click(screen.getByRole("option", { name: "SBI" }));
  expect(api.bodiesFor("POST /api/review/assign-account")).toEqual([{ transaction_ids: ["t-c"], account_id: "a-2" }]);
});

test("a number that isn't an account (a FASTag wallet) is kept as expenses and not asked again", async () => {
  const { api, user } = setup([unassigned("5678"), unassigned("5678", { amount_paise: 7 })], {
    "POST /api/review/not-my-account": { status: 200, body: { quieted: 2 } } });
  await user.click(await screen.findByRole("button", { name: "Which account?" }));
  await user.click(screen.getByRole("option", { name: "Not my account — keep as expenses" }));
  expect(api.bodiesFor("POST /api/review/not-my-account")).toEqual([{ identifier: "5678" }]);
  expect(await screen.findByRole("status")).toHaveTextContent("Ending 5678: 2 transactions kept as expenses.");
});

test("one transaction's payee can be named on its own", async () => {
  const one = unnamed("UPI/DR/1/SOMEONE@okhdfc", { id: "t-p" });
  const { api, user } = setup([one], {
    "POST /api/review/name-payee": { status: 200, body: { updated: 1, payee_id: "p-s", pattern: false } } });
  await user.click(await screen.findByRole("tab", { name: /Payees 1/ }));
  await user.click(screen.getByRole("button", { name: /Show the transactions/ }));
  await user.click(screen.getByRole("button", { name: "Payee for this one" }));
  await user.click(screen.getByRole("option", { name: "Swiggy" }));
  expect(api.bodiesFor("POST /api/review/name-payee")).toEqual([{ transaction_ids: ["t-p"], payee_id: "p-swiggy" }]);
});

test("a categorized payee moves to the bottom with its category instead of disappearing", async () => {
  const items = [uncategorized("p-swiggy", "Swiggy"), uncategorized("p-uber", "Uber")];
  const { api, user } = setup(items, {
    "POST /api/review/payee-category": { status: 200, body: { updated: 1, overrides_kept: 0 } } });
  await user.click(await screen.findByRole("tab", { name: /Categories 2/ }));
  api.set("GET /api/review", { status: 200, body: { items: [items[1]], sorted_accounts: [], sorted_payees: [], categorized: [
    { payee_id: "p-swiggy", payee_name: "Swiggy", category_id: "c-food", origin: "user", items: [items[0]] }] } });
  await user.click(within(rows()[0]!).getByRole("button", { name: "Choose a category" }));
  await user.click(screen.getByRole("option", { name: "Food" }));
  const done = await within(await screen.findByRole("list", { name: "Sorted" })).findByRole("listitem");
  expect(within(done).getByText("Food")).toBeInTheDocument();
  expect(within(done).queryByText("auto-categorized")).not.toBeInTheDocument();
});

test("a named payee stays at the bottom and can be renamed", async () => {
  const { api, user } = setup([unnamed("Swish")], {
    "POST /api/review/name-payee": { status: 200, body: { updated: 1, payee_id: "p-new", pattern: true } },
    "PATCH /api/payees/p-new": { status: 200, body: { id: "p-new", name: "Swish AB", type: "merchant" } } });
  await user.click(await screen.findByRole("tab", { name: /Payees 1/ }));
  api.set("GET /api/review", { status: 200, body: { items: [], categorized: [], sorted_accounts: [], sorted_payees: [
    { key: "Swish|p-new", label: "Swish", payee_id: "p-new", payee_name: "Swish", items: [unnamed("Swish")] }] } });
  await user.click(screen.getByRole("button", { name: "Name it" }));
  await user.keyboard("{Enter}");
  const done = await within(await screen.findByRole("list", { name: "Sorted" })).findByRole("listitem");
  expect(within(done).getByText("named Swish")).toBeInTheDocument();
  await user.click(within(done).getByRole("button", { name: "Rename" }));
  const box = screen.getByRole("textbox", { name: "New name for Swish" });
  await user.clear(box);
  await user.type(box, "Swish AB{Enter}");
  expect(api.bodiesFor("PATCH /api/payees/p-new")).toEqual([{ name: "Swish AB" }]);
});

test("lanes run accounts, categories, payees, checks; naming payees is optional, so it isn't marked or counted as left", async () => {
  setup([unassigned("4821"), unnamed("SOME RAW TEXT"), uncategorized("p-swiggy", "Swiggy")]);
  await screen.findByText("Ending 4821");
  const tabs = screen.getAllByRole("tab");
  expect(tabs.map((t) => t.textContent?.replace(/\s*\d+$/, ""))).toEqual(["Accounts", "Categories", "Payees", "Checks"]);
  expect(screen.getByRole("tab", { name: /Payees/ })).not.toHaveClass("lane--open");
  expect(screen.getByRole("tab", { name: /Accounts/ })).toHaveClass("lane--open");
  expect(screen.getByText(/left/)).toHaveTextContent("3 left"); // the unnamed one still needs a category
});

test("a payee that isn't named yet can be categorized: its text becomes the name, then one rule files it", async () => {
  const raw = "UPI/DR/1/CHAI POINT@okaxis";
  const { api, user } = setup([unnamed(raw), unnamed(raw, { amount_paise: 30 })], {
    "POST /api/review/name-payee": { status: 200, body: { updated: 2, payee_id: "p-chai", pattern: true } },
    "POST /api/review/payee-category": { status: 200, body: { updated: 2, overrides_kept: 0 } } });
  await user.click(await screen.findByRole("tab", { name: /Categories 2/ }));
  await user.click(screen.getByRole("button", { name: "Choose a category" }));
  await user.click(screen.getByRole("option", { name: "Food" }));
  await waitFor(() => expect(api.bodiesFor("POST /api/review/payee-category")).toHaveLength(1));
  expect(api.bodiesFor("POST /api/review/name-payee")).toEqual([{ payee_raw: raw, name: raw }]);
  expect(api.bodiesFor("POST /api/review/payee-category")).toEqual([{ payee_id: "p-chai", category_id: "c-food" }]);
});

test("a transaction to check shows its email: when and from whom, a Gmail link, and the text where it was kept", async () => {
  const email = { received_at: "2026-10-05T03:44:00.000Z", sender: "alerts@bank.example", subject: "Debit alert",
    has_text: true, gmail_url: "https://mail.google.com/mail/?authuser=asha%40example.test#all/18f2c0ab" };
  const odd = item({ id: "t-odd", tags: ["parse"], reason: "couldn't read: date", status: "needs_review", email });
  const { user } = setup([odd], { "GET /api/review/email/t-odd": { status: 200, body: { text: "Rs 500 debited from a/c ending 1111" } } });
  await user.click(await screen.findByRole("tab", { name: /Checks 1/ }));
  expect(screen.getByText(/Email from alerts@bank\.example, received 5 Oct 2026 at 09:14: Debit alert/)).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Open in Gmail" })).toHaveAttribute("href", email.gmail_url);
  await user.click(screen.getByRole("button", { name: "Show the email" }));
  expect(await screen.findByText("Rs 500 debited from a/c ending 1111")).toBeInTheDocument();
});

test("auto-categorize speaks plainly: no talk of models or confidence", async () => {
  const { user } = setup([uncategorized("p-swiggy", "Swiggy")]);
  await user.click(await screen.findByRole("tab", { name: /Categories 1/ }));
  expect(screen.getByRole("region", { name: "Auto-categorize" })).not.toHaveTextContent(/model|confiden/i);
});

test("a category that doesn't exist yet can be created right where it's needed", async () => {
  const { api, user } = setup([uncategorized("p-tail", "Heads Up For Tails")], {
    "POST /api/categories": { status: 201, body: { id: "c-pets", name: "Pets", archived_at: null } },
    "POST /api/review/payee-category": { status: 200, body: { updated: 1, overrides_kept: 0 } } });
  await user.click(await screen.findByRole("tab", { name: /Categories 1/ }));
  await user.click(screen.getByRole("button", { name: "Choose a category" }));
  await user.keyboard("Pets");
  await user.click(screen.getByRole("option", { name: "Create category “Pets”" }));
  await waitFor(() => expect(api.bodiesFor("POST /api/review/payee-category")).toHaveLength(1));
  expect(api.bodiesFor("POST /api/categories")).toEqual([{ name: "Pets" }]);
  expect(api.bodiesFor("POST /api/review/payee-category")).toEqual([{ payee_id: "p-tail", category_id: "c-pets" }]);
});

// ---- sorting the groups (user, 2026-10-21): by amount unless asked otherwise ----

const labels = () => rows().map((r) => r.querySelector(".group__label")?.textContent);
const mixed = () => [
  uncategorized("p-a", "Amazon", { amount_paise: 300000 }),  // 1 transaction, the most money
  uncategorized("p-c", "Chai Point", { amount_paise: 4000 }), uncategorized("p-c", "Chai Point", { amount_paise: 4000 }),
  uncategorized("p-c", "Chai Point", { amount_paise: 4000 }),  // 3 transactions, little money
  uncategorized("p-b", "BigBasket", { amount_paise: 90000 }), uncategorized("p-b", "BigBasket", { amount_paise: 90000 }),
];

test("groups come biggest total first, unless another order is picked", async () => {
  const { user } = setup(mixed(), {}, "/review?lane=categories");
  const sort = await screen.findByRole("group", { name: "Sort by" });
  expect(within(sort).getByRole("button", { name: "Amount" })).toHaveAttribute("aria-pressed", "true");
  expect(labels()).toEqual(["Amazon", "BigBasket", "Chai Point"]);
  await user.click(within(sort).getByRole("button", { name: "Transactions" }));
  expect(labels()).toEqual(["Chai Point", "BigBasket", "Amazon"]);
  await user.click(within(sort).getByRole("button", { name: "Name" }));
  expect(labels()).toEqual(["Amazon", "BigBasket", "Chai Point"]);
  expect(within(sort).getByRole("button", { name: "Name" })).toHaveAttribute("aria-pressed", "true");
});

test("the order picked stays when switching lanes, and Checks has nothing to sort", async () => {
  const { user, router } = setup([...mixed(), unnamed("UPI-RAVI"), unnamed("UPI-MEERA")], {}, "/review?lane=categories");
  await user.click(within(await screen.findByRole("group", { name: "Sort by" })).getByRole("button", { name: "Transactions" }));
  await user.click(screen.getByRole("tab", { name: /Payees/ }));
  expect(within(screen.getByRole("group", { name: "Sort by" })).getByRole("button", { name: "Transactions" }))
    .toHaveAttribute("aria-pressed", "true");
  expect(router.state.location.search).toContain("sort=count");
  await user.click(screen.getByRole("tab", { name: /Checks/ }));
  expect(screen.queryByRole("group", { name: "Sort by" })).not.toBeInTheDocument();
});

test("categorizing a payee shown by its bank text files it, though naming it regroups the list (user, 2026-10-21)", async () => {
  // Naming is step one; the review refresh after it moves the group from its text to the new payee id, which
  // closed the box before step two (the category) was sent: the pick did nothing until tried again.
  const raw = item({ id: "t-raw", payee_raw: "CHAI POINT KORAMANGALA", tags: ["uncategorized"] });
  const { api, user } = setup([raw], {
    "POST /api/review/name-payee": { status: 200, body: { updated: 1, payee_id: "p-new", pattern: true } },
    "POST /api/review/payee-category": { status: 200, body: { updated: 1, overrides_kept: 0 } },
    // the refresh after naming also reloads this; slower than the review, the box was gone before step two
    "GET /api/review/auto-categorize/latest": { ...NO_BATCH, delayMs: 150 } });
  await user.click(await screen.findByRole("tab", { name: /Categories 1/ }));
  await user.click(screen.getByRole("button", { name: "Choose a category" }));
  // what the server says after the naming: the same transaction, now under its payee
  api.set("GET /api/review", { status: 200, body: { items: [{ ...raw, payee_id: "p-new", payee_name: "CHAI POINT KORAMANGALA" }],
    categorized: [], sorted_accounts: [], sorted_payees: [] } });
  await user.click(screen.getByRole("option", { name: "Food" }));
  await waitFor(() => expect(api.bodiesFor("POST /api/review/payee-category")).toEqual([
    { payee_id: "p-new", category_id: "c-food" }]));
  expect(api.bodiesFor("POST /api/review/name-payee")).toEqual([{ payee_raw: "CHAI POINT KORAMANGALA", name: "CHAI POINT KORAMANGALA" }]);
  expect(await screen.findByRole("status")).toHaveTextContent("1 transaction updated.");
});

// ---- money in is plain to see (user, 2026-10-21): a credit is green with CR, a group's total is net ----

test("a credit says so, in its group's total and on its own line", async () => {
  const { user } = setup([uncategorized("p-d", "Deepanshu Mishra", { kind: "income", amount_paise: 26000 })],
    {}, "/review?lane=categories");
  await screen.findByRole("list", { name: /to sort/ });
  const [row] = rows();
  const total = row!.querySelector(".group__total")!;
  expect(total).toHaveTextContent("₹260.00");
  expect(total).toHaveTextContent("credit");  // read out, beside the CR tag
  expect(total.querySelector(".amt--credit")).not.toBeNull();
  await user.click(within(row!).getByRole("button", { name: /Show the transactions/ }));
  const line = within(row!).getByRole("list", { name: /Transactions in/ }).querySelector(".amt");
  expect(line).toHaveClass("amt--credit");
});

test("a group with money both ways totals the difference, and says how many were credits", async () => {
  setup([uncategorized("p-d", "Deepanshu Mishra", { amount_paise: 50000 }),
    uncategorized("p-d", "Deepanshu Mishra", { kind: "income", amount_paise: 26000 }),
    uncategorized("p-d", "Deepanshu Mishra", { amount_paise: 10000 })], {}, "/review?lane=categories");
  await screen.findByRole("list", { name: /to sort/ });
  const [row] = rows();
  expect(row!.querySelector(".group__total")).toHaveTextContent("₹340.00");  // 500 + 100 out, 260 in
  expect(row!.querySelector(".group__total .amt--credit")).toBeNull();      // net out: a debit
  expect(within(row!).getByText("3 transactions (1 credit)")).toBeInTheDocument();
});
