// The review queue (PRD §5.5), built for several hundred items after a backfill.
//
// Four lanes, by what the answer is:
//   Accounts    grouped by identifier: "ending 4821 is my HDFC savings" resolves
//               every past and future transaction carrying it
//   Categories  grouped by payee: one rule, plus auto-categorize. A payee not yet
//               named is categorized under its text, which becomes its name
//   Payees      grouped by raw text: one name for all of them. Optional, done as
//               the app is used, so never marked as work left (user, 2026-10-19)
//   Checks      one at a time: unread fields, large amounts, transfer questions,
//               each with its email: when, from whom, a Gmail link, the text
// A transaction sits in every lane whose question it raises. Answered groups
// move to a Sorted list below, built from saved data, so they are there after a
// reload and stay changeable. Groups come biggest total first; "Sort by" also offers most transactions or
// A to Z (user, 2026-10-21), kept in the address so it survives a reload and a lane switch. Keyboard first: ↑/↓ (or j/k) move, Enter answers, Space shows
// the group's transactions, 1–4 switch lanes. After an answer the cursor stays
// put, so the next group slides under it.
import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router";

import { type Account, useAccounts } from "../api/accounts";
import { type Category, useCategories, usePayees, useRenamePayee } from "../api/categories";
import { describeError } from "../api/client";
import {
  type ReviewItem,
  useAssignAccount,
  useAssignIdentifier,
  useCategorizePayee,
  useNamePayee,
  useNotMyAccount,
  useReview,
} from "../api/review";
import { useMe } from "../api/session";
import { useCorrectTransaction } from "../api/transactions";
import { formatDate } from "../dates";
import { formatPaise } from "../money";
import { AddAccountForm } from "./AccountsPage";
import { AnswerBox, type Option } from "../review/AnswerBox";
import { AutoCategorize } from "../review/AutoCategorize";
import { ALL_ACTIONS, createCategory, EmailLine, readable, TransactionActions, useCategoryId } from "../review/TransactionTools";
import { countLeft, inLane, LANES, type LaneId, OPTIONAL } from "../review/lanes";
import "../styles/review.css";

type Group = {
  key: string; label: string; name: string; items: ReviewItem[]; answerable: boolean;
  answer?: string; auto?: boolean; payeeId?: string; // once sorted: what it was answered with
};

type SortKey = "amount" | "count" | "name";
const SORTS: { key: SortKey; label: string }[] = [
  { key: "amount", label: "Amount" }, { key: "count", label: "Transactions" }, { key: "name", label: "Name" }];

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;
const totalOf = (g: Group) => g.items.reduce((sum, i) => sum + i.amount_paise, 0);
const byName = (a: Group, b: Group) => a.label.localeCompare(b.label, undefined, { sensitivity: "base", numeric: true });

/** Biggest total first, most transactions first, or A to Z; ties fall back to the other measure, then the name. */
function ordered(groups: Group[], sort: SortKey): Group[] {
  const rank = sort === "amount" ? (a: Group, b: Group) => totalOf(b) - totalOf(a) || b.items.length - a.items.length
    : sort === "count" ? (a: Group, b: Group) => b.items.length - a.items.length || totalOf(b) - totalOf(a)
    : () => 0;
  return [...groups].sort((a, b) => rank(a, b) || byName(a, b));
}
const capital = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

function groupsFor(lane: LaneId, items: ReviewItem[]): Group[] {
  const mine = items.filter((i) => inLane(lane, i));
  const single = (i: ReviewItem): Group => ({ key: i.id, label: i.payee_name ?? i.payee_raw ?? "Unknown payee",
    name: i.payee_name ?? i.payee_raw ?? "this transaction", items: [i], answerable: false });
  if (lane === "checks") return mine.map(single);
  const by = new Map<string, Group>();
  for (const i of mine) {
    // key "" is the group the email named nothing for: "Unknown account" / "Unknown payee", answered by edit.
    // In Categories a payee not yet named is keyed "raw:<text>": categorizing it names it after its text.
    const [key, name] =
      lane === "accounts" ? [i.identifier ?? "", i.identifier ? (i.account_label ?? i.identifier) : "unknown account"]
      : lane === "payees" ? [i.payee_raw ?? "", i.payee_raw ?? "unknown payee"]
      : i.payee_id ? [i.payee_id, i.payee_name ?? ""]
      : i.payee_raw ? [`raw:${i.payee_raw}`, i.payee_raw] : ["", "unknown payee"];
    const raw = key !== "" && (lane === "payees" || key.startsWith("raw:"));
    const g = by.get(key) ?? { key, label: raw ? name : capital(name), name, items: [], answerable: true };
    g.items.push(i);
    by.set(key, g);
  }
  return [...by.values()];
}

