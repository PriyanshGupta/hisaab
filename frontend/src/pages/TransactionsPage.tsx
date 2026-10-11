// The transaction list (PRD §3.11): every entry with where it came from, any
// corrections beside the source's value, and undo. Corrections here cover the
// amount and date; marking a transfer or assigning an account belongs to the
// review queue (slice 17), though the API already accepts those fields.
import { type FormEvent, useState } from "react";

import { type Account, useAccounts } from "../api/accounts";
import { describeError } from "../api/client";
import {
  type EditableField,
  type Transaction,
  isCredit,
  useCorrectTransaction,
  useDeleteTransaction,
  useTransactions,
  useUndoCorrection,
} from "../api/transactions";
import { formatDate, todayIso } from "../dates";
import { Field } from "../forms/Field";
import { formatPaise, paiseToInput, parseRupees } from "../money";
import "../styles/transactions.css";

const PROVENANCE = { manual: "your manual entry", email: "a bank email", statement: "a statement" } as const;
const FIELD_NAMES: Record<string, string> = {
  amount: "amount", transaction_date: "date", kind: "kind", from_account_id: "from account", to_account_id: "to account",
};

export function TransactionsPage() {
  const transactions = useTransactions();
  const accounts = useAccounts();
  const [open, setOpen] = useState<string | null>(null);
  const names = new Map((accounts.data ?? []).map((a) => [a.id, a.display_name]));

  return (
    <>
      <header className="page-head"><h1 className="page-title">Transactions</h1></header>
      {transactions.isPending && <p className="muted">Loading…</p>}
      {transactions.isError && <p className="form-error" role="alert">{describeError(transactions.error)}</p>}
      {transactions.isSuccess && transactions.data.length === 0 && (
        <section className="empty">
          <p className="empty__lead">No transactions yet.</p>
          <p className="empty__body">Bank alerts will appear here once Gmail is connected. Cash goes in through Add.</p>
        </section>
      )}
      {transactions.isSuccess && transactions.data.length > 0 && (
        <table className="ledger txn-table">
          <thead>
            <tr>
              <th scope="col">Date</th>
              <th scope="col">Details</th>
              <th scope="col" className="txn-table__account">Account</th>
              <th scope="col" className="amount">Amount</th>
            </tr>
          </thead>
          <tbody>
            {transactions.data.map((t) => (
              <Row key={t.id} t={t} names={names} accounts={accounts.data ?? []} open={open === t.id}
                onToggle={() => setOpen(open === t.id ? null : t.id)} />
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}

function accountText(t: Transaction, names: Map<string, string>): string {
  const name = (id: string | null) => (id ? names.get(id) ?? "Unknown account" : null);
  if (t.kind === "transfer") return `${name(t.from_account_id)} to ${name(t.to_account_id)}`;
  return name(t.from_account_id) ?? name(t.to_account_id) ?? "Not yet matched";
}

function Row({ t, names, accounts, open, onToggle }: {
  t: Transaction; names: Map<string, string>; accounts: Account[]; open: boolean; onToggle: () => void;
}) {
  const label = t.payee_raw ?? (t.kind === "transfer" ? "Transfer" : t.kind[0]!.toUpperCase() + t.kind.slice(1));
  return (
    <>
      <tr className={open ? "txn-row txn-row--open" : "txn-row"}>
        <td className="txn-table__date">{formatDate(t.transaction_date)}</td>
        <td>
          <button className="txn-row__toggle" type="button" aria-expanded={open} onClick={onToggle}>
            {label}
          </button>
          {t.edits.length > 0 && <span className="txn-row__edited">edited</span>}
        </td>
        <td className="txn-table__account">{accountText(t, names)}</td>
        <td className="amount">
          {formatPaise(t.amount_paise)}
          {isCredit(t) && <span className="cr">Cr</span>}
        </td>
      </tr>
      {open && (
        <tr className="txn-detail">
          <td colSpan={4}>
            <Detail t={t} names={names} accounts={accounts} />
          </td>
        </tr>
      )}
    </>
  );
}

function describeSourceValue(field: string, prior: string | null, names: Map<string, string>): string {
  if (prior === null) return "nothing";
  if (field === "amount") return formatPaise(Number(prior));
  if (field === "transaction_date") return formatDate(prior);
  if (field.endsWith("_account_id")) return names.get(prior) ?? "another account";
  return prior;
}

function Detail({ t, names }: { t: Transaction; names: Map<string, string>; accounts: Account[] }) {
  const undo = useUndoCorrection();
  const remove = useDeleteTransaction();
  const [confirmDelete, setConfirmDelete] = useState(false);

  return (
    <div className="txn-detail__body">
      <p className="muted">From {PROVENANCE[t.provenance]}.</p>

      {t.edits.length > 0 && (
        <ul className="txn-edits">
          {t.edits.map((e) => (
            <li key={e.field}>
              <span>
                {e.field === "amount" ? "Edited." : `${FIELD_NAMES[e.field] ?? e.field} edited.`.replace(/^./, (c) => c.toUpperCase())}{" "}
                The entry said {describeSourceValue(e.field, e.prior_value, names)}.
              </span>
              <button className="button button--quiet" type="button" disabled={undo.isPending}
                aria-label={`Undo ${FIELD_NAMES[e.field] ?? e.field}`}
                onClick={() => undo.mutate({ id: t.id, fields: [e.field as EditableField] })}>
                Undo
              </button>
            </li>
          ))}
        </ul>
      )}
      {undo.isError && <p className="form-error" role="alert">{describeError(undo.error)}</p>}

      <CorrectionForm key={`${t.amount_paise}|${t.transaction_date}`} t={t} />

      {t.provenance === "manual" && (
        confirmDelete ? (
          <div className="confirm" role="group" aria-label="Delete this entry?">
            <p>
              Delete this entry? It can't be undone.
              {t.edits.length > 0 && " Its corrections will be lost too."}
            </p>
            {remove.isError && <p className="form-error" role="alert">{describeError(remove.error)}</p>}
            <div className="actions">
              <button className="button button--primary" type="button" disabled={remove.isPending}
                onClick={() => remove.mutate(t.id)}>Delete it</button>
              <button className="button button--quiet" type="button" onClick={() => setConfirmDelete(false)}>
                Keep it
              </button>
            </div>
          </div>
        ) : (
          <div className="actions">
            <button className="button button--quiet" type="button" onClick={() => setConfirmDelete(true)}>
              Delete entry
            </button>
          </div>
        )
      )}
    </div>
  );
}

function CorrectionForm({ t }: { t: Transaction }) {
  const correct = useCorrectTransaction();
  const [amount, setAmount] = useState(paiseToInput(t.amount_paise));
  const [date, setDate] = useState(t.transaction_date);
  const [error, setError] = useState<string | undefined>();

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const parsed = parseRupees(amount);
    if (!parsed.ok || parsed.paise === 0) {
      setError(parsed.ok ? "Enter an amount more than zero." : parsed.error);
      return;
    }
    setError(undefined);
    const change = {
      ...(parsed.paise !== t.amount_paise && { amount_paise: parsed.paise }),
      ...(date !== t.transaction_date && { transaction_date: date }),
    };
    if (Object.keys(change).length > 0) correct.mutate({ id: t.id, change });
  };

  return (
    <form className="txn-correct" aria-label="Correct this transaction" onSubmit={submit} noValidate>
      <div className="form-grid">
        <Field label="Amount" inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)}
          error={error} />
        <Field label="Date" type="date" value={date} max={todayIso()} onChange={(e) => setDate(e.target.value)} />
      </div>
      {correct.isError && <p className="form-error" role="alert">{describeError(correct.error)}</p>}
      <div className="actions">
        <button className="button button--primary" type="submit" disabled={correct.isPending}>Save correction</button>
      </div>
    </form>
  );
}
