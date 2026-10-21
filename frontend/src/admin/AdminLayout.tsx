// The Admin area (slice 24): one sidebar entry, five sections, and the parser-change banner above every one.
import { NavLink, Outlet } from "react-router";

import { useMe } from "../api/session";

import "../styles/admin.css";
import { ResyncBanner } from "./ResyncBanner";

const SECTIONS: readonly { to: string; label: string }[] = [
  { to: "/admin/templates", label: "Templates" },
  { to: "/admin/senders", label: "Senders" },
  { to: "/admin/payee-names", label: "Payee names" },
  { to: "/admin/users", label: "Users" },
  { to: "/admin/resync", label: "Re-sync" },
];

export function AdminLayout() {
  const me = useMe();
  if (!me.data) return null;
  if (me.data.role !== "admin") {
    return <div className="empty"><p className="empty__lead">This area is for admins.</p>
      <p className="empty__body">An admin of this Hisaab can do what these screens do.</p></div>;
  }
  return (
    <div className="admin">
      <nav className="admin__sections" aria-label="Admin sections">
        {SECTIONS.map((s) => <NavLink key={s.to} className="admin__section" to={s.to}>{s.label}</NavLink>)}
      </nav>
      <ResyncBanner />
      <Outlet />
    </div>
  );
}
