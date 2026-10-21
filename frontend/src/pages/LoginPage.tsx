import { type FormEvent, useState } from "react";
import { Navigate, useLocation, useNavigate } from "react-router";

import { loginErrorMessage, useLogin, useMe } from "../api/session";
import "../styles/login.css";

export function LoginPage() {
  const me = useMe();
  const login = useLogin();
  const navigate = useNavigate();
  const location = useLocation();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");

  const from = (location.state as { from?: string } | null)?.from ?? "/";
  if (me.data) return <Navigate to={from} replace />;

  const submit = (event: FormEvent) => {
    event.preventDefault();
    login.mutate({ username, password }, { onSuccess: () => navigate(from, { replace: true }) });
  };

  return (
    <div className="login">
      {/* The ledger's cloth cover: the one bold surface in the app. */}
      <div className="login__cover" aria-hidden="true">
        <span className="login__wordmark">Hisaab</span>
        <span className="login__tagline">The household ledger</span>
      </div>

      <main className="login__page">
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

          <p className="login__help">Accounts are created by your Hisaab admin.</p>
        </form>
      </main>
    </div>
  );
}
