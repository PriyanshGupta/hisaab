// Bulk categorization (PRD §6.1 step 5, ruling 2026-10-11): where first run
// lands after the backfill. Uncategorized transactions grouped by recognised
// payee, most first; one action files a whole group by writing ONE payee rule,
// so later transactions from that payee arrive categorized too. Payees Hisaab
// doesn't recognise are counted; naming them is the review queue's (slice 17).
import { useState } from "react";
import { Link } from "react-router";

import { type Category, useCategories, useSetPayeeCategory } from "../api/categories";
import { describeError } from "../api/client";
import { useUncategorized } from "../api/setup";
import "../styles/setup.css";

const plural = (n: number) => `${n} ${n === 1 ? "transaction" : "transactions"}`;

export function CategorizePage() {
  const groups = useUncategorized();
  const categories = useCategories();
  const [done, setDone] = useState<string | null>(null);
  const active = (categories.data ?? []).filter((c) => c.archived_at === null);

  return (
    <>
      <header className="page-head"><h1 className="page-title">Categorize your spending</h1></header>
      {done && <p className="settings-note" role="status">{done}</p>}
      {(groups.isPending || categories.isPending) && <p className="muted">Loading…</p>}
      {groups.isError && <p className="form-error" role="alert">{describeError(groups.error)}</p>}
      {groups.data && categories.data && (
        <>
          {groups.data.groups.length > 0 ? (
            <ul className="payee-groups">
              {groups.data.groups.map((g) => (
                <PayeeGroup key={g.payee_id} payeeId={g.payee_id} name={g.payee_name} count={g.count}
                  categories={active} onFiled={setDone} />
              ))}
            </ul>
          ) : (
            <p className="empty__lead">
              Everything recognised is categorized. <Link to="/">Go to the report</Link>
            </p>
          )}
          {groups.data.unnamed > 0 && (
            <p className="muted">
              {groups.data.unnamed} more have a payee Hisaab doesn't recognise yet; naming them comes with the review
              queue.
            </p>
          )}
        </>
      )}
    </>
  );
}

function PayeeGroup({ payeeId, name, count, categories, onFiled }: {
  payeeId: string; name: string; count: number; categories: Category[]; onFiled: (message: string) => void;
}) {
  const file = useSetPayeeCategory();
  const [categoryId, setCategoryId] = useState("");
  const chosen = categories.find((c) => c.id === categoryId);

  return (
    <li className="payee-group" aria-label={name}>
      <div className="payee-group__who">
        <span className="payee-group__name">{name}</span>
        <span className="muted">{plural(count)}</span>
      </div>
      <select className="field__input" aria-label={`Category for ${name}`} value={categoryId}
        onChange={(e) => setCategoryId(e.target.value)}>
        <option value="">Choose a category</option>
        {categories.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
      </select>
      <button className="button button--primary" type="button" disabled={!chosen || file.isPending}
        onClick={() => file.mutate({ payeeId, categoryId },
          { onSuccess: () => onFiled(`${plural(count)} filed under ${chosen!.name}.`) })}>
        Categorize all {count}
      </button>
      {file.isError && <p className="form-error" role="alert">{describeError(file.error)}</p>}
    </li>
  );
}
