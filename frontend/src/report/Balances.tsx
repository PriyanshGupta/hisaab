// Balances and the reconciliation check on the report (tech-design §8.3, §8.4; slice 20).
//
// The balances panel never shows a bare confident number: each account reads "₹84,200, as of 12 Aug
// from the statement, plus 34 transactions since", the bank's last word plus the app's own arithmetic.
// A gap is a banner naming its account, never a modal, never added to another account's (a bank balance
// and a card's amount owed are different quantities). It ends one of two ways: fixed elsewhere (the gap
// goes, then "Close as investigated"), or accepted as an adjustment that counts in no total.
// No chart: each account is one current value, and its verified history is a handful of points (dataviz).
import { type FormEvent, useState } from "react";
import { Link } from "react-router";

import { type AccountBalance, type BalanceGap, useBalances, useCheckpoint, useResolveGap } from "../api/balances";
import { describeError } from "../api/client";
import { formatDate } from "../dates";
import { formatPaise, parseRupees } from "../money";
import { Icon } from "../ui/Icon";
import { indiaToday } from "./period";
import "../styles/balances.css";

/** "12 Aug" this year, "12 Aug 2025" otherwise. */
function short(iso: string): string {
  const full = formatDate(iso);
  return iso.slice(0, 4) === indiaToday().slice(0, 4) ? full.slice(0, -5) : full;
}

const SAID_BY: Record<BalanceGap["observed_source"], string> = {
  statement: "The statement", email: "An email", manual: "Your balance", opening: "Your opening balance",
};
const VERIFIED_BY: Record<string, string> = { statement: "from the statement", email: "from an email",
  manual: "from your own entry" };

const owed = (b: AccountBalance, paise: number) => formatPaise(paise) + (b.type === "credit_card" ? " owed" : "");
const since = (n: number) => (n === 0 ? "nothing since" : `plus ${n} ${n === 1 ? "transaction" : "transactions"} since`);

/** In words, which way the gap runs: the external figure against Hisaab's. */
function direction(b: AccountBalance, g: BalanceGap): string {
  const who = `${SAID_BY[g.observed_source]} on ${short(g.observed_on)} says`;
  const way = g.gap_paise > 0 ? "more" : "less";
  const amount = formatPaise(Math.abs(g.gap_paise));
  return b.type === "credit_card" ? `${who} you owe ${amount} ${way} than Hisaab has.`
    : `${who} ${amount} ${way} than Hisaab has.`;
}

export function BalanceBanners() {
  const balances = useBalances();
  const gaps = (balances.data ?? []).flatMap((b) => b.gaps.map((g) => ({ b, g })));
  if (gaps.length === 0) return null;
  return (
    <div className="checks">
      {gaps.map(({ b, g }) => <Banner key={g.observation_id} b={b} g={g} />)}
    </div>
  );
}

function Banner({ b, g }: { b: AccountBalance; g: BalanceGap }) {
  const accept = useResolveGap("accept");
  const close = useResolveGap("close");
  const [confirming, setConfirming] = useState(false);
  const failed = accept.error ?? close.error;
  const label = `Balance check — ${b.name}`;
  if (g.state === "fixed") {
    return (
      <section className="check check--fixed" role="status" aria-label={label} id={`check-${g.observation_id}`}>
        <Icon name="check" size={20} className="check__icon" />
        <div className="check__text">
          <p className="check__title">{b.name}: the gap since {short(g.since)} is gone.</p>
          <p className="check__body">Whatever caused it has been fixed. Close it as investigated: nothing is written.</p>
        </div>
        <div className="actions check__actions">
          <button className="button button--secondary button--small" type="button" disabled={close.isPending}
            onClick={() => close.mutate(g.observation_id)}>Close as investigated</button>
        </div>
        {failed && <p className="form-error check__error" role="alert">{describeError(failed)}</p>}
      </section>
    );
  }
  return (
    <section className="check" role="status" aria-label={label} id={`check-${g.observation_id}`}>
      <Icon name="alert" size={20} className="check__icon" />
      <div className="check__text">
        <p className="check__title">
          {label}: {formatPaise(Math.abs(g.gap_paise))} unexplained since {short(g.since)}
        </p>
        <p className="check__body">{direction(b, g)}</p>
      </div>
      {confirming ? (
        <div className="check__confirm">
          <p>
            This writes a {formatPaise(Math.abs(g.gap_paise))} adjustment to {b.name}, dated {short(g.observed_on)}. It
            counts in no total: not spending, not cash out, not money in.
          </p>
          <div className="actions">
            <button className="button button--primary button--small" type="button" disabled={accept.isPending}
              onClick={() => accept.mutate(g.observation_id)}>Write the adjustment</button>
            <button className="button button--secondary button--small" type="button" onClick={() => setConfirming(false)}>Cancel</button>
          </div>
        </div>
      ) : (
        <div className="actions check__actions">
          <button className="button button--secondary button--small" type="button" onClick={() => setConfirming(true)}>
            Accept as an adjustment
          </button>
          <Link className="button button--quiet button--small" to={`/transactions?from=${g.since}&to=${g.observed_on}`}>See transactions</Link>
        </div>
      )}
      {failed && <p className="form-error check__error" role="alert">{describeError(failed)}</p>}
    </section>
  );
}

