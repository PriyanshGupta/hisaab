// First-run setup (tech-design §5.4, PRD §6.1), in strict order:
// accounts -> opening balances -> Gmail -> backfill -> bulk categorization.
// Backfilling before accounts exist would bring every transaction in with an
// unknown account. The server says where the user is and refuses Gmail until
// accounts and balances are right; the accounts -> balances -> Gmail walk is
// local, so a reload starts again at the accounts list, one click from where it was.
import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, Navigate, useSearchParams } from "react-router";

import { type Account, TYPE_LABELS, useAccounts } from "../api/accounts";
import { describeError } from "../api/client";
import { useSyncNow } from "../api/gmail";
import { type Setup, setupKey, useSetup } from "../api/setup";
import { formatDate } from "../dates";
import { formatPaise } from "../money";
import { AddAccountForm } from "./AccountsPage";
import { ConnectButton } from "./SettingsPage";
import "../styles/setup.css";

const STEPS = ["Accounts", "Opening balances", "Gmail", "History", "Categorize"] as const;
type Shown = (typeof STEPS)[number];

const RETURN_MESSAGES: Record<string, string> = {
  denied: "Nothing was connected: access wasn't granted on Google's screen.",
  error: "Gmail couldn't be connected. Try again.",
};

export function SetupPage() {
  const setup = useSetup();
  const [params] = useSearchParams();
  // back from Google's screen (a full page load): straight to the Gmail step, where its message shows
  const [walked, setWalked] = useState<"accounts" | "balances" | "gmail">(params.has("gmail") ? "gmail" : "accounts");

  if (setup.isPending) return <p className="muted">Loading…</p>;
  if (setup.isError) return <p className="form-error" role="alert">{describeError(setup.error)}</p>;
  const { step } = setup.data;
  if (step === "categorize") return <Navigate to="/categorize" replace />;
  if (step === "done") return <Navigate to="/" replace />;

  const shown: Shown = step === "backfill" ? "History"
    : step === "accounts" || walked === "accounts" ? "Accounts"
    : walked === "balances" ? "Opening balances" : "Gmail";
  return (
    <>
      <header className="page-head"><h1 className="page-title">Set up Hisaab</h1></header>
      <ol className="setup-steps" aria-label="Setup steps">
        {STEPS.map((name) => (
          <li key={name} className="setup-steps__step" aria-current={name === shown ? "step" : undefined}>{name}</li>
        ))}
      </ol>
      {shown === "Accounts" && <AccountsStep setup={setup.data} onContinue={() => setWalked("balances")} />}
      {shown === "Opening balances" && <BalancesStep setup={setup.data} onContinue={() => setWalked("gmail")} />}
      {shown === "Gmail" && <GmailStep />}
      {shown === "History" && <HistoryStep setup={setup.data} />}
    </>
  );
}

function AccountsStep({ setup, onContinue }: { setup: Setup; onContinue: () => void }) {
  const accounts = useAccounts();
  const active = (accounts.data ?? []).filter((a) => a.archived_at === null);
  const [adding, setAdding] = useState(false);
  const formOpen = adding || (accounts.isSuccess && active.length === 0);

  return (
    <section className="setup-step" aria-labelledby="step-heading">
      <h2 id="step-heading" className="setup-step__title">Your accounts</h2>
      <p>
        Add each bank account, credit card and cash wallet you want to track, with its balance on{" "}
        <strong>{formatDate(setup.backfill_start)}</strong>, where your history starts. Doing this before Gmail
        means every bank alert finds its account.
      </p>
      {active.length > 0 && (
        <ul className="setup-list">
          {active.map((a) => <li key={a.id}>{a.display_name} <span className="muted">{TYPE_LABELS[a.type]}</span></li>)}
        </ul>
      )}
      {formOpen ? (
        <AddAccountForm defaultDate={setup.backfill_start} onDone={() => setAdding(false)} />
      ) : (
        <div className="actions">
          <button className="button button--quiet" type="button" onClick={() => setAdding(true)}>
            Add another account
          </button>
          <button className="button button--primary" type="button" disabled={active.length === 0}
            onClick={onContinue}>
            Continue
          </button>
        </div>
      )}
    </section>
  );
}

