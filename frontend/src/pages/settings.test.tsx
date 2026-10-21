import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";

import { goTo } from "../navigate";
import { mockApi, type Reply } from "../test/mockApi";
import { renderApp } from "../test/renderApp";

vi.mock("../navigate", () => ({ goTo: vi.fn() }));

const ME = { status: 200, body: { id: "u-1", username: "asha", display_name: "Asha", role: "member" } };
const gmail = (over: Record<string, unknown> = {}) => ({
  status: 200,
  body: { configured: true, status: "connected", email_address: "asha@example.test",
    last_successful_sync_at: "2026-10-11T04:30:00.000Z", ...over },
});
const job = (state: string, over: Record<string, unknown> = {}) => ({
  status: 200,
  body: { id: "j-1", kind: "sync", state, result: null, error: null, created_at: "2026-10-11T05:00:00.000Z",
    started_at: null, finished_at: null, ...over },
});

function setup(path: string, routes: Record<string, Reply>) {
  const api = mockApi({ "GET /api/auth/me": ME, ...routes });
  renderApp(path);
  return { api, user: userEvent.setup() };
}

test("connecting sends the person to Google's consent screen", async () => {
  const { api, user } = setup("/settings", {
    "GET /api/gmail": gmail({ status: "not_connected", email_address: null, last_successful_sync_at: null }),
    "POST /api/gmail/connect": { status: 200, body: { authorization_url: "https://accounts.google.com/o/oauth2/v2/auth?x=1" } },
  });
  await user.click(await screen.findByRole("button", { name: "Connect Gmail" }));
  expect(api.calls).toContain("POST /api/gmail/connect");
  await waitFor(() => expect(goTo).toHaveBeenCalledWith("https://accounts.google.com/o/oauth2/v2/auth?x=1"));
});

test("a connected inbox shows its address and when it last synced", async () => {
  setup("/settings", { "GET /api/gmail": gmail() });
  expect(await screen.findByText(/asha@example\.test/)).toBeInTheDocument();
  expect(screen.getByText(/11 Oct 2026/)).toBeInTheDocument();
});

test("sync now polls the job and reports what it stored", async () => {
  const { api, user } = setup("/settings", {
    "GET /api/gmail": gmail(),
    "POST /api/gmail/sync": { status: 202, body: { job_id: "j-1" } },
    "GET /api/jobs/j-1": job("running"),
  });
  await user.click(await screen.findByRole("button", { name: "Sync now" }));
  expect(await screen.findByText("Syncing…")).toBeInTheDocument();
  api.set("GET /api/jobs/j-1", job("succeeded", {
    result: { stored: 3, already_had: 5, failed: 0, transactions_new: 2, needs_review: 1, duplicates: 1, pending_llm: 0 } }));
  expect(await screen.findByText("2 new transactions, 1 needs review, 1 duplicate skipped. 5 emails were already here.",
    {}, { timeout: 3000 }))
    .toBeInTheDocument();
});

test("a sync that could not fetch some messages says the next one retries them", async () => {
  const { user } = setup("/settings", {
    "GET /api/gmail": gmail(),
    "POST /api/gmail/sync": { status: 202, body: { job_id: "j-1" } },
    "GET /api/jobs/j-1": job("succeeded", {
      result: { stored: 1, already_had: 0, failed: 2, transactions_new: 1, needs_review: 0, pending_llm: 3 } }),
  });
  await user.click(await screen.findByRole("button", { name: "Sync now" }));
  expect(await screen.findByText(
    "1 new transaction, 0 need review. 2 emails couldn't be fetched and 3 are waiting to be read; the next sync retries them.",
  )).toBeInTheDocument();
});