export function ReviewPage() {
  const review = useReview();
  const accounts = useAccounts();
  const categories = useCategories();
  const [params, setParams] = useSearchParams();
  const lane: LaneId = (LANES.find((l) => l.id === params.get("lane"))?.id) ?? "accounts";
  const sort: SortKey = SORTS.find((o) => o.key === params.get("sort"))?.key ?? "amount";
  const [status, setStatus] = useState<string | null>(null);

  const items = review.data?.items ?? [];
  const left = countLeft(items);
  const active: Account[] = (accounts.data ?? []).filter((a) => a.archived_at === null);
  const cats: Category[] = (categories.data ?? []).filter((c) => c.archived_at === null);
  const go = (id: LaneId, by: SortKey) => setParams({ lane: id, ...(by !== "amount" && { sort: by }) }, { replace: true });
  const choose = (id: LaneId) => { go(id, sort); setStatus(null); };

  return (
    <div className="review">
      <header className="page-head review__head">
        <h1 className="page-title">Review</h1>
        {review.data && <p className="review__tally"><span className="review__tally-n">{left}</span> left</p>}
      </header>
      <div className="tabs lanes" role="tablist" aria-label="What needs an answer">
        {LANES.map((l, n) => {
          const count = items.filter((i) => inLane(l.id, i)).length;
          return (
            <button key={l.id} type="button" role="tab" id={`lane-${l.id}`} aria-selected={lane === l.id}
              aria-controls="lane-panel" className={`tab lane${count > 0 && l.id !== OPTIONAL ? " lane--open" : ""}`}
              aria-keyshortcuts={String(n + 1)} onClick={() => choose(l.id)}>
              <span className="lane__name">{l.name}</span> <span className="badge lane__count">{count}</span>
            </button>
          );
        })}
      </div>
      <div id="lane-panel" role="tabpanel" aria-labelledby={`lane-${lane}`}>
        {status && <p className="settings-note review__status" role="status">{status}</p>}
        {review.isPending && <p className="loading">Loading…</p>}
        {review.isError && <p className="form-error" role="alert">{describeError(review.error)}</p>}
        {lane === "categories" && review.data && <AutoCategorize />}
        {review.data && (() => {
          const grouped: Group[] = groupsFor(lane, items);
          const groups: Group[] = lane === "checks" ? grouped : ordered(grouped, sort); // checks: one at a time, as listed
          const r = review.data;
          const sorted: Group[] = lane === "categories"
            ? r.categorized.map((c) => ({ key: c.payee_id, label: c.payee_name, name: c.payee_name, items: c.items,
                answerable: true, answer: cats.find((k) => k.id === c.category_id)?.name ?? "", auto: c.origin === "llm" }))
            : lane === "accounts"
            ? r.sorted_accounts.map((a) => ({ key: a.key, label: a.label, name: a.label, items: a.items, answerable: false,
                ...(a.answer !== null && { answer: a.answer }) }))
            : lane === "payees"
            ? r.sorted_payees.map((p) => ({ key: p.key, label: p.label, name: p.label, items: p.items, answerable: false,
                answer: p.payee_name, payeeId: p.payee_id }))
            : [];
          return (
            <>
              {lane !== "checks" && groups.length + sorted.length > 1 && (
                <div className="review__sort">
                  <span className="review__sort-label" id="review-sort">Sort by</span>
                  <div className="seg" role="group" aria-labelledby="review-sort">
                    {SORTS.map((o) => (
                      <button key={o.key} type="button" aria-pressed={sort === o.key} onClick={() => go(lane, o.key)}>
                        {o.label}
                      </button>
                    ))}
                  </div>
                </div>
              )}
              <Lane key={lane} lane={lane} order={sort} groups={groups} sorted={ordered(sorted, sort)} accounts={active}
                categories={cats} onDone={setStatus} onLane={(n) => LANES[n] && choose(LANES[n].id)} />
            </>
          );
        })()}
      </div>
      <p className="review__keys">
        Keys: <kbd>↑</kbd> and <kbd>↓</kbd> move, <kbd>Enter</kbd> answers, <kbd>Space</kbd> shows the transactions,{" "}
        <kbd>1</kbd> to <kbd>4</kbd> switch lanes.
      </p>
    </div>
  );
}

