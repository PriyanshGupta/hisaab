// Accounts (PRD §6.6): add, edit and archive the places money sits, each with
// an opening balance anchor, plus the identifiers bank alerts use to find them.
import { type FormEvent, useState } from "react";

import {
  type Account,
  type AccountType,
  OPENING_LABELS,
  TYPE_LABELS,
  useAccounts,
  useAddUpiId,
  useArchiveAccount,
  useCreateAccount,
  useRemoveIdentifier,
  useUpdateAccount,
} from "../api/accounts";
import { describeError } from "../api/client";
import { formatDate, formatInstantDate, threeMonthsAgoIso } from "../dates";
import { Field } from "../forms/Field";
import { formatPaise, paiseToInput, parseRupees } from "../money";
import "../styles/accounts.css";

export function AccountsPage() {
  const accounts = useAccounts();
  const [adding, setAdding] = useState(false);
  const [showArchived, setShowArchived] = useState(false);

  const all = accounts.data ?? [];
  const active = all.filter((a) => a.archived_at === null);
  const archived = all.filter((a) => a.archived_at !== null);

  return (
    <>
      <header className="page-head">
        <h1 className="page-title">Accounts</h1>
        {!adding && (
          <button className="button button--primary" type="button" onClick={() => setAdding(true)}>
            Add account
          </button>
        )}
      </header>

      {adding && <AddAccountForm onDone={() => setAdding(false)} />}

      {accounts.isPending && <p className="muted">Loading accounts…</p>}
      {accounts.isError && <p className="form-error" role="alert">{describeError(accounts.error)}</p>}

      {accounts.isSuccess && active.length === 0 && !adding && (
        <section className="empty">
          <p className="empty__lead">No accounts yet.</p>
          <p className="empty__body">
            Add each bank account, credit card and cash wallet you want to track, with its balance on the day your
            history starts.
          </p>
        </section>
      )}

      <div className="account-list">
        {active.map((account) => (
          <AccountItem key={account.id} account={account} />
        ))}
      </div>

      {archived.length > 0 && (
        <section className="archived">
          <button className="button button--quiet" type="button" aria-expanded={showArchived}
            onClick={() => setShowArchived((v) => !v)}>
            {showArchived ? "Hide archived" : `Show archived (${archived.length})`}
          </button>
          {showArchived && (
            <div className="account-list account-list--archived">
              {archived.map((account) => (
                <AccountItem key={account.id} account={account} />
              ))}
            </div>
          )}
        </section>
      )}
    </>
  );
}

// --- one account ---------------------------------------------------------------

function AccountItem({ account }: { account: Account }) {
  const [mode, setMode] = useState<"view" | "edit" | "confirm-archive" | "add-upi">("view");
  const archive = useArchiveAccount();
  const isArchived = account.archived_at !== null;
  const upiIds = account.identifiers.filter((i) => i.removable); // UPI IDs, and numbers assigned from review

  return (
    <article className="account" aria-label={account.display_name}>
      <div className="account__main">
        <div className="account__who">
          <h2 className="account__name">{account.display_name}</h2>
          <p className="account__meta">
            {account.last4 ? `${TYPE_LABELS[account.type]} ending ${account.last4}` : TYPE_LABELS[account.type]}
            {account.institution && <span className="account__institution">{account.institution}</span>}
          </p>
        </div>
        <div className="account__opening">
          {account.opening_balance_paise === null || account.opening_balance_date === null ? (
            <span className="account__as-of">No opening balance</span>
          ) : (
            <>
              <span className="amount">{formatPaise(account.opening_balance_paise)}</span>
              <span className="account__as-of">
                {account.type === "credit_card" ? "owed on " : "on "}
                {formatDate(account.opening_balance_date)}
              </span>
            </>
          )}
        </div>
      </div>

      {isArchived ? (
        <p className="account__archived">
          Archived {formatInstantDate(account.archived_at!)}. Its alerts no longer match; past transactions stay in your reports.
        </p>
      ) : mode === "edit" ? (
        <EditAccountForm account={account} onDone={() => setMode("view")} />
      ) : mode === "confirm-archive" ? (
        <div className="confirm" role="group" aria-label={`Archive ${account.display_name}?`}>
          <p>
            Archive {account.display_name}? It leaves your lists and stops matching bank alerts. Its past transactions
            stay in your reports.
          </p>
          {archive.isError && <p className="form-error" role="alert">{describeError(archive.error)}</p>}
          <div className="actions">
            <button className="button button--primary" type="button" disabled={archive.isPending}
              onClick={() => archive.mutate(account.id)}>
              Archive account
            </button>
            <button className="button button--quiet" type="button" onClick={() => setMode("view")}>
              Keep it
            </button>
          </div>
        </div>
      ) : (
        <>
          {account.type !== "cash" && (
            <UpiIds account={account} upiIds={upiIds} adding={mode === "add-upi"} onDoneAdding={() => setMode("view")} />
          )}
          {mode === "view" && (
            <div className="actions">
              {account.type === "bank" && ( // UPI IDs belong to bank accounts; a card has none
                <button className="button button--quiet" type="button" onClick={() => setMode("add-upi")}>
                  Add UPI ID
                </button>
              )}
              <button className="button button--quiet" type="button" onClick={() => setMode("edit")}>Edit</button>
              <button className="button button--quiet" type="button" onClick={() => setMode("confirm-archive")}>
                Archive
              </button>
            </div>
          )}
        </>
      )}
    </article>
  );
}

