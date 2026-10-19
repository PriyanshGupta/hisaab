// One transaction, looked at closely: where it came from (the email, a Gmail link, its kept text)
// and the answers that fix it one transaction at a time. Shared by the review queue's items and
// the Transactions page, so both views offer the same things (user, 2026-10-19).
import { useState } from "react";

import type { Account } from "../api/accounts";
import { type Category, useCreateCategory, usePayees } from "../api/categories";
import { describeError } from "../api/client";
import { type ReviewItem, useAcknowledge, useEmailText, useNamePayee } from "../api/review";
import { useCorrectTransaction } from "../api/transactions";
import { formatInstant } from "../dates";
import { formatPaise, parseRupees } from "../money";
import { AnswerBox, type Option } from "./AnswerBox";
import "../styles/review.css"; // item__* and answer__* live there

export type Email = NonNullable<ReviewItem["email"]>;
/** What the actions need to know about a transaction; a review item and a listed transaction both fit. */
export type Subject = {
  id: string; kind: string; amount_paise: number; transaction_date: string; payee_id: string | null;
  account_leg: "from" | "to"; who: string;
};
export type Action = "category" | "payee" | "account" | "transfer" | "correct" | "looks-right";
export const ALL_ACTIONS: readonly Action[] = ["category", "payee", "account", "transfer", "correct", "looks-right"];

const NEW_CATEGORY = "__new_category";
export const createCategory = (typed: string): Option => ({ id: NEW_CATEGORY, label: `Create category “${typed}”` });

/** A category pick, as an id: an existing category, or one created on the spot (like a new payee). */
export function useCategoryId() {
  const create = useCreateCategory();
  const pick = async (o: Option, typed: string): Promise<{ id: string; name: string }> =>
    o.id === NEW_CATEGORY ? { id: (await create.mutateAsync(typed)).id, name: typed } : { id: o.id, name: o.label };
  // a failed creation (e.g. the name is taken) is shown by the caller; the pick goes no further
  return Object.assign((o: Option, typed: string) => pick(o, typed).catch(() => new Promise<never>(() => {})),
    { error: create.error });
}

/** The one-transaction answers, in a fixed order; `actions` says which this view offers. */
export function TransactionActions({ subject, accounts, categories, actions }: {
  subject: Subject; accounts: Account[]; categories: Category[]; actions: readonly Action[];
}) {
  const correct = useCorrectTransaction();
  const acknowledge = useAcknowledge();
  const name = useNamePayee();
  const payees = usePayees();
  const categoryId = useCategoryId();
  const [action, setAction] = useState<Action | null>(null);
  const { who } = subject;
  const otherLeg = subject.account_leg === "from" ? "to_account_id" : "from_account_id";
  const ownLeg = subject.account_leg === "from" ? "from_account_id" : "to_account_id";
  const patch = (change: Record<string, unknown>) =>
    correct.mutate({ id: subject.id, change }, { onSuccess: () => setAction(null) });
  const accountOptions = accounts.map((a) => ({ id: a.id, label: a.display_name }));
  const offers = (a: Action) => actions.includes(a);

  return (
    <>
      <div className="item__actions">
        {offers("category") && (
          <button className="button button--quiet" type="button" onClick={() => setAction("category")}>Category for this one</button>
        )}
        {offers("payee") && subject.payee_id === null && (
          <button className="button button--quiet" type="button" onClick={() => setAction("payee")}>Payee for this one</button>
        )}
        {offers("account") && (
          <button className="button button--quiet" type="button" onClick={() => setAction("account")}>Account for this one</button>
        )}
        {offers("transfer") && subject.kind !== "transfer" && (
          <button className="button button--quiet" type="button" onClick={() => setAction("transfer")}>Mark as transfer</button>
        )}
        {offers("correct") && (
          <button className="button button--quiet" type="button" onClick={() => setAction("correct")}>Correct amount or date</button>
        )}
        {offers("looks-right") && (
          <button className="button button--quiet" type="button" disabled={acknowledge.isPending}
            onClick={() => acknowledge.mutate(subject.id)}>Looks right</button>
        )}
      </div>
      {action === "category" && <AnswerBox label={`Category for ${who}, this one only`} onClose={() => setAction(null)}
        options={categories.map((c) => ({ id: c.id, label: c.name }))} create={createCategory}
        onPick={(o, typed) => void categoryId(o, typed).then(({ id }) => patch({ category_override_id: id }))} />}
      {action === "payee" && <AnswerBox label={`Payee for ${who}, this one only`} onClose={() => setAction(null)}
        options={(payees.data ?? []).map((p) => ({ id: p.id, label: p.name }))}
        create={(typed) => ({ id: "__create", label: `Create “${typed}”` })}
        onPick={(o, typed) => name.mutate(o.id === "__create" ? { transaction_ids: [subject.id], name: typed }
          : { transaction_ids: [subject.id], payee_id: o.id }, { onSuccess: () => setAction(null) })} />}
      {action === "account" && <AnswerBox label={`Account for ${who}, this one only`} onClose={() => setAction(null)}
        options={accountOptions} onPick={(o) => patch({ [ownLeg]: o.id })} />}
      {action === "transfer" && <AnswerBox label={`The other account for ${who}`} onClose={() => setAction(null)}
        options={accountOptions} onPick={(o) => patch({ kind: "transfer", [otherLeg]: o.id })} />}
      {action === "correct" && <CorrectForm subject={subject} onSave={patch} onClose={() => setAction(null)} />}
      {(correct.isError || acknowledge.isError || name.isError || categoryId.error) && (
        <p className="form-error" role="alert">
          {describeError(correct.error ?? acknowledge.error ?? name.error ?? categoryId.error)}
        </p>
      )}
    </>
  );
}