function Lane({ lane, order, groups, sorted, accounts, categories, onDone, onLane }: {
  lane: LaneId; order: SortKey; groups: Group[]; sorted: Group[]; accounts: Account[]; categories: Category[];
  onDone: (message: string) => void; onLane: (index: number) => void;
}) {
  const [cursor, setCursor] = useState(0);
  const [answering, setAnswering] = useState<string | null>(null);
  const [open, setOpen] = useState<Set<string>>(() => new Set(groups.filter((g) => !g.answerable).map((g) => g.key)));
  const list = useRef<HTMLUListElement>(null);
  const rowRefs = useRef<(HTMLLIElement | null)[]>([]);
  const at = Math.min(cursor, Math.max(groups.length - 1, 0));

  const moved = useRef(false); // the cursor has moved since arriving: only then follow it with the scroll
  useEffect(() => { if (cursor !== at) setCursor(at); }, [cursor, at]);
  useEffect(() => { setCursor(0); }, [order]); // a new order: the cursor starts again at the top
  // arriving, or switching lanes (the lane remounts): keys work at once, and the page stays where it is
  useEffect(() => { list.current?.focus({ preventScroll: true }); }, []);
  useEffect(() => {
    if (moved.current) rowRefs.current[at]?.scrollIntoView?.({ block: "nearest" });
    moved.current = true;
  }, [at]);

  const toggle = (key: string) => setOpen((s) => { const n = new Set(s); n.delete(key) || n.add(key); return n; });
  const finished = (message: string) => { setAnswering(null); onDone(message); list.current?.focus(); };
  const sortedList = sorted.length > 0 && (
    <>
      <h2 className="groups__heading">Sorted</h2>
      <ul className="groups groups--sorted" aria-label="Sorted">
        {sorted.map((g) => (
          <SortedRow key={g.key} lane={lane} group={g} expanded={open.has(`sorted:${g.key}`)} accounts={accounts}
            categories={categories} onToggle={() => toggle(`sorted:${g.key}`)} onDone={onDone} />
        ))}
      </ul>
    </>
  );

  if (groups.length === 0) {
    return <>{<p className="empty__lead review__empty">Nothing to sort here.</p>}{sortedList}</>;
  }

  const onKeyDown = (e: React.KeyboardEvent<HTMLUListElement>) => {
    if (e.target !== e.currentTarget) return; // keys inside a row's own controls are theirs
    const g = groups[at];
    if (e.key === "ArrowDown" || e.key === "j") { e.preventDefault(); setCursor(Math.min(at + 1, groups.length - 1)); }
    else if (e.key === "ArrowUp" || e.key === "k") { e.preventDefault(); setCursor(Math.max(at - 1, 0)); }
    else if (e.key === "Enter" && g?.answerable) { e.preventDefault(); setAnswering(g.key); }
    else if (e.key === " " && g) { e.preventDefault(); toggle(g.key); }
    else if (/^[1-4]$/.test(e.key)) { e.preventDefault(); onLane(Number(e.key) - 1); }
  };

  return (
    <>
    <ul ref={list} className="groups" aria-label={`${plural(groups.length, "group")} to sort`} tabIndex={0}
      onKeyDown={onKeyDown}>
      {groups.map((g, i) => (
        <GroupRow key={g.key} ref={(el) => { rowRefs.current[i] = el; }} lane={lane} group={g} current={i === at} answering={answering === g.key}
          expanded={open.has(g.key)} accounts={accounts} categories={categories}
          onFocusRow={() => setCursor(i)} onAnswer={() => setAnswering(g.key)} onToggle={() => toggle(g.key)}
          onCancel={() => { setAnswering(null); list.current?.focus(); }} onDone={finished} />
      ))}
    </ul>
    {sortedList}
    </>
  );
}

