import { screen, waitFor, within } from "@testing-library/react";
import { vi } from "vitest";
import userEvent from "@testing-library/user-event";

import { mockApi, type Reply } from "../test/mockApi";
import { renderApp } from "../test/renderApp";

const ME = { status: 200, body: { id: "u-1", username: "asha", display_name: "Asha", role: "member" } };
const acct = (id: string, name: string, type: string) => ({
  id, type, display_name: name, institution: null, last4: null, opening_balance_paise: null,
  opening_balance_date: null, archived_at: null, created_at: "2026-07-01T00:00:00.000Z", identifiers: [],
});
const ACCOUNTS = { status: 200, body: [acct("a-1", "HDFC Savings", "bank"), acct("a-2", "Other Bank", "bank"),
  acct("c-1", "Millenia", "credit_card")] };

const diagnostic = (over: Record<string, unknown> = {}) => ({
  outcome: "passed", template_name: "Example statement", pages_processed: 2, rows_extracted: 3, rows_skipped: 0,
  rows_unparseable: 0, declared: { opening: 100000, closing: 85000 },
  computed: { credits: 50000, debits: 65000, closing: 85000 }, gap_paise: 0, unparseable_rows: [], ...over,
});
const line = (index: number, narration: string, amount: number, direction = "debit") => ({
  index, page: 1, date: "2026-09-0" + (index + 1), time: null, narration, reference: null, direction,
  amount_paise: amount, payment: false,
});
const preview = (over: Record<string, unknown> = {}) => ({
  id: "s-1", filename: "sept.pdf", template_name: "Example statement", created_at: "2026-10-19T04:30:00.000Z",
  expires_at: "2026-10-20T04:30:00.000Z", account_id: "a-1", account_name: "HDFC Savings", account_kind: "bank",
  new: [line(0, "UPI-TEA STALL", 4000), line(1, "SALARY", 50000, "credit")],
  matches: [{ ...line(2, "UPI-CORNERSHOP", 61000), transaction_id: "t-9", current_amount_paise: 60000,
    current_date: "2026-09-03", payee: "Corner Shop", edited: ["amount"] }],
  unparseable: [], diagnostic: diagnostic(), ...over,
});
const job = (state: string, over: Record<string, unknown> = {}) => ({
  status: 200, body: { id: "j-1", kind: "statement_merge", state, progress: null, result: null, error: null,
    created_at: "2026-10-19T05:00:00.000Z", started_at: null, finished_at: null, ...over },
});

function setup(path: string, routes: Record<string, Reply>) {
  const api = mockApi({ "GET /api/auth/me": ME, "GET /api/accounts": ACCOUNTS,
    "GET /api/statements": { status: 200, body: [] }, "GET /api/review/count": { status: 200, body: { count: 0 } },
    ...routes });
  renderApp(path);
  return { api, user: userEvent.setup() };
}

test("uploading sends the file and its password, then shows the three groups", async () => {
  const { api, user } = setup("/statements", {
    "POST /api/statements": { status: 201, body: preview() },
    "GET /api/statements/s-1": { status: 200, body: preview() },
  });
  await user.upload(await screen.findByLabelText("Statement PDF"), new File(["%PDF-1.4 hi"], "sept.pdf"));
  await user.type(screen.getByLabelText("Password"), "secret 1");
  await user.click(screen.getByRole("button", { name: "Read statement" }));
  expect(await screen.findByRole("heading", { name: "HDFC Savings" })).toBeInTheDocument();
  expect(api.bodiesFor("POST /api/statements")).toEqual([
    { filename: "sept.pdf", password: "secret 1", content_base64: btoa("%PDF-1.4 hi") }]);
  expect(screen.getByRole("heading", { name: "2 new transactions" })).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: /1 already here/ })).toBeInTheDocument();
  expect(screen.getByText("Every row was read.")).toBeInTheDocument();
  const match = screen.getByRole("region", { name: /1 already here/ });
  expect(within(match).getByText("Your edit stays: ₹600.00")).toBeInTheDocument();
  expect(screen.getByText("Parsed 3 rows. They add up to the statement's closing balance of ₹850.00.")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Merge 3 rows into HDFC Savings" })).toBeInTheDocument();
});