export function BalancesPanel() {
  const balances = useBalances();
  if (!balances.data?.length) return null;
  return (
    <section className="sheet sheet--pad balances" aria-labelledby="balances-h">
      <div className="section-head">
        <h2 id="balances-h">Balances</h2>
        <p>What each bank last said, plus the transactions Hisaab has counted since</p>
      </div>
      <div className="balances__grid">
        {balances.data.map((b) => <BalanceRow key={b.account_id} b={b} />)}
      </div>
    </section>
  );
}

function BalanceRow({ b }: { b: AccountBalance }) {
  const [entering, setEntering] = useState(false);
  const open = b.gaps.filter((g) => g.state === "open");
  const verified = b.verified;
  return (
    <div className="balance" role="group" aria-label={b.name}>
      <span className="balance__name">{b.name}</span>
      {verified && b.balance_paise !== null
        ? <span className="balance__figure">{owed(b, b.balance_paise)}</span>
        : <span className="balance__none">No starting balance yet</span>}
      <span className="balance__how">
        {verified === null ? "Enter one to start checking this account."
          : verified.source === "opening"
            ? `from your opening balance on ${short(verified.on)}, ${since(b.transactions_since)}`
            : `as of ${short(verified.on)} ${VERIFIED_BY[verified.source]}, ${since(b.transactions_since)}`}
      </span>
      {open.map((g) => (
        <a key={g.observation_id} className="balance__gap" href={`#check-${g.observation_id}`}>
          <Icon name="alert" size={14} />
          {formatPaise(Math.abs(g.gap_paise))} unexplained
        </a>
      ))}
      {!entering && (
        <div className="actions">
          <button className="button button--quiet button--small balance__enter" type="button" onClick={() => setEntering(true)}>
            Enter today's balance
          </button>
        </div>
      )}
      {entering && <Checkpoint b={b} onDone={() => setEntering(false)} />}
    </div>
  );
}

function Checkpoint({ b, onDone }: { b: AccountBalance; onDone: () => void }) {
  const save = useCheckpoint(b.account_id);
  const [amount, setAmount] = useState("");
  const [on, setOn] = useState(indiaToday());
  const [problem, setProblem] = useState<string | null>(null);
  const card = b.type === "credit_card";
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const parsed = parseRupees(amount, { allowNegative: !card });
    if (!parsed.ok) { setProblem(parsed.error); return; }
    setProblem(null);
    save.mutate({ balance_paise: parsed.paise, on }, { onSuccess: onDone });
  };
  const id = `checkpoint-${b.account_id}`;
  return (
    <form className="checkpoint" onSubmit={submit} aria-label={`Balance for ${b.name}`}>
      <p className="checkpoint__note">
        {card ? "For a card, enter what you owe as a positive amount. " : ""}
        Enter the balance at the end of the day, before anything else moves.
      </p>
      <div className="checkpoint__fields">
        <label className="field">
          <span className="field__label">{card ? "Amount owed" : "Balance"}</span>
          <input className="field__input" inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)}
            aria-describedby={`${id}-error`} />
        </label>
        <label className="field">
          <span className="field__label">On</span>
          <input className="field__input" type="date" value={on} max={indiaToday()} onChange={(e) => setOn(e.target.value)} />
        </label>
        <div className="actions">
          <button className="button button--primary" type="submit" disabled={save.isPending}>Save balance</button>
          <button className="button button--secondary" type="button" onClick={onDone}>Cancel</button>
        </div>
      </div>
      {(problem || save.error) && (
        <p className="form-error" role="alert" id={`${id}-error`}>{problem ?? describeError(save.error)}</p>
      )}
    </form>
  );
}
