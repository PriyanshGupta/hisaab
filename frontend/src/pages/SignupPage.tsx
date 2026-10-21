// Sign-up (user ruling 2026-10-21): the first person to sign up becomes the admin, everyone after a member. A new
// account is signed in at once and goes home, which is first-run setup until that is through (HomePage), so the
// page ends by saying what setup will ask for.
import { type FormEvent, useState } from "react";
import { Link, Navigate, useNavigate } from "react-router";

import { describeError } from "../api/client";
import { useMe, useSignup, useSignupInfo } from "../api/session";
import { AuthLayout } from "./LoginPage";

const NEXT: { title: string; detail: string }[] = [
  { title: "Add your accounts", detail: "Each bank account, card and cash wallet you want to track." },
  { title: "Connect Gmail", detail: "Read-only: Hisaab can't send, delete or change anything." },
  { title: "Your history arrives", detail: "The last three months of bank alerts, ready to sort into categories." },
];

export function SignupPage() {
  const me = useMe();
  const info = useSignupInfo();
  const signup = useSignup();
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [mismatch, setMismatch] = useState(false);

  if (me.data) return <Navigate to="/" replace />;
  const first = info.data?.first_user === true;

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (password !== confirm) {
      setMismatch(true);
      return;
    }
    signup.mutate({ username, display_name: name, password }, { onSuccess: () => navigate("/", { replace: true }) });
  };

  return (
    <AuthLayout>
      <form className="login__form sheet" onSubmit={submit} noValidate>
        <div className="login__head">
          <h1 className="login__title">Create your account</h1>
          <p className="login__sub">{first ? "You'll be this ledger's admin" : "to your household ledger"}</p>
        </div>

        <label className="field">
          <span className="field__label">Your name</span>
          <input className="field__input" name="name" autoComplete="name" required maxLength={80}
            value={name}
            onChange={(e) => setName(e.target.value)} />
        </label>

        <div className="field">
          <label className="field__label" htmlFor="signup-username">Username</label>
          <input id="signup-username" className="field__input" name="username" autoComplete="username"
            autoCapitalize="none" spellCheck={false} required maxLength={32} aria-describedby="signup-username-help"
            value={username} onChange={(e) => setUsername(e.target.value)} />
          <span id="signup-username-help" className="field__help">
            3 to 32 letters, digits, dots, dashes or underscores
          </span>
        </div>

        <div className="field">
          <label className="field__label" htmlFor="signup-password">Password</label>
          <input id="signup-password" className="field__input" name="password" type="password"
            autoComplete="new-password" required maxLength={256} aria-describedby="signup-password-help" value={password}
            onChange={(e) => { setPassword(e.target.value); setMismatch(false); }} />
          <span id="signup-password-help" className="field__help">At least 8 characters</span>
        </div>

        <label className="field">
          <span className="field__label">Confirm password</span>
          <input className="field__input" name="confirm" type="password" autoComplete="new-password" required maxLength={256}
            aria-invalid={mismatch || undefined} value={confirm}
            onChange={(e) => { setConfirm(e.target.value); setMismatch(false); }} />
        </label>

        {mismatch ? (
          <p className="form-error" role="alert">The two passwords don't match.</p>
        ) : signup.isError && (
          <p className="form-error" role="alert">{describeError(signup.error)}</p>
        )}

        <button className="button button--primary" type="submit" disabled={signup.isPending}>
          {signup.isPending ? "Creating your account…" : "Create account"}
        </button>

        {first ? (
          <p className="login__switch">Others in your household can sign up here after you.</p>
        ) : (
          <p className="login__switch">
            Already have an account? <Link to="/login">Sign in</Link>
          </p>
        )}
      </form>

      <section className="login__next" aria-labelledby="signup-next">
        <h2 id="signup-next" className="login__next-title">What happens next</h2>
        <ol className="login__steps">
          {NEXT.map((step, i) => (
            <li key={step.title} className="login__step">
              <span className="login__step-mark" aria-hidden="true">{i + 1}</span>
              <span>
                <strong className="login__step-title">{step.title}</strong>
                <span className="login__step-detail">{step.detail}</span>
              </span>
            </li>
          ))}
        </ol>
      </section>
    </AuthLayout>
  );
}