function UpiIds({ account, upiIds, adding, onDoneAdding }: {
  account: Account; upiIds: Account["identifiers"]; adding: boolean; onDoneAdding: () => void;
}) {
  const add = useAddUpiId();
  const remove = useRemoveIdentifier();
  const [value, setValue] = useState("");

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!value.trim()) return;
    add.mutate({ id: account.id, upiId: value.trim() }, {
      onSuccess: () => {
        setValue("");
        onDoneAdding();
      },
    });
  };

  return (
    <div className="upi">
      {upiIds.length > 0 && (
        <ul className="upi__list" aria-label={`UPI IDs for ${account.display_name}`}>
          {upiIds.map((i) => (
            <li key={i.id} className="upi__id">
              <span>{i.identifier}</span>
              <button className="upi__remove" type="button" aria-label={`Remove ${i.identifier}`}
                onClick={() => remove.mutate({ id: account.id, identifierId: i.id })}>
                ×
              </button>
            </li>
          ))}
        </ul>
      )}
      {adding && (
        <form className="upi__add" onSubmit={submit}>
          <input className="field__input upi__input" aria-label={`Add a UPI ID to ${account.display_name}`}
            placeholder="name@bank" autoCapitalize="none" spellCheck={false} autoFocus value={value}
            onChange={(e) => setValue(e.target.value)} />
          <button className="button button--primary" type="submit" disabled={add.isPending}>Add</button>
          <button className="button button--quiet" type="button" onClick={onDoneAdding}>Cancel</button>
        </form>
      )}
      {(add.isError || remove.isError) && (
        <p className="form-error" role="alert">{describeError(add.error ?? remove.error)}</p>
      )}
    </div>
  );
}

// --- forms --------------------------------------------------------------------

type FieldErrors = Partial<Record<"name" | "last4" | "amount" | "date", string>>;

function AmountField({ type, value, onChange, error }: {
  type: AccountType; value: string; onChange: (v: string) => void; error: string | undefined;
}) {
  return (
    <Field
      label={OPENING_LABELS[type]}
      inputMode="decimal"
      placeholder="0.00"
      value={value}
      onChange={(e) => onChange(e.target.value)}
      help={`Optional. ${type === "credit_card"
        ? "What you'd have paid to clear the card on that date. Enter it as a positive number. "
        : ""}A balance on a past date lets Hisaab check your balances later; leave it blank if you don't know it.`}
      error={error}
    />
  );
}

/** paise: null when left blank (the opening balance is optional); undefined with an error. */
function checkAmount(type: AccountType, raw: string): { paise?: number | null; error?: string } {
  if (!raw.trim()) return { paise: null };
  const result = parseRupees(raw, { allowNegative: type === "bank" });
  if (!result.ok) {
    return { error: type === "credit_card" && raw.trim().startsWith("-")
      ? "Enter the amount owed as a positive number." : result.error };
  }
  return { paise: result.paise };
}