/** An answered group, kept on screen at the bottom: what it was answered with, and a way to change it. */
function SortedRow({ lane, group, expanded, accounts, categories, onToggle, onDone }: {
  lane: LaneId; group: Group; expanded: boolean; accounts: Account[]; categories: Category[]; onToggle: () => void;
  onDone: (message: string) => void;
}) {
  const categorize = useCategorizePayee();
  const categoryId = useCategoryId();
  const rename = useRenamePayee();
  const [changing, setChanging] = useState(false);
  const [name, setName] = useState(group.answer ?? "");
  const total = group.items.reduce((sum, i) => sum + i.amount_paise, 0);
  const answer = lane === "accounts" ? (group.answer ? `on ${group.answer}` : "kept as expenses, not an account")
    : lane === "payees" ? `named ${group.answer}` : group.answer;
  const canChange = lane === "categories" || (lane === "payees" && group.payeeId !== undefined);
  return (
    <li className="group group--sorted">
      <div className="group__line">
        <span className="group__who">
          <span className="group__label">{group.label}</span>
          <span className="group__meta">
            <span className="group__sorted-answer">{answer}</span>
            {group.auto && <span className="group__auto">auto-categorized</span>}
            <span className="group__count">{plural(group.items.length, "transaction")}</span>
          </span>
        </span>
        <span className="amount group__total">{formatPaise(total)}</span>
        <span className="group__actions">
          {canChange && !changing && (
            <button className="button button--quiet button--small" type="button" onClick={() => setChanging(true)}>
              {lane === "categories" ? "Change category" : "Rename"}
            </button>
          )}
          <button className="button button--quiet button--small" type="button" aria-expanded={expanded}
            aria-label={`${expanded ? "Hide" : "Show"} the transactions in ${group.name}`} onClick={onToggle}>
            {expanded ? "Hide" : "Show"}
          </button>
        </span>
      </div>
      {changing && lane === "categories" && (
        <div className="group__answer">
          <AnswerBox label={`Category for ${group.name}`} onClose={() => setChanging(false)}
            options={categories.map((c) => ({ id: c.id, label: c.name }))} create={createCategory}
            onPick={(o, typed) => void categoryId(o, typed).then((c) => categorize.mutate(
              { payee_id: group.key, category_id: c.id }, { onSuccess: (r) => {
                setChanging(false);
                onDone(`${group.label}: ${plural(r.updated, "transaction")} now under ${c.name}.`);
              } }))} />
          {(categorize.isError || categoryId.error) && (
            <p className="form-error" role="alert">{describeError(categorize.error ?? categoryId.error)}</p>
          )}
        </div>
      )}
      {changing && lane === "payees" && group.payeeId !== undefined && (
        <form className="group__answer group__rename" onSubmit={(e) => {
          e.preventDefault();
          if (!name.trim() || group.payeeId === undefined) return;
          rename.mutate({ payeeId: group.payeeId, name: name.trim() }, { onSuccess: () => {
            setChanging(false);
            onDone(`Renamed to ${name.trim()}.`);
          } });
        }}>
          <input className="field__input" aria-label={`New name for ${group.answer}`} autoFocus value={name}
            onChange={(e) => setName(e.target.value)} onKeyDown={(e) => { if (e.key === "Escape") setChanging(false); }} />
          <button className="button button--primary" type="submit" disabled={rename.isPending}>Save</button>
          <button className="button button--secondary" type="button" onClick={() => setChanging(false)}>Cancel</button>
          {rename.isError && <p className="form-error" role="alert">{describeError(rename.error)}</p>}
        </form>
      )}
      {expanded && (
        <ul className="items" aria-label={`Transactions in ${group.name}`}>
          {group.items.map((i) => <ItemLine key={i.id} item={i} accounts={accounts} categories={categories} />)}
        </ul>
      )}
    </li>
  );
}

