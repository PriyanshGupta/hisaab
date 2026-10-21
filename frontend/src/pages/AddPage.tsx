// Manual entry (PRD §4.12, §6.5): cash and anything the automated path misses.
// Built for speed on a phone: kind, account, amount, who, and today's date by
// default. After saving, the kind, account and date stay put for the next entry.
import { type FormEvent, useState } from "react";
import { Link } from "react-router";

import { type Account, type AccountType, useAccounts } from "../api/accounts";
import { useCategories, usePayees } from "../api/categories";
import { describeError } from "../api/client";
import { type NewTransaction, useCreateTransaction } from "../api/transactions";
import { todayIso } from "../dates";
import { Field } from "../forms/Field";
import { formatPaise, parseRupees } from "../money";
import { Icon } from "../ui/Icon";

type Kind = NewTransaction["kind"];
type LegSpec = { label: string; types: AccountType[]; missing: string };

const ALL: AccountType[] = ["bank", "credit_card", "cash"];

// Which accounts each kind uses (PRD §3.2, cash refunds per ruling 2026-10-11).
const KINDS: Record<Kind, { label: string; from: LegSpec | null; to: LegSpec | null; payee: string | null }> = {
  expense: { label: "Expense", payee: "Paid to", to: null,
    from: { label: "Paid from", types: ALL, missing: "Choose the account it was paid from." } },
  income: { label: "Income", payee: "From", from: null,
    to: { label: "Received into", types: ["bank", "cash"], missing: "Choose the account it went into." } },
  transfer: { label: "Transfer", payee: null,
    from: { label: "From", types: ALL, missing: "Choose the account it came from." },
    to: { label: "To", types: ALL, missing: "Choose the account it went to." } },
  refund: { label: "Refund", payee: "From", from: null,
    to: { label: "Refunded into", types: ALL, missing: "Choose the account it went into." } },
};

type Errors = Partial<Record<"from" | "to" | "amount" | "date", string>>;