function CorrectForm({ subject, onSave, onClose }: {
  subject: Subject; onSave: (change: Record<string, unknown>) => void; onClose: () => void;
}) {
  const [amount, setAmount] = useState((subject.amount_paise / 100).toFixed(2));
  const [date, setDate] = useState(subject.transaction_date);
  const [error, setError] = useState<string | null>(null);
  return (
    <form className="item__correct" aria-label={`Correct ${formatPaise(subject.amount_paise)}`} onSubmit={(e) => {
      e.preventDefault();
      const parsed = parseRupees(amount);
      if (!parsed.ok) { setError(parsed.error); return; }
      onSave({ amount_paise: parsed.paise, transaction_date: date });
    }}>
      <label className="field__label">Amount <input className="field__input" inputMode="decimal" value={amount}
        onChange={(e) => setAmount(e.target.value)} /></label>
      <label className="field__label">Date <input className="field__input" type="date" value={date}
        onChange={(e) => setDate(e.target.value)} /></label>
      <button className="button button--primary" type="submit">Save</button>
      <button className="button button--quiet" type="button" onClick={onClose}>Cancel</button>
      {error && <p className="form-error" role="alert">{error}</p>}
    </form>
  );
}

/** Where a transaction came from, for checking it: when and from whom, the email in Gmail, its text if kept. */
export function EmailLine({ id, email }: { id: string; email: Email }) {
  const [showing, setShowing] = useState(false);
  const text = useEmailText(id, showing);
  return (
    <div className="item__email">
      <p className="item__email-line">
        {`Email from ${email.sender ?? "an unknown sender"}, received ${formatInstant(email.received_at)}`}
        {email.subject ? `: ${email.subject}` : "."}
      </p>
      <div className="item__email-links">
        {email.gmail_url && <a href={email.gmail_url} target="_blank" rel="noopener noreferrer">Open in Gmail</a>}
        {email.has_text && (
          <button className="button button--quiet" type="button" aria-expanded={showing}
            onClick={() => setShowing((v) => !v)}>{showing ? "Hide the email" : "Show the email"}</button>
        )}
      </div>
      {showing && text.isPending && <p className="muted">Loading…</p>}
      {showing && text.isError && <p className="form-error" role="alert">{describeError(text.error)}</p>}
      {showing && text.data && <pre className="item__email-text">{text.data.text}</pre>}
    </div>
  );
}
