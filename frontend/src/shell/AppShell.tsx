import { useEffect, useRef, useState } from "react";
import { NavLink, Outlet, useNavigate } from "react-router";

import { useLogout, useMe } from "../api/session";
import "../styles/shell.css";
import { NAV_ITEMS } from "./nav";

/**
 * The authenticated frame. Desktop: a sidebar (wordmark, destinations, who is
 * signed in) with the red double margin rule down its right edge, then the page.
 * Phone: the rule runs along the top under a slim bar with a menu button, and
 * the same sidebar slides in from the left as a drawer. One markup; CSS decides
 * which parts show.
 */
export function AppShell() {
  const me = useMe();
  const logout = useLogout();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const menuButton = useRef<HTMLButtonElement>(null);
  const drawer = useRef<HTMLElement>(null);

  useEffect(() => {
    if (!open) return;
    drawer.current?.querySelector<HTMLElement>("a, button")?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setOpen(false);
      menuButton.current?.focus();
    };
    // grown to desktop width while open: the sidebar is always there, so nothing may stay inert
    const desktop = window.matchMedia?.("(min-width: 960px)");
    const onWide = () => setOpen(false);
    document.addEventListener("keydown", onKey);
    desktop?.addEventListener("change", onWide);
    return () => {
      document.removeEventListener("keydown", onKey);
      desktop?.removeEventListener("change", onWide);
    };
  }, [open]);

  const user = me.data;
  if (!user) return null; // RequireAuth guarantees a user; this narrows the type.

  const close = () => {
    setOpen(false);
    menuButton.current?.focus();
  };
  const items = NAV_ITEMS.filter((item) => !item.admin || user.role === "admin");
  const signOut = () => logout.mutate(undefined, { onSettled: () => navigate("/login", { replace: true }) });

  return (
    <div className={open ? "shell shell--drawer-open" : "shell"}>
      <a className="skip-link" href="#main">
        Skip to content
      </a>

      <header className="shell__bar" inert={open}>
        <button ref={menuButton} className="shell__menu" type="button" aria-label="Menu" aria-expanded={open}
          aria-controls="shell-sidebar" onClick={() => setOpen(true)}>
          <svg viewBox="0 0 20 20" width="20" height="20" aria-hidden="true">
            <path d="M2 5h16M2 10h16M2 15h16" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" />
          </svg>
        </button>
        <span className="wordmark">Hisaab</span>
      </header>

      <aside ref={drawer} id="shell-sidebar" className="shell__sidebar">
        <span className="wordmark">Hisaab</span>
        <nav aria-label="Main">
          <ul className="sidenav">
            {items.map((item) => (
              <li key={item.to}>
                <NavLink className="sidenav__link" to={item.to} end={item.end ?? false} onClick={() => setOpen(false)}>
                  {item.label}
                </NavLink>
              </li>
            ))}
          </ul>
        </nav>
        <div className="shell__sidebar-user">
          <span className="shell__user-name">{user.display_name}</span>
          <button className="button button--quiet" type="button" onClick={signOut}>
            Sign out
          </button>
        </div>
      </aside>
      {open && <button className="shell__scrim" type="button" aria-label="Close menu" onClick={close} />}

      <main id="main" className="shell__main" tabIndex={-1} inert={open}>
        <Outlet />
      </main>
    </div>
  );
}
