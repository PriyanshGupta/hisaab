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

test("one Admin entry for admins, with the four sections", async () => {
  setup("/admin/users");
  expect(await screen.findByRole("link", { name: "Admin" })).toBeInTheDocument();
  const sections = screen.getByRole("navigation", { name: "Admin sections" });
  expect(within(sections).getAllByRole("link").map((l) => l.textContent)).toEqual(
    ["Templates", "Senders", "Payee names", "Users"]);  // re-sync moved to Settings (user, 2026-10-21)
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

test("a parser change shows a banner on every admin page, and re-syncs its range for every inbox", async () => {
  const { api, user } = setup("/admin/users", {
    "GET /api/admin/resync/notices": { status: 200, body: [notice] },
    "POST /api/admin/resync/notices/n-1/dismiss": { status: 204, body: null },
    "GET /api/admin/resync/preview": { status: 200, body: { transactions: 12, inboxes: 2 } },
    "POST /api/admin/resync": { status: 202, body: { jobs: ["j-1", "j-2"] } },
  });
  const banner = await screen.findByRole("region", { name: "Parser changes" });
  expect(within(banner).getByText(
    "lantern_card_charge was edited on 21 Oct 2026. Emails from 12 Jul 2026 to 18 Oct 2026 may now read differently.",
  )).toBeInTheDocument();
  await user.click(within(banner).getByRole("button", { name: "Re-sync that range" }));
  const ask = await within(banner).findByRole("group", { name: "Re-sync 12 Jul 2026 to 18 Oct 2026 for every inbox?" });
  expect(ask).toHaveTextContent("12 transactions may change. A backup is taken first.");
  expect(api.bodiesFor("POST /api/admin/resync")).toHaveLength(0);
  await user.click(within(ask).getByRole("button", { name: "Re-sync" }));
  expect(api.bodiesFor("POST /api/admin/resync")).toEqual([{ start: "2026-07-12", end: "2026-10-18", user_id: null }]);
  expect(await screen.findByRole("status")).toHaveTextContent("Re-sync started for 2 inboxes.");
  await user.click(within(banner).getByRole("button", { name: "Dismiss" }));
  expect(api.bodiesFor("POST /api/admin/resync/notices/n-1/dismiss")).toHaveLength(1);
});

test("the old Re-sync address leads to Settings, where it lives now", async () => {
  setup("/admin/resync", { "GET /api/gmail": { status: 200, body: { configured: true, status: "connected",
    email_address: "asha@example.test", last_successful_sync_at: null } } });
  expect(await screen.findByRole("heading", { name: "Settings", level: 1 })).toBeInTheDocument();
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