test("a refused upload shows the server's sentence under the form", async () => {
  const { user } = setup("/statements", {
    "POST /api/statements": { status: 422, body: { detail: "That password doesn't open this statement." } },
  });
  await user.upload(await screen.findByLabelText("Statement PDF"), new File(["x"], "sept.pdf"));
  await user.click(screen.getByRole("button", { name: "Read statement" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("That password doesn't open this statement.");
});

test("when balances can't be read the preview says so and shows no sum", async () => {
  setup("/statements?open=s-1", { "GET /api/statements/s-1": { status: 200, body: preview({
    diagnostic: diagnostic({ outcome: "unavailable", declared: { opening: null, closing: null }, gap_paise: null,
      computed: { credits: 50000, debits: 65000, closing: null } }) }) } });
  expect(await screen.findByText(/couldn't be checked against it/)).toBeInTheDocument();
  expect(screen.getByText("Read through the rows before merging.")).toBeInTheDocument();
  expect(screen.queryByText("The rows come to")).not.toBeInTheDocument();
});

test("a failed check blocks the plain merge; merging anyway asks first and sends accept_gap", async () => {
  const failed = preview({ diagnostic: diagnostic({ outcome: "failed", gap_paise: 200000,
    computed: { credits: 50000, debits: 65000, closing: 85000 }, declared: { opening: 100000, closing: 285000 } }) });
  const { api, user } = setup("/statements?open=s-1", {
    "GET /api/statements/s-1": { status: 200, body: failed },
    "POST /api/statements/s-1/merge": { status: 202, body: { job_id: "j-1" } },
    "GET /api/jobs/j-1": job("succeeded", { result: { new: 2, matched: 1, needs_review: 1, unparseable: 0 } }),
  });
  expect(await screen.findByText(/₹2,000.00 less than the statement's own closing balance/)).toBeInTheDocument();
  expect(screen.getByText("Gap")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /^Merge 3 rows/ })).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Merge anyway" }));
  expect(screen.getByText("The ₹2,000.00 gap is kept on record. Merge anyway?")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Yes, merge anyway" }));
  await waitFor(() => expect(api.bodiesFor("POST /api/statements/s-1/merge")).toEqual([{ accept_gap: true }]));
  expect(await screen.findByText("2 added, 1 updated, 1 needs review.")).toBeInTheDocument();
});

test("the details toggle shows the raw cells of rows that couldn't be read, and the full report", async () => {
  const bad = { page: 3, raw_cells: ["14/08/26", "NEFT CR", "", "2,000.00"], reason: "row has no date" };
  const { user } = setup("/statements?open=s-1", { "GET /api/statements/s-1": { status: 200, body: preview({
    unparseable: [bad], diagnostic: diagnostic({ rows_unparseable: 1, unparseable_rows: [bad] }) }) } });
  expect(await screen.findByRole("heading", { name: "1 row couldn't be read" })).toBeInTheDocument();
  await user.click(screen.getByText("Show the full parser report"));
  const report = screen.getByRole("group", { name: "Show the full parser report" });
  expect(within(report).getAllByText("NEFT CR").length).toBeGreaterThan(0);
  expect(within(report).getByText(/"rows_unparseable": 1/)).toBeInTheDocument();
});

test("an unbound statement asks which account, offering only accounts of its kind", async () => {
  const unbound = preview({ account_id: null, account_name: null, new: [line(0, "UPI-TEA", 4000)], matches: [] });
  const { api, user } = setup("/statements?open=s-1", {
    "GET /api/statements/s-1": { status: 200, body: unbound },
    "PATCH /api/statements/s-1": { status: 200, body: preview({ account_id: "a-2", account_name: "Other Bank" }) },
  });
  expect(await screen.findByRole("heading", { name: "Which account is this statement for?" })).toBeInTheDocument();
  const other = await screen.findByRole("radio", { name: "Other Bank" });
  expect(screen.queryByRole("radio", { name: "Millenia" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /^Merge/ })).not.toBeInTheDocument();
  await user.click(other);
  expect(api.bodiesFor("PATCH /api/statements/s-1")).toEqual([]); // choosing isn't committing (arrow keys move it)
  await user.click(screen.getByRole("button", { name: "Use this account" }));
  await waitFor(() => expect(api.bodiesFor("PATCH /api/statements/s-1")).toEqual([{ account_id: "a-2" }]));
  expect(await screen.findByRole("heading", { name: "Other Bank" })).toBeInTheDocument();
});

test("a bound account can be changed before merging", async () => {
  const { api, user } = setup("/statements?open=s-1", {
    "GET /api/statements/s-1": { status: 200, body: preview() },
    "PATCH /api/statements/s-1": { status: 200, body: preview({ account_id: "a-2", account_name: "Other Bank" }) },
  });
  await user.click(await screen.findByRole("button", { name: "Change account" }));
  expect(await screen.findByRole("radio", { name: "HDFC Savings" })).toBeChecked();
  await user.click(screen.getByRole("button", { name: "Cancel" }));
  expect(screen.getByRole("heading", { name: "HDFC Savings" })).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Change account" }));
  await user.click(await screen.findByRole("radio", { name: "Other Bank" }));
  await user.click(screen.getByRole("button", { name: "Use this account" }));
  expect(await screen.findByRole("heading", { name: "Other Bank" })).toBeInTheDocument();
  expect(api.bodiesFor("PATCH /api/statements/s-1")).toEqual([{ account_id: "a-2" }]);
});

test("merging polls the job and reports what changed", async () => {
  const { api, user } = setup("/statements?open=s-1", {
    "GET /api/statements/s-1": { status: 200, body: preview() },
    "POST /api/statements/s-1/merge": { status: 202, body: { job_id: "j-1" } },
    "GET /api/jobs/j-1": job("running"),
  });
  await user.click(await screen.findByRole("button", { name: "Merge 3 rows into HDFC Savings" }));
  expect(await screen.findByText("Merging…")).toBeInTheDocument();
  api.set("GET /api/jobs/j-1", job("succeeded", { result: { new: 2, matched: 1, needs_review: 0, unparseable: 0 } }));
  expect(await screen.findByText("2 added, 1 updated, 0 need review.", {}, { timeout: 3000 })).toBeInTheDocument();
  expect(api.bodiesFor("POST /api/statements/s-1/merge")).toEqual([{ accept_gap: false }]);
});

test("a merge that fails shows the job's sentence", async () => {
  const { user } = setup("/statements?open=s-1", {
    "GET /api/statements/s-1": { status: 200, body: preview() },
    "POST /api/statements/s-1/merge": { status: 202, body: { job_id: "j-1" } },
    "GET /api/jobs/j-1": job("failed", { error: "Pick an active account for this statement first." }),
  });
  await user.click(await screen.findByRole("button", { name: "Merge 3 rows into HDFC Savings" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Pick an active account");
});

test("discarding deletes the preview and returns to the upload form", async () => {
  const { api, user } = setup("/statements?open=s-1", {
    "GET /api/statements/s-1": { status: 200, body: preview() },
    "DELETE /api/statements/s-1": { status: 204 },
  });
  await user.click(await screen.findByRole("button", { name: "Discard" }));
  expect(await screen.findByLabelText("Statement PDF")).toBeInTheDocument();
  expect(api.calls).toContain("DELETE /api/statements/s-1");
});

test("an expired preview says so", async () => {
  setup("/statements?open=s-1", { "GET /api/statements/s-1": { status: 404, body: {
    detail: "This preview has expired or was discarded." } } });
  expect(await screen.findByText(/expired or was discarded/)).toBeInTheDocument();
});

test("open previews are listed above the form", async () => {
  setup("/statements", { "GET /api/statements": { status: 200, body: [{ id: "s-1", filename: "sept.pdf",
    account_name: "HDFC Savings", outcome: "passed", created_at: "2026-10-19T04:30:00.000Z",
    expires_at: "2026-10-20T04:30:00.000Z" }] } });
  expect(await screen.findByRole("link", { name: /HDFC Savings, sept\.pdf/ })).toHaveAttribute("href", "/statements?open=s-1");
});

test("the merge action sits at the top, before the check and the rows", async () => {
  setup("/statements?open=s-1", { "GET /api/statements/s-1": { status: 200, body: preview() } });
  const merge = await screen.findByRole("button", { name: "Merge 3 rows into HDFC Savings" });
  const check = screen.getByRole("region", { name: "The statement's own check" });
  expect(merge.compareDocumentPosition(check) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
});

test("a statement read by Claude says so, and that every amount was found in the PDF", async () => {
  setup("/statements?open=s-1", { "GET /api/statements/s-1": { status: 200, body: preview({
    template_name: null, diagnostic: diagnostic({ tier: 3, model: "claude-sonnet-5-5", institution: "Other Bank",
      template_name: null }) }) } });
  expect(await screen.findByText(/sept\.pdf, read by Claude because no known layout matched, 2 pages\./)).toBeInTheDocument();
  expect(screen.getByText("Every amount below was found printed in the PDF. Rows Claude read but Hisaab couldn't " +
    "find there are under “couldn't be read”.")).toBeInTheDocument();
});

test("when a known layout didn't fit, the preview says which and why", async () => {
  setup("/statements?open=s-1", { "GET /api/statements/s-1": { status: 200, body: preview({
    template_name: null, diagnostic: diagnostic({ tier: 3, template_name: null,
      template_tried: { template: "hdfc_cc_statement", reason: "no header row on any page" } }) }) } });
  expect(await screen.findByText(/read by Claude because the “hdfc_cc_statement” layout didn't fit \(no header row on any page\)/))
    .toBeInTheDocument();
});

test("a statement read by a template shows no Claude note", async () => {
  setup("/statements?open=s-1", { "GET /api/statements/s-1": { status: 200, body: preview() } });
  expect(await screen.findByText(/read as “Example statement”/)).toBeInTheDocument();
  expect(screen.queryByText(/found printed in the PDF/)).not.toBeInTheDocument();
});

test("while reading, the form says an unfamiliar layout can take a minute or two", async () => {
  const { user } = setup("/statements", {});
  let release: () => void = () => {};
  vi.stubGlobal("fetch", (input: Request) => new Promise<Response>((resolve) => {
    if (input.method === "POST") release = () => resolve(new Response(JSON.stringify(preview()), {
      status: 201, headers: { "content-type": "application/json" } }));
    else resolve(new Response(JSON.stringify(input.url.endsWith("/api/auth/me") ? ME.body : []), {
      status: 200, headers: { "content-type": "application/json" } }));
  }));
  await user.upload(await screen.findByLabelText("Statement PDF"), new File(["%PDF"], "sept.pdf"));
  await user.click(screen.getByRole("button", { name: "Read statement" }));
  expect(await screen.findByText("A layout Hisaab doesn't know yet is read by Claude, which can take a minute or two."))
    .toBeInTheDocument();
  release();
});
