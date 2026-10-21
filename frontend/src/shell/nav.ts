// The app's top-level destinations, in the order they appear in the sidebar (on phones, the drawer).
// admin: true shows only to admins (the Admin area).
export type NavItem = { to: string; label: string; end?: boolean; admin?: boolean };

export const NAV_ITEMS: readonly NavItem[] = [
  { to: "/", label: "Report", end: true },
  { to: "/review", label: "Review" },
  { to: "/transactions", label: "Transactions" },
  { to: "/statements", label: "Statements" },
  { to: "/add", label: "Add" },
  { to: "/accounts", label: "Accounts" },
  { to: "/categories", label: "Categories" },
  { to: "/settings", label: "Settings" },
  { to: "/admin", label: "Admin", admin: true },
];
