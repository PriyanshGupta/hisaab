import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";

import { goTo } from "../navigate";
import { mockApi, type Reply } from "../test/mockApi";
import { renderApp } from "../test/renderApp";

vi.mock("../navigate", () => ({ goTo: vi.fn() }));

const ME = { status: 200, body: { id: "u-1", username: "asha", display_name: "Asha", role: "member" } };
const GMAIL = { status: 200, body: { configured: true, status: "not_connected", email_address: null,
  last_successful_sync_at: null } };
const setupAt = (step: string, job: unknown = null) =>
  ({ status: 200, body: { step, backfill_start: "2026-07-11", job } });
const account = (over: Record<string, unknown> = {}) => ({
  id: "a-1", type: "bank", display_name: "HDFC Savings", institution: null, last4: "1111",
  opening_balance_paise: 1234500, opening_balance_date: "2026-07-11", archived_at: null,
  created_at: "2026-10-11T00:00:00.000Z", identifiers: [], ...over,
});
const job = (state: string, over: Record<string, unknown> = {}) => ({
  id: "j-1", kind: "backfill", state, progress: null, result: null, error: null,
  created_at: "2026-10-11T05:00:00.000Z", started_at: null, finished_at: null, ...over,
});

function setup(routes: Record<string, Reply>, path = "/setup") {
  const api = mockApi({ "GET /api/auth/me": ME, "GET /api/gmail": GMAIL, ...routes });
  const { router } = renderApp(path);
  return { api, router, user: userEvent.setup() };
}

test("a new user adds an account dated where the history starts, checks balances, then reaches Gmail", async () => {
  const { api, user } = setup({
    "GET /api/setup": setupAt("accounts"),
    "GET /api/accounts": { status: 200, body: [] },
    "POST /api/accounts": { status: 201, body: account() },
  });
  expect(await screen.findByRole("heading", { name: "Set up Hisaab" })).toBeInTheDocument();
  expect(await screen.findByLabelText("As of")).toHaveValue("2026-07-11"); // the server's backfill start
  await user.type(screen.getByLabelText("Name"), "HDFC Savings");
  await user.type(screen.getByLabelText("Last 4 digits"), "1111");
  await user.type(screen.getByLabelText("Balance"), "12345");
  api.set("GET /api/accounts", { status: 200, body: [account()] });
  api.set("GET /api/setup", setupAt("gmail"));
  await user.click(screen.getByRole("button", { name: "Add account" }));
  expect(api.bodiesFor("POST /api/accounts")[0]).toMatchObject({ opening_balance_date: "2026-07-11" });
  await user.click(await screen.findByRole("button", { name: "Continue" }));
  expect(await screen.findByRole("heading", { name: "Opening balances" })).toBeInTheDocument();
  expect(screen.getByText("HDFC Savings")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Continue" }));
  expect(await screen.findByRole("button", { name: "Connect Gmail" })).toBeInTheDocument();
});

test("a balance dated after the history starts holds the setup at balances", async () => {
  const { user } = setup({
    "GET /api/setup": setupAt("gmail"),
    "GET /api/accounts": { status: 200, body: [account({ opening_balance_date: "2026-10-01" })] },
  });
  await user.click(await screen.findByRole("button", { name: "Continue" }));
  expect(await screen.findByText(/after 11 Jul 2026/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Continue" })).toBeDisabled();
});

test("the Gmail step sends the person to Google's consent screen", async () => {
  const { user } = setup({
    "GET /api/setup": setupAt("gmail"),
    "GET /api/accounts": { status: 200, body: [account()] },
    "POST /api/gmail/connect": { status: 200, body: { authorization_url: "https://accounts.google.com/x" } },
  });
  await user.click(await screen.findByRole("button", { name: "Continue" }));
  await user.click(await screen.findByRole("button", { name: "Continue" }));
  await user.click(await screen.findByRole("button", { name: "Connect Gmail" }));
  await waitFor(() => expect(goTo).toHaveBeenCalledWith("https://accounts.google.com/x"));
});

test("the backfill shows its progress, then lands in bulk categorization", async () => {
  const { api, router } = setup({
    "GET /api/setup": setupAt("backfill", job("running", { progress: { fetched: 2, estimate: 4 } })),
    "GET /api/setup/uncategorized": { status: 200, body: { groups: [], unnamed: 0 } },
    "GET /api/categories": { status: 200, body: [] },
  });
  expect(await screen.findByText("Fetched 2 of ~4 emails")).toBeInTheDocument();
  api.set("GET /api/setup", setupAt("backfill", job("running", { progress: { read: 1, to_read: 4 } })));
  expect(await screen.findByText("Reading 1 of 4 emails", {}, { timeout: 3000 })).toBeInTheDocument();
  api.set("GET /api/setup", setupAt("categorize", job("succeeded")));
  expect(await screen.findByRole("heading", { name: "Categorize your spending" }, { timeout: 3000 }))
    .toBeInTheDocument();
  expect(router.state.location.pathname).toBe("/categorize");
});

test("a backfill that stopped shows why and resumes on request", async () => {
  const { api, user } = setup({
    "GET /api/setup": setupAt("backfill", job("failed", { error: "interrupted by shutdown" })),
    "POST /api/gmail/sync": { status: 202, body: { job_id: "j-2" } },
  });
  expect(await screen.findByRole("alert")).toHaveTextContent("interrupted by shutdown");
  await user.click(screen.getByRole("button", { name: "Resume" }));
  expect(api.calls).toContain("POST /api/gmail/sync");
});

test("coming back from Google having declined shows the Gmail step and says so", async () => {
  setup({ "GET /api/setup": setupAt("gmail"), "GET /api/accounts": { status: 200, body: [account()] } },
    "/setup?gmail=denied");
  expect(await screen.findByRole("alert")).toHaveTextContent("Nothing was connected");
  expect(screen.getByRole("button", { name: "Connect Gmail" })).toBeInTheDocument();
});

test("a backfill that ended with emails it couldn't fetch says how many and offers to resume", async () => {
  setup({ "GET /api/setup": setupAt("backfill", job("succeeded", { result: { failed: 2 } })) });
  expect(await screen.findByRole("alert")).toHaveTextContent("2 emails couldn't be fetched");
  expect(screen.getByRole("button", { name: "Resume" })).toBeInTheDocument();
});

test("once more emails arrive than Gmail estimated, the estimate is dropped rather than shown wrong", async () => {
  setup({ "GET /api/setup": setupAt("backfill", job("running", { progress: { fetched: 300, estimate: 201 } })) });
  expect(await screen.findByText("Fetched 300 emails")).toBeInTheDocument();
});
