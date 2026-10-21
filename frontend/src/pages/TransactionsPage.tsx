// The transaction list (PRD §3.11): every entry with where it came from (the email, a Gmail link,
// its kept text, why it's in review), any corrections beside the source's value, and undo. The
// one-transaction answers are the review queue's own (review/TransactionTools): payee, account,
// mark as transfer, looks right; category and amount/date have the fuller forms below.
// Finding one: search (payee, bank text, amount, category), an account and a category filter, and the date
// shortcuts, with the count and the money in and out of what's shown. Search and filters run in the browser
// over the list the server sends; with no dates picked they ask it for everything, not just the latest 200.
import { type FormEvent, useState } from "react";
import { Link, useSearchParams } from "react-router";

import { type Account, useAccounts } from "../api/accounts";
import { type Category, useCategories, useSetPayeeCategory } from "../api/categories";
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
import { type Period, indiaToday, periodAt, periodTitle, shift } from "../report/period";
import { type Action, EmailLine, readable, TransactionActions } from "../review/TransactionTools";
import { Icon } from "../ui/Icon";
import { CustomRange } from "./ReportPage";
import "../styles/transactions.css";

const PROVENANCE = { manual: "your manual entry", email: "a bank email", statement: "a statement" } as const;
const FIELD_NAMES: Record<string, string> = {
  amount: "amount", transaction_date: "date", kind: "kind", from_account_id: "from account", to_account_id: "to account",
  payee_id: "payee",
};
const UNCATEGORIZED = "Not categorized yet";
const NONE = "__none"; // the category filter's "not categorized yet"
const PAGE = 100; // rows drawn at once; "Show more" adds the next hundred
const LATEST = 200; // what the server sends for "all dates" when nothing narrows it (api/transactions.py)
const EVERYTHING = "2000-01-01"; // a filter with no dates looks this far back (the server sends up to 1,000)

const ISO = /^\d{4}-\d{2}-\d{2}$/;

/** The date shortcuts, worked out from India's today (tech-design §8.3: month boundaries in Asia/Kolkata). */
function shortcuts(today: string): [string, Period][] {
  return [
    ["Today", periodAt("day", today)], ["Yesterday", shift(periodAt("day", today), -1)],
    ["This week", periodAt("week", today)], ["Last week", shift(periodAt("week", today), -1)],
    ["This month", periodAt("month", today)], ["Last month", shift(periodAt("month", today), -1)],
  ];
}

const plural = (n: number, one: string, many = `${one}s`) => `${n.toLocaleString("en-IN")} ${n === 1 ? one : many}`;

