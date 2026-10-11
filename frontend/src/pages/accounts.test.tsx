import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { mockApi } from "../test/mockApi";
import { renderApp } from "../test/renderApp";

const ME = { status: 200, body: { id: "u-1", username: "asha", display_name: "Asha", role: "admin" } };

const bank = {
  id: "a-1", type: "bank", display_name: "HDFC Savings", institution: "HDFC Bank", last4: "5512",
  opening_balance_paise: 8420000, opening_balance_date: "2026-07-11", archived_at: null,
  created_at: "2026-10-11T08:00:00.000Z",
  identifiers: [
    { id: "i-1", kind: "last4", identifier: "5512", removable: false },
    { id: "i-2", kind: "vpa", identifier: "asha@ybl", removable: true },
  ],
};
const card = {
  ...bank, id: "a-2", type: "credit_card", display_name: "Regalia card", last4: "8830",
  opening_balance_paise: 4700000, identifiers: [{ id: "i-3", kind: "card_last4", identifier: "8830", removable: false }],
};
const oldAccount = {
  // Archived at 01:30 India time on 1 Sep, which is still 31 Aug in UTC.
  ...bank, id: "a-3", display_name: "Old ICICI", last4: "2218", archived_at: "2026-08-31T20:00:00.000Z", identifiers: [],
};

function setup(accounts: unknown[] = [bank, card, oldAccount]) {
  const api = mockApi({ "GET /api/auth/me": ME, "GET /api/accounts": { status: 200, body: accounts } });
  renderApp("/accounts");
  return { api, user: userEvent.setup() };
}

async function openAddForm(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: "Add account" }));
  return screen.getByRole("form", { name: "Add account" });
}

test("lists active accounts with their opening anchors; archived ones are hidden", async () => {
  setup();
  const hdfc = await screen.findByRole("article", { name: "HDFC Savings" });
  expect(within(hdfc).getByText("Bank account ending 5512")).toBeInTheDocument();
  expect(within(hdfc).getByText("₹84,200.00")).toBeInTheDocument();
  expect(within(hdfc).getByText("asha@ybl")).toBeInTheDocument();
  expect(within(screen.getByRole("article", { name: "Regalia card" })).getByText(/owed on 11 Jul 2026/)).toBeInTheDocument();
  expect(screen.queryByRole("article", { name: "Old ICICI" })).not.toBeInTheDocument();
});

