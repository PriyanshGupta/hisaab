import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { mockApi, type Reply } from "../test/mockApi";
import { renderApp } from "../test/renderApp";

const ME = { status: 200, body: { id: "u-1", username: "asha", display_name: "Asha", role: "member" } };
const CATEGORIES = { status: 200, body: [
  { id: "c-food", name: "Food", archived_at: null },
  { id: "c-old", name: "Old", archived_at: "2026-10-01T00:00:00.000Z" },
] };
const GROUPS = { status: 200, body: {
  groups: [{ payee_id: "p-1", payee_name: "Corner Shop", count: 3 }, { payee_id: "p-2", payee_name: "Tea Stall", count: 1 }],
  unnamed: 2 } };

function setup(routes: Record<string, Reply> = {}) {
  const api = mockApi({ "GET /api/auth/me": ME, "GET /api/categories": CATEGORIES,
    "GET /api/setup/uncategorized": GROUPS, ...routes });
  renderApp("/categorize");
  return { api, user: userEvent.setup() };
}

test("each recognised payee is one group, filed with one action as one rule", async () => {
  const { api, user } = setup({
    "PUT /api/payees/p-1/category": { status: 200, body: { id: "p-1", name: "Corner Shop", type: "merchant",
      category_id: "c-food" } },
  });
  expect(await screen.findByText("Corner Shop")).toBeInTheDocument();
  expect(screen.getByText("3 transactions")).toBeInTheDocument();
  const picker = screen.getByLabelText("Category for Corner Shop");
  expect(screen.queryByRole("option", { name: "Old" })).not.toBeInTheDocument(); // archived
  await user.selectOptions(picker, "c-food");
  api.set("GET /api/setup/uncategorized", { status: 200, body: {
    groups: [{ payee_id: "p-2", payee_name: "Tea Stall", count: 1 }], unnamed: 2 } });
  await user.click(screen.getByRole("button", { name: "Categorize all 3" }));
  expect(api.bodiesFor("PUT /api/payees/p-1/category")).toEqual([{ category_id: "c-food" }]);
  expect(await screen.findByText("3 transactions filed under Food.")).toBeInTheDocument();
  expect(api.calls.filter((c) => c === "PUT /api/payees/p-1/category")).toHaveLength(1);
});

test("the button waits for a category to be chosen", async () => {
  setup();
  expect(await screen.findByRole("button", { name: "Categorize all 3" })).toBeDisabled();
});

test("payees Hisaab doesn't recognise yet are counted, not hidden", async () => {
  setup();
  expect(await screen.findByText(/2 more have a payee Hisaab doesn't recognise yet/)).toBeInTheDocument();
});

test("with nothing left the screen says so and points to the report", async () => {
  setup({ "GET /api/setup/uncategorized": { status: 200, body: { groups: [], unnamed: 0 } } });
  expect(await screen.findByText(/Everything recognised is categorized/)).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Go to the report" })).toHaveAttribute("href", "/");
});
