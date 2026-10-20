import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { mockApi, type Reply } from "../test/mockApi";
import { renderApp } from "../test/renderApp";

const ADMIN = { status: 200, body: { id: "u-1", username: "asha", display_name: "Asha", role: "admin" } };
const MEMBER = { status: 200, body: { id: "u-2", username: "ravi", display_name: "Ravi", role: "member" } };

const row = (field: string, expected: unknown, got: unknown = expected) => ({ field, expected, got, match: expected === got });
const example = (n: number, payee: string, amount: number, reference: string) => ({
  source_id: `s-${n}`, subject: "You have done a UPI txn", received_at: `2026-10-0${n}T04:30:00.000Z`,
  text: `Dear Customer, Rs.${amount / 100}.00 has been debited from account **4321 to VPA shop@okbank ${payee} on 0${n}-10-26. Your UPI transaction reference number is ${reference}.`,
  fields: [row("outcome", "transaction"), row("amount", amount), row("account_identifier", "4321"),
    row("payee_raw", payee), row("reference_number", reference)],
});
const candidate = (over: Record<string, unknown> = {}) => ({
  id: "t-1", name: "lantern_upi_debit", status: "validated", passed: true, problems: [],
  sender: "alerts@lantern.example", subject_masked: "You have done a UPI txn", sightings: 4,
  first_seen_at: "2026-10-01T04:30:00.000Z", last_seen_at: "2026-10-04T04:30:00.000Z",
  spec: { version: 1, match: { sender: "alerts@lantern.example", subject: "^You have done a UPI txn" } },
  examples: [example(1, "Corner Grocer", 125000, "612345678901"), example(2, "Lamp House", 240000, "655544433322"),
    example(3, "Tea Stall", 31000, "677788899900")],
  ...over,
});

function setup(routes: Record<string, Reply>, me: Reply = ADMIN) {
  const api = mockApi({ "GET /api/auth/me": me, "GET /api/review/count": { status: 200, body: { count: 0 } },
    ...routes });
  renderApp("/templates");
  return { api, user: userEvent.setup() };
}

test("a candidate shows each email's text beside what Haiku and the template read", async () => {
  setup({ "GET /api/admin/templates": { status: 200, body: { candidates: [candidate()], demoted: [], excluded: [] } } });
  const sheet = await screen.findByRole("region", { name: /alerts@lantern.example/ });
  expect(within(sheet).getByText("Reads all three emails exactly as Haiku did.")).toBeInTheDocument();
  expect(within(sheet).getByText("Seen 4 times since 1 Oct 2026")).toBeInTheDocument();
  const first = within(sheet).getByRole("group", { name: "Email 1" });
  expect(within(first).getByText("Corner Grocer", { selector: "mark" })).toBeInTheDocument();  // found in the email
  const table = within(first).getByRole("table");
  const amount = within(table).getByRole("row", { name: /Amount/ });
  expect(within(amount).getAllByText("₹1,250.00")).toHaveLength(2);
  expect(within(amount).getByText("Match")).toBeInTheDocument();
  expect(within(sheet).getByRole("button", { name: "Approve template" })).toBeEnabled();
});

