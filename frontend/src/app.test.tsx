import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { meQueryKey } from "./api/session";
import { mockApi } from "./test/mockApi";
import { renderApp } from "./test/renderApp";

const ASHA = { id: "u-1", username: "asha", display_name: "Asha Kulkarni", role: "admin" };
const NOT_LOGGED_IN = { status: 401, body: { detail: "Not logged in" } };
const setupAt = (step: string) => ({ status: 200, body: { step, backfill_start: "2026-07-11", job: null } });
const SETUP_DONE = setupAt("done");

async function signIn(username = "asha", password = "correct horse") {
  const user = userEvent.setup();
  await user.type(await screen.findByLabelText("Username"), username);
  await user.type(screen.getByLabelText("Password"), password);
  await user.click(screen.getByRole("button", { name: "Sign in" }));
}

test("an anonymous visitor to / is sent to the sign-in form", async () => {
  mockApi({ "GET /api/setup": SETUP_DONE, "GET /api/auth/me": NOT_LOGGED_IN });
  const { router } = renderApp("/");
  expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
  expect(router.state.location.pathname).toBe("/login");
});

test("signing in lands on the report with the user's name in the chrome", async () => {
  const api = mockApi({ "GET /api/setup": SETUP_DONE,
    "GET /api/auth/me": NOT_LOGGED_IN,
    "POST /api/auth/login": { status: 200, body: ASHA },
  });
  const { router } = renderApp("/");
  await signIn();
  expect(await screen.findByRole("heading", { name: "Report" })).toBeInTheDocument();
  expect(screen.getAllByText("Asha Kulkarni").length).toBeGreaterThan(0);
  expect(router.state.location.pathname).toBe("/");
  expect(api.calls).toContain("POST /api/auth/login");
});

test("a wrong password says so and stays on the sign-in form", async () => {
  mockApi({ "GET /api/setup": SETUP_DONE,
    "GET /api/auth/me": NOT_LOGGED_IN,
    "POST /api/auth/login": { status: 401, body: { detail: "Invalid username or password" } },
  });
  renderApp("/login");
  await signIn("asha", "nope nope");
  expect(await screen.findByRole("alert")).toHaveTextContent("Wrong username or password.");
  expect(screen.getByRole("heading", { name: "Sign in" })).toBeInTheDocument();
});

test("a busy server asks the user to try again shortly", async () => {
  mockApi({ "GET /api/setup": SETUP_DONE,
    "GET /api/auth/me": NOT_LOGGED_IN,
    "POST /api/auth/login": { status: 503, body: { detail: "Busy" } },
  });
  renderApp("/login");
  await signIn();
  expect(await screen.findByRole("alert")).toHaveTextContent("busy");
});

test("a locked-out username shows the server's explanation", async () => {
  mockApi({ "GET /api/setup": SETUP_DONE,
    "GET /api/auth/me": NOT_LOGGED_IN,
    "POST /api/auth/login": {
      status: 429,
      body: { detail: "Too many wrong passwords for this username. Try again in 15 minutes." },
    },
  });
  renderApp("/login");
  await signIn();
  expect(await screen.findByRole("alert")).toHaveTextContent("Try again in 15 minutes.");
});

test("an unreachable server is reported as such", async () => {
  mockApi({ "GET /api/setup": SETUP_DONE, "GET /api/auth/me": NOT_LOGGED_IN, "POST /api/auth/login": "network-error" });
  renderApp("/login");
  await signIn();
  expect(await screen.findByRole("alert")).toHaveTextContent("Can't reach the Hisaab server");
});

test("signing out ends the session and returns to sign-in", async () => {
  const api = mockApi({ "GET /api/setup": SETUP_DONE,
    "GET /api/auth/me": { status: 200, body: ASHA },
    "POST /api/auth/logout": { status: 204 },
  });
  const { router } = renderApp("/");
  const user = userEvent.setup();
  await user.click((await screen.findAllByRole("button", { name: "Sign out" }))[0]!);
  expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
  expect(api.calls).toContain("POST /api/auth/logout");
  expect(router.state.location.pathname).toBe("/login");
});

test("a session that expires while the app is open lands on sign-in", async () => {
  const api = mockApi({ "GET /api/setup": SETUP_DONE, "GET /api/auth/me": { status: 200, body: ASHA } });
  const { queryClient } = renderApp("/");
  expect(await screen.findByRole("heading", { name: "Report" })).toBeInTheDocument();
  api.set("GET /api/auth/me", NOT_LOGGED_IN);
  await queryClient.invalidateQueries({ queryKey: meQueryKey });
  expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
});

test("an unknown page shows not-found inside the shell", async () => {
  mockApi({ "GET /api/setup": SETUP_DONE, "GET /api/auth/me": { status: 200, body: ASHA } });
  renderApp("/no/such/page");
  expect(await screen.findByRole("heading", { name: "Page not found" })).toBeInTheDocument();
  expect(screen.getAllByText("Asha Kulkarni").length).toBeGreaterThan(0);
});

test("visiting sign-in while signed in goes to the report", async () => {
  mockApi({ "GET /api/setup": SETUP_DONE, "GET /api/auth/me": { status: 200, body: ASHA } });
  const { router } = renderApp("/login");
  expect(await screen.findByRole("heading", { name: "Report" })).toBeInTheDocument();
  expect(router.state.location.pathname).toBe("/");
});

test("after sign-in the user returns to the page they first asked for", async () => {
  mockApi({ "GET /api/setup": SETUP_DONE,
    "GET /api/auth/me": NOT_LOGGED_IN,
    "POST /api/auth/login": { status: 200, body: ASHA },
  });
  const { router } = renderApp("/somewhere/deep");
  await signIn();
  expect(await screen.findByRole("heading", { name: "Page not found" })).toBeInTheDocument();
  expect(router.state.location.pathname).toBe("/somewhere/deep");
});

test("home during first run goes to setup, so a closed tab comes back to the backfill", async () => {
  mockApi({ "GET /api/setup": setupAt("backfill"), "GET /api/auth/me": { status: 200, body: ASHA } });
  const { router } = renderApp("/");
  expect(await screen.findByRole("heading", { name: "Set up Hisaab" })).toBeInTheDocument();
  expect(router.state.location.pathname).toBe("/setup");
});

test("home after the backfill, with nothing categorized yet, is bulk categorization", async () => {
  mockApi({
    "GET /api/setup": setupAt("categorize"), "GET /api/auth/me": { status: 200, body: ASHA },
    "GET /api/setup/uncategorized": { status: 200, body: { groups: [], unnamed: 0 } },
    "GET /api/categories": { status: 200, body: [] },
  });
  const { router } = renderApp("/");
  expect(await screen.findByRole("heading", { name: "Categorize your spending" })).toBeInTheDocument();
  expect(router.state.location.pathname).toBe("/categorize");
});
