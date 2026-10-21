// The app's top-level destinations, in the order they appear in the sidebar (on phones, the drawer), in three
// groups: reading your money, putting money in, and the set-up behind it. admin: true shows only to admins.
import type { IconName } from "../ui/Icon";

export type NavItem = { to: string; label: string; icon: IconName; group: 1 | 2 | 3; end?: boolean; admin?: boolean };

export const NAV_ITEMS: readonly NavItem[] = [
  { to: "/", label: "Report", icon: "report", group: 1, end: true },
  { to: "/review", label: "Review", icon: "review", group: 1 },
  { to: "/transactions", label: "Transactions", icon: "transactions", group: 1 },
  { to: "/add", label: "Add", icon: "add", group: 2 },
  { to: "/statements", label: "Statements", icon: "statements", group: 2 },
  { to: "/accounts", label: "Accounts", icon: "accounts", group: 3 },
  { to: "/categories", label: "Categories", icon: "categories", group: 3 },
  { to: "/settings", label: "Settings", icon: "settings", group: 3 },
  { to: "/admin", label: "Admin", icon: "admin", group: 3, admin: true },
];