function BalancesStep({ setup, onContinue }: { setup: Setup; onContinue: () => void }) {
  const accounts = useAccounts();
  const active = (accounts.data ?? []).filter((a) => a.archived_at === null);
  const late = (a: Account) => a.opening_balance_date > setup.backfill_start; // ISO dates compare as strings

  return (
    <section className="setup-step" aria-labelledby="step-heading">
      <h2 id="step-heading" className="setup-step__title">Opening balances</h2>
      <p>
        Each balance should be as of {formatDate(setup.backfill_start)} or earlier, so that the three months of
        history arriving next add up from it. A card's figure is what you owed.
      </p>
      <table className="ledger">
        <tbody>
          {active.map((a) => (
            <tr key={a.id}>
              <td>{a.display_name}</td>
              <td className="amount">{formatPaise(a.opening_balance_paise)}</td>
              <td>
                {formatDate(a.opening_balance_date)}
                {late(a) && (
                  <span className="form-error setup-late">
                    {" "}after {formatDate(setup.backfill_start)}: change it on the <Link to="/accounts">Accounts</Link>{" "}
                    page (enter 0 if the account is newer)
                  </span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="actions">
        <button className="button button--primary" type="button" disabled={active.some(late)} onClick={onContinue}>
          Continue
        </button>
      </div>
    </section>
  );
}

function GmailStep() {
  const [params] = useSearchParams();
  const returned = RETURN_MESSAGES[params.get("gmail") ?? ""];
  return (
    <section className="setup-step" aria-labelledby="step-heading">
      <h2 id="step-heading" className="setup-step__title">Connect Gmail</h2>
      {returned && <p className="form-error" role="alert">{returned}</p>}
      <p>
        Hisaab reads the bank alerts in your inbox, starting with the last three months. Access is read-only:
        it can't send, delete or change anything.
      </p>
      <ConnectButton label="Connect Gmail" />
    </section>
  );
}

function HistoryStep({ setup }: { setup: Setup }) {
  const queryClient = useQueryClient();
  const resume = useSyncNow();
  const job = setup.job;
  const p = job?.progress ?? null;
  // Any finished job while the step is still backfill stopped short: failed, or some emails couldn't be fetched
  const stopped = job !== null && job.state !== "queued" && job.state !== "running";
  const unfetched = Number(job?.result?.failed ?? 0);

  let line = "Waiting to start…";
  if (p && p.read != null) line = `Reading ${p.read} of ${p.to_read} emails`;
  // Gmail's estimate can be far off (201 for 426 on a real inbox): once passed, it is dropped, not shown wrong
  else if (p && p.fetched != null) line = p.estimate != null && p.fetched < p.estimate
    ? `Fetched ${p.fetched} of ~${p.estimate} emails` : `Fetched ${p.fetched} emails`;

  return (
    <section className="setup-step" aria-labelledby="step-heading">
      <h2 id="step-heading" className="setup-step__title">Bringing in your history</h2>
      <p>Fetching the last three months of bank emails and reading each one. You can close this page; it carries on.</p>
      {stopped ? (
        <>
          <p className="form-error" role="alert">
            {unfetched > 0
              ? `${unfetched} ${unfetched === 1 ? "email" : "emails"} couldn't be fetched.`
              : `The backfill stopped: ${job.error ?? "it didn't finish"}.`}{" "}
            Nothing fetched so far is lost; resuming picks up where it left off.
          </p>
          <button className="button button--primary" type="button" disabled={resume.isPending}
            onClick={() => resume.mutate(undefined, { onSuccess: () => queryClient.invalidateQueries({ queryKey: setupKey }) })}>
            Resume
          </button>
          {resume.isError && <p className="form-error" role="alert">{describeError(resume.error)}</p>}
        </>
      ) : (
        <p className="settings-note" aria-live="polite">{line}</p>
      )}
    </section>
  );
}
