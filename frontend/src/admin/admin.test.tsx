import { cleanup, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { mockApi, type Reply } from "../test/mockApi";
import { renderApp } from "../test/renderApp";

const ADMIN = { status: 200, body: { id: "u-1", username: "asha", display_name: "Asha", role: "admin" } };
const MEMBER = { status: 200, body: { id: "u-2", username: "ravi", display_name: "Ravi", role: "member" } };
const notice = { id: "n-1", template_name: "lantern_card_charge", reason: "edited", range_from: "2026-07-12",
  range_to: "2026-10-18", created_at: "2026-10-21T04:00:00.000Z" };
const users = [
  { id: "u-1", username: "asha", display_name: "Asha", role: "admin", created_at: "2026-10-10T00:00:00.000Z" },
  { id: "u-2", username: "ravi", display_name: "Ravi", role: "member", created_at: "2026-10-12T00:00:00.000Z" },
];

function setup(path: string, routes: Record<string, Reply> = {}, me: Reply = ADMIN) {
  const api = mockApi({ "GET /api/auth/me": me, "GET /api/review/count": { status: 200, body: { count: 0 } },
    "GET /api/admin/resync/notices": { status: 200, body: [] },
    "GET /api/admin/users": { status: 200, body: users },
    "GET /api/admin/templates": { status: 200, body: { candidates: [], demoted: [], excluded: [] } },
    "GET /api/admin/templates/active": { status: 200, body: [] },
    "GET /api/admin/senders": { status: 200, body: { candidates: [], ignored: [], last_sweep_at: null } },
    "GET /api/admin/payee-names": { status: 200, body: [] },
    ...routes });
  renderApp(path);
  return { api, user: userEvent.setup() };
}

test("one Admin entry for admins, with the five sections", async () => {
  setup("/admin/users");
  expect(await screen.findByRole("link", { name: "Admin" })).toBeInTheDocument();
  const sections = screen.getByRole("navigation", { name: "Admin sections" });
  expect(within(sections).getAllByRole("link").map((l) => l.textContent)).toEqual(
    ["Templates", "Senders", "Payee names", "Users", "Re-sync"]);
  expect(within(sections).getByRole("link", { name: "Users" })).toHaveAttribute("aria-current", "page");
});

test("members see no Admin entry", async () => {
  setup("/settings", { "GET /api/gmail": { status: 200, body: { configured: true, status: "connected",
    email_address: "ravi@example.test", last_successful_sync_at: null } } }, MEMBER);
  await screen.findByText("ravi@example.test");
  expect(screen.queryByRole("link", { name: "Admin" })).not.toBeInTheDocument();
});

test("the old addresses lead to their sections", async () => {
  setup("/templates");
  expect(await screen.findByRole("heading", { name: "Templates", level: 1 })).toBeInTheDocument();
  cleanup();
  setup("/senders");
  expect(await screen.findByRole("heading", { name: "Senders", level: 1 })).toBeInTheDocument();
});

test("a parser change shows a banner on every admin page, with its range", async () => {
  const { api, user } = setup("/admin/users", {
    "GET /api/admin/resync/notices": { status: 200, body: [notice] },
    "POST /api/admin/resync/notices/n-1/dismiss": { status: 204, body: null },
    "GET /api/admin/resync/preview": { status: 200, body: { transactions: 12, inboxes: 1 } },
  });
  const banner = await screen.findByRole("region", { name: "Parser changes" });
  expect(within(banner).getByText(
    "lantern_card_charge was edited on 21 Oct 2026. Emails from 12 Jul 2026 to 18 Oct 2026 may now read differently.",
  )).toBeInTheDocument();
  await user.click(within(banner).getByRole("button", { name: "Dismiss" }));
  expect(api.bodiesFor("POST /api/admin/resync/notices/n-1/dismiss")).toHaveLength(1);
  await user.click(within(banner).getByRole("link", { name: "Re-sync that range" }));
  expect(await screen.findByRole("heading", { name: "Re-sync", level: 1 })).toBeInTheDocument();
  expect(screen.getByLabelText("From")).toHaveValue("2026-07-12");
  expect(screen.getByLabelText("To")).toHaveValue("2026-10-18");
});

test("users are listed, and an admin adds one", async () => {
  const { api, user } = setup("/admin/users", {
    "POST /api/admin/users": { status: 201, body: { id: "u-3", username: "meera", display_name: "Meera", role: "member",
      created_at: "2026-10-21T00:00:00.000Z" } },
  });
  const list = await screen.findByRole("list", { name: "People" });
  expect(within(list).getByText("Asha")).toBeInTheDocument();
  expect(within(list).getByText("ravi, member")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Add a person" }));
  await user.type(screen.getByLabelText("Name"), "Meera");
  await user.type(screen.getByLabelText("Username"), "meera");
  await user.type(screen.getByLabelText("Password"), "meera password 1");
  await user.click(screen.getByRole("button", { name: "Add" }));
  expect(api.bodiesFor("POST /api/admin/users")).toEqual([
    { display_name: "Meera", username: "meera", password: "meera password 1", role: "member" }]);
});

test("resetting a password and changing a role, with the last admin's refusal", async () => {
  const { api, user } = setup("/admin/users", {
    "POST /api/admin/users/u-2/password": { status: 204, body: null },
    "PATCH /api/admin/users/u-1": { status: 409, body: { detail: "There must be at least one admin." } },
  });
  const ravi = await screen.findByRole("listitem", { name: "Ravi" });
  await user.click(within(ravi).getByRole("button", { name: "Reset password" }));
  await user.type(within(ravi).getByLabelText("New password for Ravi"), "brand new pass");
  await user.click(within(ravi).getByRole("button", { name: "Set password" }));
  expect(api.bodiesFor("POST /api/admin/users/u-2/password")).toEqual([{ password: "brand new pass" }]);
  expect(await within(ravi).findByText("Password changed. Ravi is signed out everywhere.")).toBeInTheDocument();
  const asha = screen.getByRole("listitem", { name: "Asha" });
  await user.click(within(asha).getByRole("button", { name: "Make member" }));
  expect(await within(asha).findByRole("alert")).toHaveTextContent("There must be at least one admin.");
});


// --- Payee names (PRD §5.6) ------------------------------------------------------------------------------------

const correction = { key: "upicornergrocerokbank", raw: "UPI-CORNERGROCER-OKBANK", payee_id: "p-1",
  payee_name: "Corner Grocer", shared: 12, reporters: [{ user: "Asha", count: 3 }, { user: "Ravi", count: 1 }] };

test("a captured correction shows the raw text, the name, how many share it and who reported it", async () => {
  setup("/admin/payee-names", { "GET /api/admin/payee-names": { status: 200, body: [correction] } });
  const sheet = await screen.findByRole("region", { name: "UPI-CORNERGROCER-OKBANK" });
  expect(within(sheet).getByText("Corner Grocer")).toBeInTheDocument();
  expect(within(sheet).getByText("12 transactions carry this text. Reported by Asha (3) and Ravi (1).")).toBeInTheDocument();
});

test("promoting as it is, and after editing it", async () => {
  const { api, user } = setup("/admin/payee-names", {
    "GET /api/admin/payee-names": { status: 200, body: [correction] },
    "POST /api/admin/payee-names/promote": { status: 200, body: { pattern_id: "np-1", named: 11, users: 2 } },
  });
  await user.click(await screen.findByRole("button", { name: "Promote" }));
  expect(api.bodiesFor("POST /api/admin/payee-names/promote")).toEqual([
    { match_type: "exact", pattern: "UPI-CORNERGROCER-OKBANK", payee_id: "p-1" }]);
  expect(await screen.findByText(
    "Promoted. 11 transactions for 2 people now read Corner Grocer. Nothing was fetched from Gmail.")).toBeInTheDocument();
});

test("edit before promoting: type, pattern and name, with the server's refusal shown", async () => {
  const { api, user } = setup("/admin/payee-names", {
    "GET /api/admin/payee-names": { status: 200, body: [correction] },
    "POST /api/admin/payee-names/promote": { status: 422, body: { detail: "That regular expression can't be used: x" } },
  });
  await user.click(await screen.findByRole("button", { name: "Edit before promoting" }));
  await user.selectOptions(screen.getByLabelText("Match"), "regex");
  await user.clear(screen.getByLabelText("Pattern"));
  await user.type(screen.getByLabelText("Pattern"), "CORNER.?GROCER");
  await user.clear(screen.getByLabelText("Payee name"));
  await user.type(screen.getByLabelText("Payee name"), "Corner Grocery");
  await user.click(screen.getByRole("button", { name: "Promote this pattern" }));
  expect(api.bodiesFor("POST /api/admin/payee-names/promote")).toEqual([
    { match_type: "regex", pattern: "CORNER.?GROCER", payee_name: "Corner Grocery" }]);
  expect(await screen.findByRole("alert")).toHaveTextContent("That regular expression can't be used");
});

test("dismissing posts, and an empty list says where corrections come from", async () => {
  const { api, user } = setup("/admin/payee-names", {
    "GET /api/admin/payee-names": { status: 200, body: [correction] },
    "POST /api/admin/payee-names/dismiss": { status: 204, body: null },
  });
  await user.click(await screen.findByRole("button", { name: "Dismiss" }));
  expect(api.bodiesFor("POST /api/admin/payee-names/dismiss")).toEqual([{ key: "upicornergrocerokbank", payee_id: "p-1" }]);
  cleanup();
  setup("/admin/payee-names");
  expect(await screen.findByText("No corrections waiting.")).toBeInTheDocument();
});

// --- Re-sync (tech-design §5.2) ---------------------------------------------------------------------------------

test("re-sync previews the count and only the confirmation's button starts it", async () => {
  const { api, user } = setup("/admin/resync?from=2026-10-05&to=2026-10-07", {
    "GET /api/admin/resync/preview": { status: 200, body: { transactions: 37, inboxes: 2 } },
    "POST /api/admin/resync": { status: 202, body: { jobs: ["j-1", "j-2"] } },
  });
  await user.click(await screen.findByRole("button", { name: "Re-sync…" }));
  const dialog = await screen.findByRole("dialog");
  expect(within(dialog).getByRole("heading")).toHaveTextContent("Re-sync 5 Oct 2026 to 7 Oct 2026 for all inboxes?");
  expect(within(dialog).getByText("37 transactions may change.")).toBeInTheDocument();
  expect(within(dialog).getByText(/A backup is taken first\./)).toBeInTheDocument();
  expect(api.bodiesFor("POST /api/admin/resync")).toHaveLength(0);
  await user.click(within(dialog).getByRole("button", { name: "Re-sync" }));
  expect(api.bodiesFor("POST /api/admin/resync")).toEqual([{ start: "2026-10-05", end: "2026-10-07", user_id: null }]);
  expect(await screen.findByText("Re-sync started for 2 inboxes. Changes appear as each one finishes.")).toBeInTheDocument();
});

test("re-sync for one person's inbox names them", async () => {
  const { api, user } = setup("/admin/resync?from=2026-10-05&to=2026-10-07", {
    "GET /api/admin/resync/preview": { status: 200, body: { transactions: 4, inboxes: 1 } },
  });
  await screen.findByRole("option", { name: "Ravi" });
  await user.selectOptions(screen.getByLabelText("Inbox"), "u-2");
  await user.click(screen.getByRole("button", { name: "Re-sync…" }));
  expect(within(await screen.findByRole("dialog")).getByRole("heading")).toHaveTextContent("for Ravi's inbox?");
  expect(api.urls.some((u) => u.includes("user_id=u-2"))).toBe(true);
});

// --- Templates in use (slice 24) --------------------------------------------------------------------------------

const learned = { id: "t-1", name: "lantern_card_charge", origin: "llm_proposed", editable: true,
  spec: { version: 1, match: { sender: "alerts@lantern.example", subject: "^Card charge" } }, emails_read: 7,
  last_read_at: "2026-10-18T04:00:00.000Z" };
const shipped = { ...learned, id: "t-2", name: "example_debit", origin: "human", editable: false, emails_read: 120 };

test("templates in use: a learned one is edited, a shipped one explains where it lives", async () => {
  const { api, user } = setup("/admin/templates", {
    "GET /api/admin/templates/active": { status: 200, body: [learned, shipped] },
    "PUT /api/admin/templates/t-1/spec": { status: 422, body: { detail: "match.subject: missing" } },
  });
  const inUse = await screen.findByRole("region", { name: "Templates in use" });
  const row = within(inUse).getByRole("group", { name: "lantern_card_charge" });
  expect(within(row).getByText("Learned here, read 7 emails")).toBeInTheDocument();
  await user.click(within(row).getByRole("button", { name: "Edit" }));
  const box = within(row).getByLabelText("Spec for lantern_card_charge");
  await user.clear(box);
  await user.type(box, '{{"version": 1}');
  await user.click(within(row).getByRole("button", { name: "Save" }));
  expect(api.bodiesFor("PUT /api/admin/templates/t-1/spec")).toEqual([{ spec: { version: 1 } }]);
  expect(await within(row).findByRole("alert")).toHaveTextContent("match.subject: missing");
  const other = within(inUse).getByRole("group", { name: "example_debit" });
  expect(within(other).queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
  expect(within(other).getByText(/its file is in the repository/)).toBeInTheDocument();
});

test("a spec that isn't JSON is caught before saving", async () => {
  const { api, user } = setup("/admin/templates", { "GET /api/admin/templates/active": { status: 200, body: [learned] } });
  const row = await screen.findByRole("group", { name: "lantern_card_charge" });
  await user.click(within(row).getByRole("button", { name: "Edit" }));
  const box = within(row).getByLabelText("Spec for lantern_card_charge");
  await user.clear(box);
  await user.type(box, "not json");
  await user.click(within(row).getByRole("button", { name: "Save" }));
  expect(within(row).getByRole("alert")).toHaveTextContent("isn't valid JSON");
  expect(api.bodiesFor("PUT /api/admin/templates/t-1/spec")).toHaveLength(0);
});


test("re-sync that range refills the form even when already on Re-sync", async () => {
  const other = { ...notice, id: "n-2", template_name: "lantern_upi_credit", range_from: "2026-08-01", range_to: "2026-08-31" };
  const { user } = setup("/admin/resync?from=2026-07-12&to=2026-10-18",
    { "GET /api/admin/resync/notices": { status: 200, body: [notice, other] } });
  expect(await screen.findByLabelText("From")).toHaveValue("2026-07-12");
  const banner = await screen.findByRole("region", { name: "Parser changes" });
  await user.click(within(banner).getAllByRole("link", { name: "Re-sync that range" })[1]!);
  expect(await screen.findByDisplayValue("2026-08-01")).toBeInTheDocument();
  expect(screen.getByLabelText("To")).toHaveValue("2026-08-31");
});