test("archived accounts appear only when asked for", async () => {
  const { user } = setup();
  await user.click(await screen.findByRole("button", { name: "Show archived (1)" }));
  const old = screen.getByRole("article", { name: "Old ICICI" });
  expect(within(old).getByText(/Archived 1 Sep 2026/)).toBeInTheDocument();
  expect(within(old).queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
});

test("adding a bank account sends integer paise", async () => {
  const { api, user } = setup([]);
  api.set("POST /api/accounts", { status: 201, body: bank });
  const form = await openAddForm(user);
  await user.type(within(form).getByLabelText("Name"), "HDFC Savings");
  await user.type(within(form).getByLabelText("Bank or card issuer (optional)"), "HDFC Bank");
  await user.type(within(form).getByLabelText("Last 4 digits"), "5512");
  await user.type(within(form).getByLabelText("Balance"), "84,200");
  await user.clear(within(form).getByLabelText("As of"));
  await user.type(within(form).getByLabelText("As of"), "2026-07-11");
  await user.click(within(form).getByRole("button", { name: "Add account" }));
  await screen.findByRole("button", { name: "Add account" }); // form closed
  expect(api.bodiesFor("POST /api/accounts")).toEqual([{
    type: "bank", display_name: "HDFC Savings", institution: "HDFC Bank", last4: "5512",
    opening_balance_paise: 8420000, opening_balance_date: "2026-07-11",
  }]);
});

test("a card asks for the amount owed and sends it positive", async () => {
  const { api, user } = setup([]);
  api.set("POST /api/accounts", { status: 201, body: card });
  const form = await openAddForm(user);
  await user.click(within(form).getByRole("radio", { name: "Credit card" }));
  expect(within(form).getByText(/Enter it as a positive number/)).toBeInTheDocument();
  await user.type(within(form).getByLabelText("Name"), "Regalia card");
  await user.type(within(form).getByLabelText("Last 4 digits"), "8830");
  await user.type(within(form).getByLabelText("Amount you owed"), "47,000");
  await user.click(within(form).getByRole("button", { name: "Add account" }));
  await screen.findByRole("button", { name: "Add account" });
  const [sent] = api.bodiesFor("POST /api/accounts") as { type: string; opening_balance_paise: number }[];
  expect([sent!.type, sent!.opening_balance_paise]).toEqual(["credit_card", 4700000]);
});

test("cash has no last 4 digits field", async () => {
  const { user } = setup([]);
  const form = await openAddForm(user);
  await user.click(within(form).getByRole("radio", { name: "Cash" }));
  expect(within(form).queryByLabelText("Last 4 digits")).not.toBeInTheDocument();
  expect(within(form).getByLabelText("Cash in hand")).toBeInTheDocument();
});

test("a bad amount is caught before anything is sent", async () => {
  const { api, user } = setup([]);
  const form = await openAddForm(user);
  await user.type(within(form).getByLabelText("Name"), "HDFC Savings");
  await user.type(within(form).getByLabelText("Last 4 digits"), "5512");
  await user.type(within(form).getByLabelText("Balance"), "12.345");
  await user.click(within(form).getByRole("button", { name: "Add account" }));
  expect(within(form).getByText("Use at most 2 decimal places.")).toBeInTheDocument();
  expect(api.calls).not.toContain("POST /api/accounts");
});

test("a duplicate last 4 shows the server's explanation", async () => {
  const { api, user } = setup([bank]);
  api.set("POST /api/accounts", { status: 409, body: { detail: "You already have an account ending 5512: HDFC Savings." } });
  const form = await openAddForm(user);
  await user.click(within(form).getByRole("radio", { name: "Credit card" }));
  await user.type(within(form).getByLabelText("Name"), "Some card");
  await user.type(within(form).getByLabelText("Last 4 digits"), "5512");
  await user.type(within(form).getByLabelText("Amount you owed"), "0");
  await user.click(within(form).getByRole("button", { name: "Add account" }));
  expect(await within(form).findByRole("alert")).toHaveTextContent("You already have an account ending 5512: HDFC Savings.");
});

test("archiving asks first, then the account leaves the list", async () => {
  const { api, user } = setup([bank, card]);
  api.set("POST /api/accounts/a-1/archive", { status: 200, body: { ...bank, archived_at: "2026-10-11T09:00:00.000Z" } });
  const hdfc = await screen.findByRole("article", { name: "HDFC Savings" });
  await user.click(within(hdfc).getByRole("button", { name: "Archive" }));
  expect(within(hdfc).getByText(/stops matching bank alerts/)).toBeInTheDocument();
  expect(api.calls).not.toContain("POST /api/accounts/a-1/archive");
  api.set("GET /api/accounts", { status: 200, body: [{ ...bank, archived_at: "2026-10-11T09:00:00.000Z" }, card] });
  await user.click(within(hdfc).getByRole("button", { name: "Archive account" }));
  await screen.findByRole("button", { name: "Show archived (1)" });
  expect(screen.queryByRole("article", { name: "HDFC Savings" })).not.toBeInTheDocument();
  expect(api.calls).toContain("POST /api/accounts/a-1/archive");
});

test("a UPI ID can be added to an account", async () => {
  const { api, user } = setup([bank]);
  api.set("POST /api/accounts/a-1/identifiers", { status: 201, body: { id: "i-9", kind: "vpa", identifier: "asha.k@okhdfc", removable: true } });
  const hdfc = await screen.findByRole("article", { name: "HDFC Savings" });
  expect(within(hdfc).queryByLabelText("Add a UPI ID to HDFC Savings")).not.toBeInTheDocument(); // quiet until asked
  await user.click(within(hdfc).getByRole("button", { name: "Add UPI ID" }));
  await user.type(within(hdfc).getByLabelText("Add a UPI ID to HDFC Savings"), "Asha.K@okHDFC");
  await user.click(within(hdfc).getByRole("button", { name: "Add" }));
  expect(api.bodiesFor("POST /api/accounts/a-1/identifiers")).toEqual([{ kind: "vpa", identifier: "Asha.K@okHDFC" }]);
  await waitFor(() => expect(within(hdfc).queryByLabelText("Add a UPI ID to HDFC Savings")).not.toBeInTheDocument());
});

test("editing corrects the opening anchor", async () => {
  const { api, user } = setup([bank]);
  api.set("PATCH /api/accounts/a-1", { status: 200, body: { ...bank, opening_balance_paise: 9000000 } });
  const hdfc = await screen.findByRole("article", { name: "HDFC Savings" });
  await user.click(within(hdfc).getByRole("button", { name: "Edit" }));
  const form = within(hdfc).getByRole("form", { name: "Edit HDFC Savings" });
  expect(within(form).getByLabelText("Balance")).toHaveValue("84,200.00");
  await user.clear(within(form).getByLabelText("Balance"));
  await user.type(within(form).getByLabelText("Balance"), "90,000");
  await user.click(within(form).getByRole("button", { name: "Save changes" }));
  await within(hdfc).findByRole("button", { name: "Edit" });
  expect(api.bodiesFor("PATCH /api/accounts/a-1")).toEqual([{
    display_name: "HDFC Savings", institution: "HDFC Bank", opening_balance_paise: 9000000, opening_balance_date: "2026-07-11",
  }]);
});

test("the nav now offers Accounts", async () => {
  setup([]);
  expect((await screen.findAllByRole("link", { name: "Accounts" })).length).toBeGreaterThan(0);
});

test("a card debt typed as a negative number is refused, never sent", async () => {
  const { api, user } = setup([]);
  const form = await openAddForm(user);
  await user.click(within(form).getByRole("radio", { name: "Credit card" }));
  await user.type(within(form).getByLabelText("Name"), "Regalia card");
  await user.type(within(form).getByLabelText("Last 4 digits"), "8830");
  await user.type(within(form).getByLabelText("Amount you owed"), "-47,000");
  await user.click(within(form).getByRole("button", { name: "Add account" }));
  expect(within(form).getByText("Enter the amount owed as a positive number.")).toBeInTheDocument();
  expect(api.calls).not.toContain("POST /api/accounts");
});