test("a failed sync shows the job's sentence", async () => {
  const { user } = setup("/settings", {
    "GET /api/gmail": gmail(),
    "POST /api/gmail/sync": { status: 202, body: { job_id: "j-1" } },
    "GET /api/jobs/j-1": job("failed", { error: "Gmail access has expired or was revoked; reconnect Gmail in Settings" }),
  });
  await user.click(await screen.findByRole("button", { name: "Sync now" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("reconnect Gmail");
});

test("a revoked grant asks the person to reconnect", async () => {
  setup("/settings", { "GET /api/gmail": gmail({ status: "needs_reauth" }) });
  expect(await screen.findByRole("alert")).toHaveTextContent("Reconnect");
  expect(screen.getByRole("button", { name: "Reconnect Gmail" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Sync now" })).not.toBeInTheDocument();
});

test("an unconfigured server says so instead of offering to connect", async () => {
  setup("/settings", { "GET /api/gmail": gmail({ configured: false, status: "not_connected", email_address: null }) });
  expect(await screen.findByText(/isn't set up on this server/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Connect Gmail" })).not.toBeInTheDocument();
});

test("coming back from Google having declined says nothing was connected", async () => {
  setup("/settings?gmail=denied", {
    "GET /api/gmail": gmail({ status: "not_connected", email_address: null, last_successful_sync_at: null }),
  });
  expect(await screen.findByText(/Nothing was connected/)).toBeInTheDocument();
});

test("an admin gets a link to the Admin area; a member doesn't", async () => {
  mockApi({ "GET /api/auth/me": { ...ME, body: { ...ME.body, role: "admin" } }, "GET /api/gmail": gmail() });
  renderApp("/settings");
  expect((await screen.findAllByRole("link", { name: "Admin" })).map((l) => l.getAttribute("href"))).toContain("/admin");
  cleanup();
  setup("/settings", { "GET /api/gmail": gmail() });
  await screen.findByText("asha@example.test");
  expect(screen.queryByRole("link", { name: "Admin" })).not.toBeInTheDocument();
});


// --- sync a date range (user, 2026-10-21: here for everyone, was Admin's) ---------------------------------------

test("anyone can sync a date range of their own inbox, after seeing what it will do", async () => {
  const { api, user } = setup("/settings", {
    "GET /api/gmail": gmail(),
    "GET /api/gmail/resync/preview": { status: 200, body: { transactions: 12 } },
    "POST /api/gmail/resync": { status: 202, body: { job_id: "j-9" } },
    "GET /api/jobs/j-9": job("succeeded", { id: "j-9", kind: "resync",
      result: { transactions_new: 191, needs_review: 80, already_had: 0, failed: 0 } }),
  });
  const section = await screen.findByRole("region", { name: "Sync a date range" });
  fireEvent.change(within(section).getByLabelText("From"), { target: { value: "2026-06-01" } });
  fireEvent.change(within(section).getByLabelText("To"), { target: { value: "2026-07-20" } });
  await user.click(within(section).getByRole("button", { name: "Sync these dates…" }));
  const ask = await within(section).findByRole("group", { name: "Sync 1 Jun 2026 to 20 Jul 2026?" });
  expect(ask).toHaveTextContent("12 transactions already here are read again");
  expect(ask).toHaveTextContent("A backup is taken first.");
  expect(api.urls.some((u) => u.includes("/api/gmail/resync/preview?start=2026-06-01&end=2026-07-20"))).toBe(true);
  expect(api.bodiesFor("POST /api/gmail/resync")).toHaveLength(0);  // only the confirmation starts it
  await user.click(within(ask).getByRole("button", { name: "Sync" }));
  expect(api.bodiesFor("POST /api/gmail/resync")).toEqual([{ start: "2026-06-01", end: "2026-07-20" }]);
  expect(await within(section).findByText("191 new transactions, 80 need review.")).toBeInTheDocument();
});

test("the date-range sync waits until Gmail is connected", async () => {
  setup("/settings", { "GET /api/gmail": gmail({ status: "not_connected", email_address: null,
    last_successful_sync_at: null }) });
  await screen.findByRole("button", { name: "Connect Gmail" });
  expect(screen.queryByRole("region", { name: "Sync a date range" })).not.toBeInTheDocument();
});

test("a date-range sync the server refuses says why", async () => {
  const { user } = setup("/settings", {
    "GET /api/gmail": gmail(),
    "GET /api/gmail/resync/preview": { status: 422, body: { detail: "Pick up to 6 months at a time; sync a longer stretch in parts." } },
  });
  const section = await screen.findByRole("region", { name: "Sync a date range" });
  await user.click(within(section).getByRole("button", { name: "Sync these dates…" }));
  expect(await within(section).findByRole("alert")).toHaveTextContent("Pick up to 6 months at a time");
});