function GroupRow({ ref, lane, group, current, answering, expanded, accounts, categories, onFocusRow, onAnswer, onToggle,
  onCancel, onDone }: {
  ref: React.Ref<HTMLLIElement>; lane: LaneId; group: Group; current: boolean; answering: boolean; expanded: boolean; accounts: Account[];
  categories: Category[]; onFocusRow: () => void; onAnswer: () => void; onToggle: () => void; onCancel: () => void;
  onDone: (message: string) => void;
}) {
  const total = totalOf(group);
  const ask = lane === "accounts" ? "Which account?" : lane === "payees" ? "Name it" : "Choose a category";
  return (
    <li ref={ref} className={`group${current ? " group--current" : ""}`} aria-current={current ? "true" : undefined}
      onClick={onFocusRow}>
      <div className="group__line">
        <span className="group__who">
          <span className="group__label">{group.label}</span>
          <span className="group__meta">
            <span className="group__count">{plural(group.items.length, "transaction")}</span>
          </span>
        </span>
        <span className="amount group__total">{formatPaise(total)}</span>
        <span className="group__actions">
          {group.answerable && lane !== "checks" && !answering && (
            // one main button on the screen: the row the cursor is on
            <button className={`button button--small ${current ? "button--primary" : "button--quiet"}`} type="button"
              onClick={onAnswer}>{ask}</button>
          )}
          {group.answerable && (
            <button className="button button--quiet button--small" type="button" aria-expanded={expanded}
              aria-label={`${expanded ? "Hide" : "Show"} the transactions in ${group.name}`} onClick={onToggle}>
              {expanded ? "Hide" : "Show"}
            </button>
          )}
        </span>
      </div>
      {answering && <GroupAnswer lane={lane} group={group} accounts={accounts} categories={categories}
        onCancel={onCancel} onDone={onDone} />}
      {expanded && (
        <ul className="items" aria-label={`Transactions in ${group.name}`}>
          {group.items.map((i) => <ItemLine key={i.id} item={i} accounts={accounts} categories={categories}
            single={lane === "checks"} />)}
        </ul>
      )}
    </li>
  );
}

