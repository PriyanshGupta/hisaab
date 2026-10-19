// The app's top-level destinations, in the order they appear in the sidebar
// (desktop) and the bottom tab bar (phone). Later slices add the admin screens
// here; only routes that exist are listed.
// tab: false keeps an occasional destination out of the phone's tab bar, which fits about seven labels;
// the page is linked from a related screen instead (Statements from Transactions).
export type NavItem = { to: string; label: string; end?: boolean; tab?: boolean };

export const NAV_ITEMS: readonly NavItem[] = [
  { to: "/", label: "Report", end: true },
  { to: "/review", label: "Review" },
  { to: "/transactions", label: "Transactions" },
  { to: "/statements", label: "Statements", tab: false },
  { to: "/add", label: "Add" },
  { to: "/accounts", label: "Accounts" },
  { to: "/categories", label: "Categories" },
  { to: "/settings", label: "Settings" },
];
