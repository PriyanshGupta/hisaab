import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { mockApi } from "../test/mockApi";
import { renderApp } from "../test/renderApp";

const ME = { status: 200, body: { id: "u-1", username: "asha", display_name: "Asha Kulkarni", role: "member" } };

function setup() {
  mockApi({
    "GET /api/auth/me": ME, "GET /api/accounts": { status: 200, body: [] },
    "GET /api/transactions": { status: 200, body: [] },
  });
  const { router } = renderApp("/transactions");
  return { router, user: userEvent.setup() };
}

// Phones get the sidebar as a drawer from the left (user, 2026-10-21): too many destinations for a bottom bar.
test("one navigation, opened from the menu button and closed by choosing a page", async () => {
  const { router, user } = setup();
  const menu = await screen.findByRole("button", { name: "Menu" });
  expect(screen.getAllByRole("navigation", { name: "Main" })).toHaveLength(1); // no second, bottom bar
  expect(menu).toHaveAttribute("aria-expanded", "false");
  await user.click(menu);
  expect(menu).toHaveAttribute("aria-expanded", "true");
  const nav = screen.getByRole("navigation", { name: "Main" });
  expect(within(nav).getByRole("link", { name: "Statements" })).toBeInTheDocument(); // the drawer has room for all
  await user.click(within(nav).getByRole("link", { name: "Accounts" }));
  expect(router.state.location.pathname).toBe("/accounts");
  expect(menu).toHaveAttribute("aria-expanded", "false");
});

test("Escape or a tap beside the drawer closes it, and focus goes back to the menu button", async () => {
  const { user } = setup();
  const menu = await screen.findByRole("button", { name: "Menu" });
  await user.click(menu);
  await user.keyboard("{Escape}");
  expect(menu).toHaveAttribute("aria-expanded", "false");
  expect(menu).toHaveFocus();
  await user.click(menu);
  await user.click(screen.getByRole("button", { name: "Close menu" }));
  expect(menu).toHaveAttribute("aria-expanded", "false");
});