function GroupAnswer({ lane, group, accounts, categories, onCancel, onDone }: {
  lane: LaneId; group: Group; accounts: Account[]; categories: Category[]; onCancel: () => void;
  onDone: (message: string) => void;
}) {
  const assign = useAssignIdentifier();
  const assignEach = useAssignAccount();
  const notMine = useNotMyAccount();
  const name = useNamePayee();
  const categorize = useCategorizePayee();
  const correct = useCorrectTransaction();
  const categoryId = useCategoryId();
  const payees = usePayees();
  const me = useMe();
  const [adding, setAdding] = useState(false);
  const error = assign.error ?? assignEach.error ?? notMine.error ?? name.error ?? categorize.error ?? correct.error
    ?? categoryId.error;
  const n = group.items.length;

  if (adding) {
    return <AddAccountForm defaultLast4={group.key} onDone={() => { setAdding(false); onDone(
      `Ending ${group.key}: a new account; its transactions are on it now.`); }} />;
  }
  let box;
  const ids = group.items.map((i) => i.id);
  const unknown = group.key === "";
  if (lane === "accounts") {
    const extras: Option[] = unknown ? [] : [
      ...(/^\d{4}$/.test(group.key) ? [{ id: "__new", label: `Add an account ending ${group.key}` }] : []),
      { id: "__notmine", label: "Not my account — keep as expenses" }];
    box = <AnswerBox label={`Account for ${group.name}`} onClose={onCancel}
      options={accounts.map((a) => ({ id: a.id, label: a.display_name }))} extras={extras}
      onPick={(o) => {
        if (o.id === "__new") setAdding(true);
        else if (o.id === "__notmine") notMine.mutate({ identifier: group.key }, {
          onSuccess: (r) => onDone(`${capital(group.name)}: ${plural(r.quieted, "transaction")} kept as expenses.`) });
        else if (unknown) assignEach.mutate({ transaction_ids: ids, account_id: o.id }, {
          onSuccess: (r) => onDone(`${plural(r.updated, "transaction")} now on ${o.label}.`) });
        else assign.mutate({ identifier: group.key, account_id: o.id }, {
          onSuccess: (r) => onDone(`${capital(group.name)}: ${plural(r.resolved, "transaction")} now on ${o.label}.`) });
      }} />;
  } else if (lane === "payees") {
    const target = unknown ? { transaction_ids: ids } : { payee_raw: group.key };
    box = <AnswerBox label={`Payee for ${group.name}`} onClose={onCancel}
      options={(payees.data ?? []).map((p) => ({ id: p.id, label: p.name }))}
      create={(typed) => ({ id: "__create", label: `Create “${typed}”` })}
      suggested={unknown ? undefined : { id: "__exact", label: `Create “${group.key}”` }}
      note={unknown ? (n === 1 ? "This one only" : `These ${n} only`) : me.data?.role === "admin" ? `All ${n} with this text, now and later`
        : `All ${n} of yours`}
      onPick={(o: Option, typed) => {
        const label = o.id === "__exact" ? group.key : o.id === "__create" ? typed : o.label;
        const body = o.id === "__exact" || o.id === "__create" ? { ...target, name: label } : { ...target, payee_id: o.id };
        name.mutate(body, { onSuccess: (r) => onDone(`Named ${label}: ${plural(r.updated, "transaction")}.`) });
      }} />;
  } else {
    const filed = (r: { updated: number; overrides_kept: number }) => onDone(
      `${plural(r.updated, "transaction")} updated${r.overrides_kept > 0 ? `, ${plural(r.overrides_kept, "override")} kept` : ""}.`);
    box = <AnswerBox label={`Category for ${group.name}`} onClose={onCancel}
      options={categories.map((c) => ({ id: c.id, label: c.name }))} create={createCategory}
      note={unknown ? (n === 1 ? "This one only: its email named no payee" : `These ${n} only: their emails named no payee`)
        : undefined}
      onPick={(o, typed) => void categoryId(o, typed).then(({ id }) => {
        if (unknown) {  // no payee to hold a rule: each transaction gets its own category
          void Promise.all(ids.map((t) => correct.mutateAsync({ id: t, change: { category_override_id: id } })))
            .then(() => onDone(`${plural(n, "transaction")} updated.`));
        } else if (group.key.startsWith("raw:")) {  // not named yet: its text becomes its name, then one rule
          const raw = group.key.slice(4);
          name.mutate({ payee_raw: raw, name: raw }, {
            onSuccess: (p) => categorize.mutate({ payee_id: p.payee_id, category_id: id }, { onSuccess: filed }) });
        } else {
          categorize.mutate({ payee_id: group.key, category_id: id }, { onSuccess: filed });
        }
      })} />;
  }
  return (
    <div className="group__answer">
      {box}
      {error && <p className="form-error" role="alert">{describeError(error)}</p>}
    </div>
  );
}

/** One transaction of a group. `single`: the group is this one transaction (a check), whose name and amount the
 *  group's own line already shows, so only its date and account are said again. */
function ItemLine({ item, accounts, categories, single = false }: {
  item: ReviewItem; accounts: Account[]; categories: Category[]; single?: boolean;
}) {
  const who = item.payee_name ?? item.payee_raw ?? "Unknown payee";
  return (
    <li className="item" aria-label={`${formatDate(item.transaction_date)} ${who} ${formatPaise(item.amount_paise)}`}>
      {single ? (
        <p className="item__line item__line--single">
          <span className="item__date">{formatDate(item.transaction_date)}</span>
          <span className="item__account muted">{item.account_label ?? ""}</span>
        </p>
      ) : (
        <div className="item__line">
          <span className="item__date">{formatDate(item.transaction_date)}</span>
          <span className="item__who">{who}</span>
          <span className="item__account muted">{item.account_label ?? ""}</span>
          <span className="amount">{formatPaise(item.amount_paise)}</span>
        </div>
      )}
      {item.reason && <p className="item__reason">{readable(item.reason)}</p>}
      {item.email && <EmailLine id={item.id} email={item.email} />}
      <TransactionActions subject={{ ...item, who }} accounts={accounts} categories={categories} actions={ALL_ACTIONS} />
    </li>
  );
}
