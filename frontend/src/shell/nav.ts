// The app's top-level destinations, in the order they appear in the sidebar
// (desktop) and the bottom tab bar (phone). Later slices add Review, Add,
// Accounts and the admin screens here; only routes that exist are listed.
export type NavItem = { to: string; label: string; end?: boolean };

export const NAV_ITEMS: readonly NavItem[] = [{ to: "/", label: "Report", end: true }];