export function AddAccountForm({ onDone, defaultDate, defaultLast4 }: {
  onDone: () => void; defaultDate?: string; defaultLast4?: string; // from review: "Add an account ending 4821"
}) {
  const create = useCreateAccount();
  const [type, setType] = useState<AccountType>("bank");
  const [name, setName] = useState("");
  const [institution, setInstitution] = useState("");
  const [last4, setLast4] = useState(defaultLast4 ?? "");
  const [amount, setAmount] = useState("");
  const [date, setDate] = useState(defaultDate ?? threeMonthsAgoIso());
  const [errors, setErrors] = useState<FieldErrors>({});

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const found: FieldErrors = {};
    if (!name.trim()) found.name = "Give the account a name, e.g. 'HDFC Savings'.";
    if (type !== "cash" && (last4.match(/\d/g)?.length ?? 0) < 4) found.last4 = "Enter the last 4 digits.";
    const { paise, error } = checkAmount(type, amount);
    if (error) found.amount = error;
    if (paise !== null && !date) found.date = "Pick the date of this balance.";
    setErrors(found);
    if (Object.keys(found).length > 0 || paise === undefined) return;
    create.mutate(
      {
        type,
        display_name: name.trim(),
        institution: institution.trim() || null,
        ...(type !== "cash" && { last4: last4.trim() }),
        opening_balance_paise: paise,
        opening_balance_date: paise === null ? null : date,
      },
      { onSuccess: onDone },
    );
  };

  return (
    <form className="sheet sheet--ruled account-form" aria-label="Add account" onSubmit={submit} noValidate>
      <fieldset className="choice">
        <legend className="field__label">Type</legend>
        {(Object.keys(TYPE_LABELS) as AccountType[]).map((t) => (
          <label key={t} className="choice__option">
            <input type="radio" name="type" value={t} checked={type === t} onChange={() => setType(t)} />
            {TYPE_LABELS[t]}
          </label>
        ))}
      </fieldset>

      <div className="form-grid">
        <Field label="Name" value={name} onChange={(e) => setName(e.target.value)} error={errors.name}
          placeholder={type === "cash" ? "Wallet" : type === "credit_card" ? "Regalia card" : "HDFC Savings"} />

        {type !== "cash" && (
          <>
            <Field label="Bank or card issuer (optional)" value={institution}
              onChange={(e) => setInstitution(e.target.value)} />
            <Field label="Last 4 digits" inputMode="numeric" autoComplete="off" value={last4}
              onChange={(e) => setLast4(e.target.value)} error={errors.last4}
              help="How bank alerts find this account. The full number is never needed." />
          </>
        )}

        <AmountField type={type} value={amount} onChange={setAmount} error={errors.amount} />

        <Field label="As of" type="date" value={date} onChange={(e) => setDate(e.target.value)} error={errors.date}
          help="The day your history starts: 3 months ago, unless you know better." />
      </div>

      {create.isError && <p className="form-error" role="alert">{describeError(create.error)}</p>}

      <div className="actions">
        <button className="button button--primary" type="submit" disabled={create.isPending}>Add account</button>
        <button className="button button--quiet" type="button" onClick={onDone}>Cancel</button>
      </div>
    </form>
  );
}

function EditAccountForm({ account, onDone }: { account: Account; onDone: () => void }) {
  const update = useUpdateAccount();
  const [name, setName] = useState(account.display_name);
  const [institution, setInstitution] = useState(account.institution ?? "");
  const [amount, setAmount] = useState(
    account.opening_balance_paise === null ? "" : paiseToInput(account.opening_balance_paise));
  const [date, setDate] = useState(account.opening_balance_date ?? threeMonthsAgoIso());
  const [errors, setErrors] = useState<FieldErrors>({});
  const [type, setType] = useState<AccountType>(account.type); // bank <-> card, for one entered as the wrong kind

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const found: FieldErrors = {};
    if (!name.trim()) found.name = "Give the account a name.";
    const { paise, error } = checkAmount(type, amount);
    if (error) found.amount = error;
    setErrors(found);
    if (Object.keys(found).length > 0 || paise === undefined) return;
    update.mutate(
      {
        id: account.id,
        change: {
          ...(type !== account.type && { type }),
          display_name: name.trim(),
          institution: institution.trim() || null,
          opening_balance_paise: paise,
          opening_balance_date: paise === null ? null : date,
        },
      },
      { onSuccess: onDone },
    );
  };

  return (
    <form className="account-form account-form--inline" aria-label={`Edit ${account.display_name}`} onSubmit={submit}
      noValidate>
      <div className="form-grid">
        <Field label="Name" value={name} onChange={(e) => setName(e.target.value)} error={errors.name} />
        {account.type !== "cash" && (
          <Field label="Bank or card issuer (optional)" value={institution}
            onChange={(e) => setInstitution(e.target.value)} />
        )}
        {account.type !== "cash" && (
          <fieldset className="choice">
            <legend className="field__label">Type</legend>
            {(["bank", "credit_card"] as const).map((t) => (
              <label key={t} className="choice__option">
                <input type="radio" name={`type-${account.id}`} value={t} checked={type === t} onChange={() => setType(t)} />
                {TYPE_LABELS[t]}
              </label>
            ))}
            {type !== account.type && (
              <p className="field__help">
                Its transactions will be read again: money into a card is a refund or a bill payment, not income.
              </p>
            )}
          </fieldset>
        )}
        <AmountField type={type} value={amount} onChange={setAmount} error={errors.amount} />
        <Field label="As of" type="date" value={date} onChange={(e) => setDate(e.target.value)} />
      </div>
      {update.isError && <p className="form-error" role="alert">{describeError(update.error)}</p>}
      <div className="actions">
        <button className="button button--primary" type="submit" disabled={update.isPending}>Save changes</button>
        <button className="button button--quiet" type="button" onClick={onDone}>Cancel</button>
      </div>
    </form>
  );
}