export function TransactionsPage() {
  const [params, setParams] = useSearchParams();
  const today = indiaToday();
  const from = params.get("from");
  const to = params.get("to");
  const range = from && to && ISO.test(from) && ISO.test(to) && from <= to ? { start: from, end: to } : null;
  const [query, setQuery] = useState("");
  const [accountId, setAccountId] = useState("");
  const [categoryId, setCategoryId] = useState("");
  const [shown, setShown] = useState(PAGE);
  const narrowed = query.trim() !== "" || accountId !== "" || categoryId !== "";
  const transactions = useTransactions(range ?? (narrowed ? { start: EVERYTHING, end: today } : null));
  const accounts = useAccounts();
  const categories = useCategories();
  const [open, setOpen] = useState<string | null>(null);
  const names = new Map((accounts.data ?? []).map((a) => [a.id, a.display_name]));
  const allCategories = categories.data ?? [];
  const categoryNames = new Map(allCategories.map((c) => [c.id, c.name]));

  const all = transactions.data ?? [];
  const list = all.filter((t) => matches(t, query, accountId, categoryId, names, categoryNames));
  const out = list.filter((t) => t.kind === "expense").reduce((n, t) => n + t.amount_paise, 0);
  const into = list.filter(isCredit).reduce((n, t) => n + t.amount_paise, 0);
  const latestOnly = !range && !narrowed && all.length >= LATEST;
  const setRange = (r: { start: string; end: string } | null) => {
    setShown(PAGE);
    setParams(r ? { from: r.start, to: r.end } : {}, { replace: true });
  };

  return (
    <>
      <header className="page-head">
        <h1 className="page-title">Transactions</h1>
        <div className="page-head__actions">
          <Link className="button button--secondary" to="/statements">
            <Icon name="upload" size={16} />
            Upload a statement
          </Link>
          <Link className="button button--primary" to="/add">
            <Icon name="add" size={16} />
            Add
          </Link>
        </div>
      </header>

      <div className="finder">
        <div className="finder__fields">
          <label className="finder__search">
            <span className="visually-hidden">Search</span>
            <Icon name="search" size={18} className="finder__search-icon" />
            <input className="field__input" type="search" value={query} placeholder="Search payee, amount or category"
              onChange={(e) => { setQuery(e.target.value); setShown(PAGE); }} />
          </label>
          <label className="finder__pick">
            <span className="visually-hidden">Account</span>
            <select className="field__input" value={accountId} onChange={(e) => { setAccountId(e.target.value); setShown(PAGE); }}>
              <option value="">All accounts</option>
              {(accounts.data ?? []).map((a) => <option key={a.id} value={a.id}>{a.display_name}</option>)}
            </select>
          </label>
          <label className="finder__pick">
            <span className="visually-hidden">Category filter</span>
            <select className="field__input" value={categoryId} onChange={(e) => { setCategoryId(e.target.value); setShown(PAGE); }}>
              <option value="">All categories</option>
              <option value={NONE}>{UNCATEGORIZED}</option>
              {allCategories.filter((c) => c.archived_at === null).map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
            </select>
          </label>
        </div>
        <DateFilter range={range} today={today} onRange={setRange} />
      </div>

      {transactions.isPending && <p className="loading">Loading…</p>}
      {transactions.isError && <p className="form-error" role="alert">{describeError(transactions.error)}</p>}
      {transactions.isSuccess && all.length === 0 && (range ? (
        <section className="empty">
          <p className="empty__lead">Nothing on these dates.</p>
          <p className="empty__body">Try another range, or All dates.</p>
        </section>
      ) : narrowed ? null : (
        <section className="empty">
          <p className="empty__lead">No transactions yet.</p>
          <p className="empty__body">Bank alerts will appear here once Gmail is connected. Cash goes in through Add.</p>
        </section>
      ))}
      {transactions.isSuccess && (all.length > 0 || narrowed) && (
        <>
          <p className="finder__summary" aria-live="polite">
            <strong>{plural(list.length, "transaction")}</strong>
            {list.length > 0 && <span>{formatPaise(out)} spent</span>}
            {into > 0 && <span>{formatPaise(into)} in</span>}
            {range && <span className="muted">{periodTitle({ unit: "custom", ...range })}</span>}
          </p>
          {list.length === 0 ? (
            <section className="empty">
              <p className="empty__lead">Nothing matches.</p>
              <p className="empty__body">Try fewer words, another account or category, or All dates.</p>
            </section>
          ) : (
            <div className="sheet txn-sheet">
              <table className="ledger txn-table">
                <thead>
                  <tr>
                    <th scope="col" className="txn-table__date">Date</th>
                    <th scope="col">Details</th>
                    <th scope="col" className="txn-table__account">Account</th>
                    <th scope="col" className="amount">Amount</th>
                  </tr>
                </thead>
                <tbody>
                  {list.slice(0, shown).map((t) => (
                    <Row key={t.id} t={t} names={names} accounts={accounts.data ?? []} categories={allCategories}
                      open={open === t.id} onToggle={() => setOpen(open === t.id ? null : t.id)} />
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {(list.length > shown || latestOnly) && (
            <div className="finder__more">
              {list.length > shown && (
                <button className="button button--secondary" type="button" onClick={() => setShown(shown + PAGE)}>
                  Show {Math.min(PAGE, list.length - shown)} more
                </button>
              )}
              {latestOnly && list.length <= shown && (
                <p className="muted">These are the latest {LATEST}. Search, filter or pick dates to look further back.</p>
              )}
            </div>
          )}
        </>
      )}
    </>
  );
}

/** Whether a transaction is in what the person asked for: every word of the search, and the two filters. */
function matches(t: Transaction, query: string, accountId: string, categoryId: string, names: Map<string, string>,
  categoryNames: Map<string, string>): boolean {
  if (accountId && t.from_account_id !== accountId && t.to_account_id !== accountId) return false;
  if (categoryId === NONE ? t.kind === "transfer" || t.category_id !== null
    : categoryId && t.category_id !== categoryId) return false;
  const words = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (words.length === 0) return true;
  const rupees = (t.amount_paise / 100).toFixed(2);
  const haystack = [t.payee_name, t.payee_raw, t.kind, rupees, formatPaise(t.amount_paise),
    t.category_id ? categoryNames.get(t.category_id) : UNCATEGORIZED,
    t.from_account_id ? names.get(t.from_account_id) : null, t.to_account_id ? names.get(t.to_account_id) : null,
  ].filter(Boolean).join(" ").toLowerCase();
  return words.every((w) => haystack.includes(w.replace(/^₹/, "")));
}

/** Which dates the list shows: everything, a shortcut, or any range picked by hand. */
function DateFilter({ range, today, onRange }: {
  range: { start: string; end: string } | null; today: string; onRange: (r: { start: string; end: string } | null) => void;
}) {
  const options = shortcuts(today);
  const current = options.find(([, p]) => range && p.start === range.start && p.end === range.end)?.[0];
  const [picking, setPicking] = useState(range !== null && current === undefined);
  const picked = picking || (range !== null && current === undefined);
  // picking dates starts from what's on screen (or today), so the list always matches the dates shown
  const pick = () => { setPicking(true); onRange(range ?? { start: today, end: today }); };
  return (
    <div className="txn-filter">
      <div className="chips" role="group" aria-label="Dates">
        <button className="chip" type="button" aria-pressed={range === null && !picking}
          onClick={() => { setPicking(false); onRange(null); }}>All dates</button>
        {options.map(([label, p]) => (
          <button key={label} className="chip" type="button" aria-pressed={current === label && !picking}
            onClick={() => { setPicking(false); onRange({ start: p.start, end: p.end }); }}>{label}</button>
        ))}
        <button className="chip" type="button" aria-pressed={picked} onClick={pick}>
          <Icon name="chevron-down" size={14} />
          Pick dates
        </button>
      </div>
      {picked && (
        <CustomRange key={range ? `${range.start}/${range.end}` : "none"} today={today}
          period={{ unit: "custom", start: range?.start ?? today, end: range?.end ?? today }}
          onRange={(p) => onRange({ start: p.start, end: p.end })} />
      )}
    </div>
  );
}

function accountText(t: Transaction, names: Map<string, string>): string {
  const name = (id: string | null) => (id ? names.get(id) ?? "Unknown account" : null);
  if (t.kind === "transfer") return `${name(t.from_account_id)} to ${name(t.to_account_id)}`;
  return name(t.from_account_id) ?? name(t.to_account_id) ?? "Not yet matched";
}

/** "5 Oct" this year, "5 Oct 2025" otherwise: the phone's date, under the name. */
const shortDate = (iso: string) => (iso.slice(0, 4) === todayIso().slice(0, 4) ? formatDate(iso).slice(0, -5) : formatDate(iso));

function Row({ t, names, accounts, categories, open, onToggle }: {
  t: Transaction; names: Map<string, string>; accounts: Account[]; categories: Category[]; open: boolean;
  onToggle: () => void;
}) {
  const label = t.payee_name ?? t.payee_raw ?? (t.kind === "transfer" ? "Transfer" : t.kind[0]!.toUpperCase() + t.kind.slice(1));
  const categoryName = categories.find((c) => c.id === t.category_id)?.name;
  return (
    <>
      {/* the whole row opens it (a phone's thumb lands anywhere); the name's button is the keyboard's way in,
          and its click bubbles up to the row's handler */}
      <tr className={open ? "txn-row txn-row--open" : "txn-row"} onClick={onToggle}>
        <td className="txn-table__date">{formatDate(t.transaction_date)}</td>
        <td className="txn-row__details">
          <button className="txn-row__toggle" type="button" aria-expanded={open}>
            {label}
          </button>
          <span className="txn-row__sub">
            {t.kind === "transfer" ? <span className="txn-row__kind">Transfer</span> : (
              <span className={categoryName ? "txn-row__category" : "txn-row__category txn-row__category--none"}>
                {categoryName ?? UNCATEGORIZED}
              </span>
            )}
            {t.status === "needs_review" && <span className="txn-row__flag">In review</span>}
            {t.edits.length > 0 && <span className="txn-row__edited">edited</span>}
            <span className="txn-row__phone">{shortDate(t.transaction_date)}, {accountText(t, names)}</span>
          </span>
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
            <Detail t={t} label={label} names={names} accounts={accounts} categories={categories} onClose={onToggle} />
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

function Detail({ t, label, names, accounts, categories, onClose }: {
  t: Transaction; label: string; names: Map<string, string>; accounts: Account[]; categories: Category[];
  onClose: () => void;
}) {
  const undo = useUndoCorrection();
  const remove = useDeleteTransaction();
  const [confirmDelete, setConfirmDelete] = useState(false);
  const edits = t.edits.filter((e) => e.field !== "category_override_id"); // the category section shows that one
  const actions: Action[] = ["payee", "account", "transfer", ...(t.status === "needs_review" ? ["looks-right" as const] : [])];

  return (
    <div className="txn-detail__body">
      <div className="txn-detail__head">
        <div className="txn-detail__source">
          {t.email ? <EmailLine id={t.id} email={t.email} /> : <p className="muted">From {PROVENANCE[t.provenance]}.</p>}
          {t.reason && <p className="item__reason">{readable(t.reason)}</p>}
        </div>
        <button className="button button--secondary button--small txn-detail__close" type="button" onClick={onClose}>
          <Icon name="close" size={14} />
          <span className="txn-detail__close-label">Close</span>
        </button>
      </div>
      <TransactionActions subject={{ ...t, who: label }} accounts={accounts.filter((a) => a.archived_at === null)}
        categories={categories} actions={actions} />

      <div className="txn-detail__forms">
        {t.kind !== "transfer" && <CategorySection t={t} categories={categories} />}
        <CorrectionForm key={`${t.amount_paise}|${t.transaction_date}`} t={t} />
      </div>

      {edits.length > 0 && (
        <ul className="txn-edits">
          {edits.map((e) => (
            <li key={e.field}>
              <span>
                {e.field === "amount" ? "Edited." : `${FIELD_NAMES[e.field] ?? e.field} edited.`.replace(/^./, (c) => c.toUpperCase())}{" "}
                The entry said {describeSourceValue(e.field, e.prior_value, names)}.
              </span>
              <button className="button button--quiet button--small" type="button" disabled={undo.isPending}
                aria-label={`Undo ${FIELD_NAMES[e.field] ?? e.field}`}
                onClick={() => undo.mutate({ id: t.id, fields: [e.field as EditableField] })}>
                Undo
              </button>
            </li>
          ))}
        </ul>
      )}
      {undo.isError && <p className="form-error" role="alert">{describeError(undo.error)}</p>}

      {t.provenance === "manual" && (
        confirmDelete ? (
          <div className="confirm" role="group" aria-label="Delete this entry?">
            <p>
              Delete this entry? It can't be undone.
              {t.edits.length > 0 && " Its corrections will be lost too."}
            </p>
            {remove.isError && <p className="form-error" role="alert">{describeError(remove.error)}</p>}
            <div className="actions">
              <button className="button button--danger" type="button" disabled={remove.isPending}
                onClick={() => remove.mutate(t.id)}>Delete it</button>
              <button className="button button--secondary" type="button" onClick={() => setConfirmDelete(false)}>
                Keep it
              </button>
            </div>
          </div>
        ) : (
          <div className="actions txn-detail__delete">
            <button className="button button--quiet button--small txn-detail__delete-button" type="button"
              onClick={() => setConfirmDelete(true)}>
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
    <form className="txn-panel txn-correct" aria-label="Correct this transaction" onSubmit={submit} noValidate>
      <h2 className="txn-panel__title">Amount and date</h2>
      <div className="txn-correct__fields">
        <Field label="Amount" inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)}
          error={error} />
        <Field label="Date" type="date" value={date} max={todayIso()} onChange={(e) => setDate(e.target.value)} />
      </div>
      {correct.isError && <p className="form-error" role="alert">{describeError(correct.error)}</p>}
      <div className="actions">
        <button className="button button--secondary" type="submit" disabled={correct.isPending}>Save correction</button>
      </div>
    </form>
  );
}

/**
 * Category for one transaction: which mechanism decides it (PRD §3.11), and the
 * two ways to change it. "Use for all <payee>" writes one rule for the payee;
 * "Use for this one" writes an override on this transaction only, which later
 * rule changes leave alone.
 */
function CategorySection({ t, categories }: { t: Transaction; categories: Category[] }) {
  const correct = useCorrectTransaction();
  const setRule = useSetPayeeCategory();
  const undo = useUndoCorrection();
  const [pick, setPick] = useState(t.category_id ?? "");
  const [saved, setSaved] = useState<string | null>(null); // the confirmation: a click must show that it took
  const picked = categories.find((c) => c.id === pick)?.name ?? "";
  const active = categories.filter((c) => c.archived_at === null);
  const name = categories.find((c) => c.id === t.category_id)?.name;
  const payee = t.payee_name;

  let status: string;
  if (t.category_source === "override") {
    status = name ? `${name}, set for this transaction only.` : "Not categorized: the category set for this transaction is archived.";
  } else if (t.category_source === "rule") {
    status = name ? `${name}, from your rule for ${payee}.` : `Not categorized: the category your rule for ${payee} uses is archived.`;
  } else {
    status = `${UNCATEGORIZED}.`;
  }
  const failed = correct.error ?? setRule.error ?? undo.error;

  return (
    <div className="txn-panel txn-category">
      <h2 className="txn-panel__title">Category</h2>
      <p className="txn-category__status">
        <span>{status}</span>
        {t.category_source === "override" && (
          <button className="button button--quiet button--small" type="button" aria-label="Undo category" disabled={undo.isPending}
            onClick={() => undo.mutate({ id: t.id, fields: ["category_override_id"] })}>
            Undo
          </button>
        )}
      </p>
      <div className="field">
        <label className="visually-hidden" htmlFor={`category-${t.id}`}>Category</label>
        <select id={`category-${t.id}`} className="field__input" value={pick}
          onChange={(e) => { setPick(e.target.value); setSaved(null); }}>
          <option value="">Choose a category</option>
          {active.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
        </select>
      </div>
      <div className="actions">
        {t.payee_id && payee && (
          <button className="button button--primary" type="button" disabled={!pick || setRule.isPending}
            onClick={() => setRule.mutate({ payeeId: t.payee_id!, categoryId: pick },
              { onSuccess: () => setSaved(`Saved: every ${payee} payment is now ${picked}.`) })}>
            Use for all {payee}
          </button>
        )}
        <button className="button button--secondary" type="button" disabled={!pick || correct.isPending}
          onClick={() => correct.mutate({ id: t.id, change: { category_override_id: pick } },
            { onSuccess: () => setSaved(`Saved: this payment is now ${picked}.`) })}>
          Use for this one
        </button>
      </div>
      {saved && <p className="txn-category__saved" role="status">{saved}</p>}
      {failed && <p className="form-error" role="alert">{describeError(failed)}</p>}
    </div>
  );
}
