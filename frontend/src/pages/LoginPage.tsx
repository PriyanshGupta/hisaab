import { type FormEvent, type ReactNode, useState } from "react";
import { Link, Navigate, useLocation, useNavigate } from "react-router";

import { loginErrorMessage, useLogin, useMe, useSignupInfo } from "../api/session";
import "../styles/login.css";

/** The frame both doors share: the ledger's cloth cover, then the paper the form sits on. */
export function AuthLayout({ children }: { children: ReactNode }) {
  return (
    <div className="login">
      {/* The ledger's cloth cover: the one bold surface in the app. */}
      <div className="login__cover" aria-hidden="true">
        <span className="login__wordmark">Hisaab</span>
        <span className="login__tagline">The household ledger</span>
      </div>
      <main className="login__page">{children}</main>
    </div>
  );
}

export function LoginPage() {
  const me = useMe();
  const info = useSignupInfo();
  const login = useLogin();
  const navigate = useNavigate();
  const location = useLocation();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");

  const from = (location.state as { from?: string } | null)?.from ?? "/";
  if (me.data) return <Navigate to={from} replace />;
  // Nobody has an account yet, so nobody can sign in: the first person creates the admin account.
  if (info.data?.first_user) return <Navigate to="/signup" replace />;

  const submit = (event: FormEvent) => {
    event.preventDefault();
    login.mutate({ username, password }, { onSuccess: () => navigate(from, { replace: true }) });
  };

  return (
    <AuthLayout>
      <form className="login__form sheet" onSubmit={submit} noValidate>
        <div className="login__head">
          <h1 className="login__title">Sign in</h1>
          <p className="login__sub">to your household ledger</p>
        </div>

        <label className="field">
          <span className="field__label">Username</span>
          <input
            className="field__input"
            name="username"
            autoComplete="username"
            autoCapitalize="none"
            spellCheck={false}
            required
            value={username}
            onChange={(e) => setUsername(e.target.value)}
          />
        </label>

        <label className="field">
          <span className="field__label">Password</span>
          <input
            className="field__input"
            name="password"
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>

        {login.isError && (
          <p className="form-error" role="alert">
            {loginErrorMessage(login.error)}
          </p>
        )}

        <button className="button button--primary" type="submit" disabled={login.isPending}>
          {login.isPending ? "Signing in…" : "Sign in"}
        </button>

        <p className="login__switch">
          New to Hisaab? <Link to="/signup">Create an account</Link>
        </p>
      </form>
    </AuthLayout>
  );
}
