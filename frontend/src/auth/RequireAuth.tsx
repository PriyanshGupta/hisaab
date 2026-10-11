import type { ReactNode } from "react";
import { Navigate, useLocation } from "react-router";

import { useMe } from "../api/session";

/** Renders children for a signed-in user; otherwise goes to sign-in, remembering where they were headed. */
export function RequireAuth({ children }: { children: ReactNode }) {
  const me = useMe();
  const location = useLocation();

  if (me.isPending) return <div className="boot" aria-busy="true" aria-label="Loading" />;
  if (me.isError) {
    return (
      <main className="boot boot--error">
        <p role="alert">Can't reach the Hisaab server. Check that it's running, then reload.</p>
      </main>
    );
  }
  if (me.data === null) return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  return children;
}
