import { cleanup, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { mockApi, type Reply } from "../test/mockApi";
import { renderApp } from "../test/renderApp";

const ADMIN = { status: 200, body: { id: "u-1", username: "asha", display_name: "Asha", role: "admin" } };
const MEMBER = { status: 200, body: { id: "u-2", username: "ravi", display_name: "Ravi", role: "member" } };

const ex = (subject: string, snippet: string, day: string) => ({ subject, snippet, received_at: `2026-10-${day}T04:30:00.000Z` });
const candidate = {
  id: "s-1", address: "alerts@harbour-bank.example", first_seen_at: "2026-10-18T04:30:00.000Z",
  found_in: [
    { user: "Asha", match_count: 4, examples: [ex("Debit alert", "Rs.1,250.00 has been debited from your account", "17"),
      ex("UPI credited", "Rs.300.00 has been credited", "12")] },
    { user: "Ravi", match_count: 1, examples: [ex("Debit alert", "Rs.89.50 has been debited", "15")] },
  ],
};
const queue = (over: Record<string, unknown> = {}) => ({
  status: 200, body: { candidates: [candidate], ignored: [], last_sweep_at: "2026-10-18T05:00:00.000Z", ...over },
});

function setup(routes: Record<string, Reply>, me: Reply = ADMIN, path = "/senders") {
  const api = mockApi({ "GET /api/auth/me": me, "GET /api/review/count": { status: 200, body: { count: 0 } }, ...routes });
  renderApp(path);
  return { api, user: userEvent.setup() };
}

test("a candidate shows whose inbox it came from and what the sweep saw", async () => {
  setup({ "GET /api/admin/senders": queue() });
  const sheet = await screen.findByRole("region", { name: "alerts@harbour-bank.example" });
  expect(within(sheet).getByText("Found in Asha's inbox, seen in 4 matching emails")).toBeInTheDocument();
  expect(within(sheet).getByText("Found in Ravi's inbox, seen in 1 matching email")).toBeInTheDocument();
  expect(within(sheet).getByText("Rs.1,250.00 has been debited from your account")).toBeInTheDocument();
  expect(within(sheet).getAllByText("Debit alert")).toHaveLength(2);
  expect(screen.getByText(/Last sweep 18 Oct 2026/)).toBeInTheDocument();
});

test("approving posts and says three months are being fetched", async () => {
  const { api, user } = setup({
    "GET /api/admin/senders": queue(),
    "POST /api/admin/senders/s-1/approve": { status: 200, body: { address: "alerts@harbour-bank.example", backfills: 2 } },
  });
  await user.click(await screen.findByRole("button", { name: "Approve" }));
  expect(await screen.findByText(
    "Approved alerts@harbour-bank.example. Its last three months of mail are being fetched for 2 inboxes.",
  )).toBeInTheDocument();
  expect(api.bodiesFor("POST /api/admin/senders/s-1/approve")).toHaveLength(1);
});

test("ignoring posts", async () => {
  const { api, user } = setup({
    "GET /api/admin/senders": queue(), "POST /api/admin/senders/s-1/ignore": { status: 204, body: null },
  });
  await user.click(await screen.findByRole("button", { name: "Ignore" }));
  expect(api.bodiesFor("POST /api/admin/senders/s-1/ignore")).toHaveLength(1);
});

test("run sweep now posts and says it started", async () => {
  const { api, user } = setup({
    "GET /api/admin/senders": queue({ candidates: [] }),
    "POST /api/admin/senders/sweep": { status: 202, body: { jobs: ["j-1", "j-2"] } },
  });
  await user.click(await screen.findByRole("button", { name: "Run sweep now" }));
  expect(await screen.findByText("Sweeping 2 inboxes. New senders appear here when it finishes.")).toBeInTheDocument();
  expect(api.bodiesFor("POST /api/admin/senders/sweep")).toHaveLength(1);
});

test("an empty queue says what the sweep does, and ignored senders are listed", async () => {
  setup({ "GET /api/admin/senders": queue({ candidates: [], last_sweep_at: null,
    ignored: [{ id: "i-1", address: "news@kite-mart.example", reviewed_at: "2026-10-19T08:00:00.000Z" }] }) });
  expect(await screen.findByText("No new senders.")).toBeInTheDocument();
  expect(screen.getByText(/Not swept yet/)).toBeInTheDocument();
  const ignored = screen.getByRole("region", { name: "Ignored" });
  expect(within(ignored).getByText("news@kite-mart.example")).toBeInTheDocument();
});

test("Senders is a section of the Admin area, for admins only", async () => {
  setup({ "GET /api/admin/senders": queue({ candidates: [] }) });
  expect(await screen.findByRole("link", { name: "Senders" })).toHaveAttribute("aria-current", "page");
  cleanup();
  setup({ "GET /api/admin/senders": { status: 403, body: { detail: "Only an admin can do this." } } }, MEMBER);
  expect(await screen.findByText("This area is for admins.")).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "Senders" })).not.toBeInTheDocument();
});

test("Settings links admins to the Admin area", async () => {
  setup({ "GET /api/gmail": { status: 200, body: { configured: true, status: "connected", email_address: "asha@example.test",
    last_successful_sync_at: "2026-10-11T04:30:00.000Z" } } }, ADMIN, "/settings");
  expect((await screen.findAllByRole("link", { name: "Admin" })).map((l) => l.getAttribute("href"))).toContain("/admin");
});