test("a failed candidate says why, names the mismatch and cannot be approved", async () => {
  const failed = candidate({ status: "proposed", passed: false, problems: ["email 2: reference_number differs"],
    examples: [example(1, "Corner Grocer", 125000, "612345678901"),
      { ...example(2, "Lamp House", 240000, "655544433322"),
        fields: [row("reference_number", "655544433322", "655544")] }, example(3, "Tea Stall", 31000, "677788899900")] });
  setup({ "GET /api/admin/templates": { status: 200, body: { candidates: [failed], demoted: [], excluded: [] } } });
  const sheet = await screen.findByRole("region", { name: /alerts@lantern.example/ });
  expect(within(sheet).getByText(/Didn't pass/)).toBeInTheDocument();
  expect(within(sheet).getByText("Email 2: reference differs")).toBeInTheDocument();
  const second = within(sheet).getByRole("group", { name: "Email 2" });
  const reference = within(second).getByRole("row", { name: /Reference/ });
  expect(within(reference).getByText("No match")).toBeInTheDocument();
  expect(within(sheet).getByRole("button", { name: "Approve template" })).toBeDisabled();
  expect(within(sheet).queryByRole("button", { name: "Reject" })).not.toBeInTheDocument();
});

test("approving posts and says what happened", async () => {
  const { api, user } = setup({
    "GET /api/admin/templates": { status: 200, body: { candidates: [candidate()], demoted: [], excluded: [] } },
    "POST /api/admin/templates/t-1/approve": { status: 200, body: { reread: 7, waiting: 0, invalidated: 7,
      exported: ["promoted/a_01.eml", "promoted/a_02.eml", "promoted/a_03.eml"], flagged: 1 } },
  });
  await user.click(await screen.findByRole("button", { name: "Approve template" }));
  expect(await screen.findByText(
    "Approved lantern_upi_debit. Re-read 7 emails with it; 1 went to Review because it read them differently. 3 examples exported to fixtures.",
  )).toBeInTheDocument();
  expect(api.bodiesFor("POST /api/admin/templates/t-1/approve")).toHaveLength(1);
});

test("reject posts", async () => {
  const { api, user } = setup({
    "GET /api/admin/templates": { status: 200, body: { candidates: [candidate()], demoted: [], excluded: [] } },
    "POST /api/admin/templates/t-1/reject": { status: 204, body: null },
  });
  await user.click(await screen.findByRole("button", { name: "Reject" }));
  expect(api.bodiesFor("POST /api/admin/templates/t-1/reject")).toHaveLength(1);
});

test("an empty queue says what will appear, and demoted templates are listed", async () => {
  setup({ "GET /api/admin/templates": { status: 200, body: { candidates: [], demoted: [
    { id: "d-1", name: "old_format", demoted_at: "2026-10-19T08:00:00.000Z", consecutive_failures: 3 }], excluded: [] } } });
  expect(await screen.findByText(/No templates waiting/)).toBeInTheDocument();
  const demoted = screen.getByRole("region", { name: "Demoted" });
  expect(within(demoted).getByText("old_format")).toBeInTheDocument();
  expect(within(demoted).getByText(/Stopped after 3 failed reads, 19 Oct/)).toBeInTheDocument();
});

test("the navigation offers Templates to an admin only", async () => {
  setup({ "GET /api/admin/templates": { status: 200, body: { candidates: [], demoted: [], excluded: [] } } });
  expect(await screen.findByRole("link", { name: "Templates" })).toBeInTheDocument();
});

test("a member sees no Templates link, and the page shows the server's refusal", async () => {
  setup({ "GET /api/admin/templates": { status: 403, body: { detail: "Only an admin can do this." } } }, MEMBER);
  expect(await screen.findByRole("alert")).toHaveTextContent("Only an admin can do this.");
  expect(screen.getAllByRole("link", { name: "Review" }).length).toBeGreaterThan(0);
  expect(screen.queryByRole("link", { name: "Templates" })).not.toBeInTheDocument();
});

test("a format treated as not a transaction can be read again", async () => {
  const { api, user } = setup({
    "GET /api/admin/templates": { status: 200, body: { candidates: [], demoted: [], excluded: [
      { id: "f-1", sender: "alerts@lantern.example", subject_masked: "Offer #", sightings: 5,
        last_seen_at: "2026-10-18T08:00:00.000Z" }] } },
    "POST /api/admin/templates/formats/f-1/include": { status: 204, body: null },
  });
  const section = await screen.findByRole("region", { name: "Treated as not transactions" });
  expect(within(section).getByText("Offer #")).toBeInTheDocument();
  expect(within(section).getByText(/alerts@lantern.example, seen 5 times/)).toBeInTheDocument();
  await user.click(within(section).getByRole("button", { name: "Read again" }));
  expect(api.bodiesFor("POST /api/admin/templates/formats/f-1/include")).toHaveLength(1);
});
