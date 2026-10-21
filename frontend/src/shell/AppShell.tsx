import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Link, NavLink, Outlet, useNavigate } from "react-router";

import { api, unwrap } from "../api/client";
import { reviewKey } from "../api/review";
import { useLogout, useMe } from "../api/session";
import { countLeft } from "../review/lanes";
import "../styles/shell.css";
import { Icon } from "../ui/Icon";
import { NAV_ITEMS, type NavItem } from "./nav";

/** What's waiting in Review, for the count beside it: the same number the Review page shows as left. */
function useReviewLeft() {
  return useQuery({
    queryKey: reviewKey,
    queryFn: () => unwrap(() => api.GET("/api/review")),
    select: (data) => countLeft(data.items),
    retry: false, // a count beside a link: never worth retrying for
  });
}

/**
 * The authenticated frame. Desktop: the sidebar is the ledger's green cloth spine (wordmark, destinations in three
 * groups, who is signed in), and the page beside it is the paper, its red double margin rule running down the left.
 * Phone: the spine is a slim bar at the top (menu button, wordmark, what's waiting in Review), and the same sidebar
 * slides in from the left as a drawer. One markup; CSS decides which parts show.
 */
export function AppShell() {
  const me = useMe();
  const logout = useLogout();
  const navigate = useNavigate();
  const left = useReviewLeft().data ?? 0;
  const [open, setOpen] = useState(false);
  const menuButton = useRef<HTMLButtonElement>(null);
  const drawer = useRef<HTMLElement>(null);

  useEffect(() => {
    if (!open) return;
    drawer.current?.querySelector<HTMLElement>("nav a")?.focus();
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
  const groups = [1, 2, 3].map((g) => items.filter((item) => item.group === g)).filter((g) => g.length > 0);
  const signOut = () => logout.mutate(undefined, { onSettled: () => navigate("/login", { replace: true }) });
  const waiting = `${left} waiting`;

  return (
    <div className={open ? "shell shell--drawer-open" : "shell"}>
      <a className="skip-link" href="#main">
        Skip to content
      </a>

      <header className="shell__bar" inert={open}>
        <button ref={menuButton} className="shell__menu" type="button" aria-label="Menu" aria-expanded={open}
          aria-controls="shell-sidebar" onClick={() => setOpen(true)}>
          <Icon name="menu" size={22} />
        </button>
        <Link className="wordmark shell__bar-mark" to="/">Hisaab</Link>
        {left > 0 && (
          <Link className="shell__bar-review" to="/review" aria-label={`Review: ${waiting}`}>
            <Icon name="review" size={18} />
            <span>{left}</span>
          </Link>
        )}
      </header>

      <aside ref={drawer} id="shell-sidebar" className="shell__sidebar">
        <div className="shell__brand">
          <span className="wordmark">Hisaab</span>
          <button className="shell__close" type="button" aria-label="Close navigation" onClick={close}>
            <Icon name="close" size={20} />
          </button>
        </div>
        <nav aria-label="Main" className="shell__nav">
          {groups.map((group) => (
            <ul key={group[0]!.group} className="sidenav">
              {group.map((item) => (
                <NavItemLink key={item.to} item={item} count={item.to === "/review" ? left : 0}
                  onPick={() => setOpen(false)} />
              ))}
            </ul>
          ))}
        </nav>
        <div className="shell__user">
          <span className="shell__avatar" aria-hidden="true">{user.display_name.trim().charAt(0).toUpperCase()}</span>
          <span className="shell__who">
            <span className="shell__user-name">{user.display_name}</span>
            <span className="shell__user-role">{user.role === "admin" ? "Admin" : "Member"}</span>
          </span>
          <button className="shell__signout" type="button" onClick={signOut}>
            <Icon name="signout" size={16} />
            Sign out
          </button>
        </div>
      </aside>
      {open && <button className="shell__scrim" type="button" aria-label="Close menu" onClick={close} />}

      <main id="main" className="shell__main" tabIndex={-1} inert={open}>
        <div className="shell__page">
          <Outlet />
        </div>
      </main>
    </div>
  );
}

function NavItemLink({ item, count, onPick }: { item: NavItem; count: number; onPick: () => void }) {
  const countId = `nav-count-${item.icon}`;
  return (
    <li>
      <NavLink className="sidenav__link" to={item.to} end={item.end ?? false} onClick={onPick}
        aria-describedby={count > 0 ? countId : undefined}>
        <Icon name={item.icon} size={18} />
        <span className="sidenav__label">{item.label}</span>
        {count > 0 && <span className="sidenav__count" aria-hidden="true">{count}</span>}
      </NavLink>
      {/* the count describes the link without renaming it: the link is still just "Review" */}
      {count > 0 && <span id={countId} hidden>{count} waiting</span>}
    </li>
  );
}
