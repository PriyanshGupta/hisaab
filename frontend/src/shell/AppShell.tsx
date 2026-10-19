import { NavLink, Outlet, useNavigate } from "react-router";

import { useLogout, useMe } from "../api/session";
import "../styles/shell.css";
import { NAV_ITEMS } from "./nav";

/**
 * The authenticated frame. Desktop: a red binding strip at the left edge, a
 * sidebar (wordmark, destinations, who is signed in), then the page. Phone: the
 * binding runs along the top, a slim bar carries the wordmark and sign-out, and
 * destinations sit in a bottom tab bar within thumb reach. Both layouts render
 * from the same markup; CSS decides which parts show.
 */
export function AppShell() {
  const me = useMe();
  const logout = useLogout();
  const navigate = useNavigate();
  const user = me.data;
  if (!user) return null; // RequireAuth guarantees a user; this narrows the type.

  const signOut = () => logout.mutate(undefined, { onSettled: () => navigate("/login", { replace: true }) });

  return (
    <div className="shell">
      <a className="skip-link" href="#main">
        Skip to content
      </a>

      <header className="shell__bar">
        <span className="wordmark">Hisaab</span>
        <div className="shell__bar-user">
          <span className="shell__user-name">{user.display_name}</span>
          <button className="button button--quiet" type="button" onClick={signOut}>
            Sign out
          </button>
        </div>
      </header>

      <aside className="shell__sidebar">
        <span className="wordmark">Hisaab</span>
        <nav aria-label="Main">
          <ul className="sidenav">
            {NAV_ITEMS.map((item) => (
              <li key={item.to}>
                <NavLink className="sidenav__link" to={item.to} end={item.end ?? false}>
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

      <main id="main" className="shell__main" tabIndex={-1}>
        <Outlet />
      </main>

      <nav className="tabbar" aria-label="Main">
        {NAV_ITEMS.filter((item) => item.tab !== false).map((item) => (
          <NavLink key={item.to} className="tabbar__link" to={item.to} end={item.end ?? false}>
            {item.label}
          </NavLink>
        ))}
      </nav>
    </div>
  );
}