export function AddPage() {
  const accounts = useAccounts();
  const categories = useCategories();
  const payees = usePayees();
  const create = useCreateTransaction();
  const [kind, setKind] = useState<Kind>("expense");
  const [fromId, setFromId] = useState("");
  const [toId, setToId] = useState("");
  const [amount, setAmount] = useState("");
  const [payee, setPayee] = useState("");
  const [date, setDate] = useState(todayIso());
  const [categoryId, setCategoryId] = useState("");
  const [errors, setErrors] = useState<Errors>({});
  const [saved, setSaved] = useState<string | null>(null);

  const active = (accounts.data ?? []).filter((a) => a.archived_at === null);
  const activeCategories = (categories.data ?? []).filter((c) => c.archived_at === null);
  const spec = KINDS[kind];

  const chooseKind = (next: Kind) => {
    setKind(next);
    setFromId("");
    setToId("");
    setErrors({});
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    setSaved(null);
    const found: Errors = {};
    if (spec.from && !fromId) found.from = spec.from.missing;
    if (spec.to && !toId) found.to = spec.to.missing;
    if (spec.from && spec.to && fromId && fromId === toId) found.to = "Pick two different accounts.";
    const parsed = parseRupees(amount);
    if (!parsed.ok) found.amount = parsed.error;
    else if (parsed.paise === 0) found.amount = "Enter an amount more than zero.";
    if (!date) found.date = "Pick the date.";
    setErrors(found);
    if (Object.keys(found).length > 0 || !parsed.ok) return;

    const who = spec.payee ? payee.trim() : "";
    const body: NewTransaction = {
      kind,
      amount_paise: parsed.paise,
      transaction_date: date,
      ...(spec.from && { from_account_id: fromId }),
      ...(spec.to && { to_account_id: toId }),
      ...(who && { payee: who }),
      ...(spec.payee && categoryId && { category_id: categoryId }),
    };
    create.mutate(body, {
      onSuccess: () => {
        setSaved(`Saved ${formatPaise(parsed.paise)}${who ? `${kind === "expense" ? " to " : " from "}${who}` : ""}.`);
        setAmount("");
        setPayee("");
        setCategoryId("");
      },
    });
  };

  // No empty pickers, even briefly. (Payee suggestions are optional; they may arrive later.)
  if (accounts.isPending || categories.isPending) return <p className="loading">Loading…</p>;
  if (accounts.isError) return <p className="form-error" role="alert">{describeError(accounts.error)}</p>;
  if (active.length === 0) {
    return (
      <>
        <header className="page-head"><h1 className="page-title">Add</h1></header>
        <section className="empty">
          <p className="empty__lead">Every transaction needs an account.</p>
          <p className="empty__body"><Link to="/accounts">Add an account first</Link>, then come back here.</p>
        </section>
      </>
    );
  }

  return (
    <>
      <header className="page-head"><h1 className="page-title">Add</h1></header>
      <form className="sheet entry-form" aria-label="Add a transaction" onSubmit={submit} noValidate>
        <fieldset className="choice">
          <legend className="field__label">Kind</legend>
          {(Object.keys(KINDS) as Kind[]).map((k) => (
            <label key={k} className="choice__option">
              <input type="radio" name="kind" value={k} checked={kind === k} onChange={() => chooseKind(k)} />
              {KINDS[k].label}
            </label>
          ))}
        </fieldset>

        <div className="form-grid">
          <div className="field entry-form__amount">
            <label className="field__label" htmlFor="entry-amount">Amount</label>
            <span className="money-input">
              <span className="money-input__sign" aria-hidden="true">₹</span>
              <input id="entry-amount" className="field__input amount-input" inputMode="decimal" placeholder="0.00"
                value={amount} onChange={(e) => setAmount(e.target.value)} aria-invalid={errors.amount ? true : undefined}
                aria-describedby={errors.amount ? "entry-amount-error" : undefined} />
            </span>
            {errors.amount && <span className="field__error" id="entry-amount-error">{errors.amount}</span>}
          </div>
          <Field label="Date" type="date" value={date} max={todayIso()} onChange={(e) => setDate(e.target.value)}
            error={errors.date} />
          {spec.from && <AccountSelect spec={spec.from} accounts={active} value={fromId} onChange={setFromId}
            error={errors.from} />}
          {spec.to && <AccountSelect spec={spec.to} accounts={active} value={toId} onChange={setToId}
            error={errors.to} />}
          {spec.payee && (
            <>
              <Field label={spec.payee} value={payee} onChange={(e) => setPayee(e.target.value)}
                placeholder={kind === "expense" ? "Tea stall" : "Who"} list="payee-names" autoComplete="off" />
              <datalist id="payee-names">
                {(payees.data ?? []).map((p) => <option key={p.id} value={p.name} />)}
              </datalist>
              <div className="field">
                <label className="field__label" htmlFor="entry-category">Category</label>
                <select id="entry-category" className="field__input" value={categoryId}
                  onChange={(e) => setCategoryId(e.target.value)}>
                  <option value="">No category</option>
                  {activeCategories.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
                </select>
              </div>
            </>
          )}
        </div>

        {create.isError && <p className="form-error" role="alert">{describeError(create.error)}</p>}

        <div className="entry-form__actions">
          <button className="button button--primary" type="submit" disabled={create.isPending}>Save</button>
          {saved && <p className="saved" role="status"><Icon name="check" size={18} />{saved}</p>}
        </div>
      </form>
    </>
  );
}

function AccountSelect({ spec, accounts, value, onChange, error }: {
  spec: LegSpec; accounts: Account[]; value: string; onChange: (v: string) => void; error: string | undefined;
}) {
  const id = `leg-${spec.label.toLowerCase().replace(/\W+/g, "-")}`;
  const usable = accounts.filter((a) => spec.types.includes(a.type));
  return (
    <div className="field">
      <label className="field__label" htmlFor={id}>{spec.label}</label>
      <select id={id} className="field__input" value={value} onChange={(e) => onChange(e.target.value)}
        aria-invalid={error ? true : undefined} aria-describedby={error ? `${id}-error` : undefined}>
        <option value="">Choose an account</option>
        {usable.map((a) => <option key={a.id} value={a.id}>{a.display_name}</option>)}
      </select>
      {error && <span className="field__error" id={`${id}-error`}>{error}</span>}
    </div>
  );
}
