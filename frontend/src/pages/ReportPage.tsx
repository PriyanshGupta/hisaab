// The report: the home screen, opened daily (PRD §7, tech-design §8). One period's spending by
// category. The Spending / Cash out toggle changes only the big total: the breakdown is always
// spending, so nothing shifts underneath you (§8.2). The uncategorized line is always there, even
// at zero: if money is unclassified, every share on the screen is wrong (§7.4).
import { useState } from "react";
import { Link, useSearchParams } from "react-router";

import { describeError } from "../api/client";
import { type Report, type ReportCategory, type ReportPayee, type ReportTransaction,
  type ReportTransfer, useReport } from "../api/report";
import { useReview } from "../api/review";
import { formatDate } from "../dates";
import { formatPaise } from "../money";
import { BalanceBanners, BalancesPanel } from "../report/Balances";
import { type Period, type Unit, fromParams, indiaToday, periodAt, periodTitle, shift, toParams } from "../report/period";
import { countLeft } from "../review/lanes";
import "../styles/report.css";

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;
const UNITS: { unit: Unit; label: string }[] = [
  { unit: "day", label: "Day" }, { unit: "week", label: "Week" }, { unit: "month", label: "Month" },
  { unit: "custom", label: "Custom" },
];
const PROVENANCE: Record<ReportTransaction["provenance"], string> = {
  email: "From an email", statement: "From a statement", manual: "Entered by hand",
};
const TRANSFER: Record<ReportTransfer["kind"], (t: ReportTransfer) => string> = {
  card_bill: () => "Card bill paid", cash_withdrawal: () => "Cash withdrawn",
  between_accounts: (t) => `To ${t.to_label ?? "another account"}`,
};
const share = (part: number, whole: number) => (part > 0 && whole > 0 ? `${Math.round((part / whole) * 100)}%` : "");

export function ReportPage() {
  const [params, setParams] = useSearchParams();
  const today = indiaToday();
  const period = fromParams(params, today);
  const cash = params.get("basis") === "cash";
  const report = useReport(period.start, period.end);
  const review = useReview();
  const left = countLeft(review.data?.items ?? []);

  const go = (p: Period, basis = cash, replace = false) =>
    setParams({ ...toParams(p), ...(basis ? { basis: "cash" } : {}) }, { replace });
  // a new length keeps today in view when today is in the current period, else the period's first day
  const anchor = period.start <= today && today <= period.end ? today : period.start;
  const pickUnit = (unit: Unit) =>
    go(unit === "custom" ? { unit, start: period.start, end: period.end } : periodAt(unit, anchor));
  const next = shift(period, 1);
  const word = period.unit === "custom" ? "period" : period.unit;

  return (
    <div className="report">
      <header className="report__top">
        <div>
          <div className="report__nav">
            {period.unit !== "custom" && (
              <button type="button" className="icon-btn" aria-label={`Previous ${word}`} onClick={() => go(shift(period, -1))}>‹</button>
            )}
            <h1 className="report__title">{periodTitle(period)}</h1>
            {period.unit !== "custom" && (
              <button type="button" className="icon-btn" aria-label={`Next ${word}`} disabled={next.start > today}
                onClick={() => go(next)}>›</button>
            )}
          </div>
          <div className="report__controls">
            <div className="seg" role="group" aria-label="Period length">
              {UNITS.map((u) => (
                <button key={u.unit} type="button" aria-pressed={period.unit === u.unit} onClick={() => pickUnit(u.unit)}>
                  {u.label}
                </button>
              ))}
            </div>
            {period.unit === "custom" && (
              <CustomRange key={`${period.start}/${period.end}`} period={period} today={today}
                onRange={(p) => go(p, cash, true)} />
            )}
          </div>
        </div>
        {left > 0 && (
          <Link className="pill" to="/review"><span className="pill__n">{left}</span> waiting for review</Link>
        )}
      </header>

      <BalanceBanners />
      {report.isError && <p className="form-error" role="alert">{describeError(report.error)}</p>}
      {report.data && (
        <>
          <section className="sheet sheet--ruled" aria-label="Spending">
            <Hero report={report.data} cash={cash} title={periodTitle(period)} onBasis={(b) => go(period, b)} />
            <Ledger report={report.data} word={word} />
          </section>
          <Moved report={report.data} cash={cash} word={word} />
        </>
      )}
      <BalancesPanel />
      {report.isPending && <p className="muted">Loading…</p>}
    </div>
  );
}

const EARLIEST = "2000-01-01"; // below this a date input is still mid-typing ("0002-…", "0202-…")

/**
 * The two dates of a custom range. Browsers report every keystroke of a typed date, so each one
 * lands in a draft; only a whole range (both real dates, from ≤ to, not in the future) becomes
 * the period. Edits within custom mode replace the history entry rather than piling them up.
 */
export function CustomRange({ period, today, onRange }: { period: Period; today: string; onRange: (p: Period) => void }) {
  const [draft, setDraft] = useState({ start: period.start, end: period.end });
  const change = (field: "start" | "end", value: string) => {
    const next = { ...draft, [field]: value };
    setDraft(next);
    if (next.start >= EARLIEST && next.start <= next.end && next.end <= today) onRange({ unit: "custom", ...next });
  };
  return (
    <div className="report__custom">
      <label>From <input type="date" value={draft.start} min={EARLIEST} max={draft.end}
        onChange={(e) => change("start", e.target.value)} /></label>
      <label>to <input type="date" value={draft.end} min={draft.start} max={today}
        onChange={(e) => change("end", e.target.value)} /></label>
    </div>
  );
}

function Hero({ report, cash, title, onBasis }: { report: Report; cash: boolean; title: string; onBasis: (cash: boolean) => void }) {
  const total = cash ? report.cash_out_paise : report.spending_paise;
  return (
    <div className="hero">
      <div className="hero__top">
        <p className="hero__lead">{cash ? `Left your bank and cash in ${title}` : `Spent in ${title}`}</p>
        <div className="seg" role="group" aria-label="What the total counts">
          <button type="button" aria-pressed={!cash} onClick={() => onBasis(false)}>Spending</button>
          <button type="button" aria-pressed={cash} onClick={() => onBasis(true)}>Cash out</button>
        </div>
      </div>
      <p className="hero__total num" data-testid="hero-total">{formatPaise(total)}</p>
      <p className="hero__note">
        {cash
          ? "Cash out counts payments from your bank and cash accounts, plus card bills you paid. The categories below always show spending, so they don’t shift when you switch."
          : "Card purchases count on the day you made them, not when the bill is paid. Card bills and moves between your own accounts are listed separately below."}
      </p>
      {cash && report.cash_out_unknown_paise !== 0 && (
        <p className="hero__note hero__note--attention">
          {formatPaise(report.cash_out_unknown_paise)} has no account yet, so it isn’t in cash out.{" "}
          <Link to="/review?lane=accounts">Assign accounts</Link>
        </p>
      )}
    </div>
  );
}

function Ledger({ report, word }: { report: Report; word: string }) {
  const [open, setOpen] = useState<string | null>(null);
  const widest = Math.max(1, ...report.categories.map((c) => c.amount_paise));
  const counted = report.categories.reduce((n, c) => n + c.count, 0);
  return (
    <>
      <div className="cats__head" aria-hidden="true">
        <span>Category</span><span /><span className="r">Amount</span><span className="r">Share</span><span />
      </div>
      <ul className="cats" aria-label="Spending by category">
        {report.categories.map((c) => {
          const key = c.category_id ?? "uncategorized";
          return (
            <CategoryRow key={key} c={c} widest={widest} total={report.spending_paise} word={word}
              open={open === key} onToggle={() => setOpen(open === key ? null : key)} />
          );
        })}
      </ul>
      <p className="cats__foot">
        <span>{plural(counted, "payment")} counted. Refunds are taken off their category.</span>
        {report.reversed.length > 0 && <span>{plural(report.reversed.length, "failed payment")} not counted</span>}
      </p>
    </>
  );
}

function Bar({ amount, widest, label }: { amount: number; widest: number; label: string }) {
  return (
    <span className="track" title={label}>
      <span className="bar" aria-hidden="true" style={{ width: `${(Math.max(0, amount) / widest) * 100}%` }} />
    </span>
  );
}

function CategoryRow({ c, widest, total, word, open, onToggle }: {
  c: ReportCategory; widest: number; total: number; word: string; open: boolean; onToggle: () => void;
}) {
  const uncategorized = c.category_id === null;
  const [openPayee, setOpenPayee] = useState<string | null>(null);
  const payeeWidest = Math.max(1, ...c.payees.map((p) => p.amount_paise));
  return (
    <li className={`lrow-wrap${uncategorized ? " lrow-wrap--uncat" : ""}`}>
      <div className="lrow">
        <button type="button" className="lname" aria-expanded={open} disabled={c.count === 0} onClick={onToggle}>
          <span className="caret" aria-hidden="true">›</span>
          <span className="lname__text">
            <span className="lname__name">{c.name}</span>
            <span className="lname__sub">{c.count ? plural(c.count, "payment") : `None this ${word}`}</span>
          </span>
        </button>
        <Bar amount={c.amount_paise} widest={widest} label={`${c.name}: ${formatPaise(c.amount_paise)}`} />
        <span className="amt num r">{formatPaise(c.amount_paise)}</span>
        <span className="share num r">{share(c.amount_paise, total)}</span>
        <span className="act r">
          {uncategorized && (c.count > 0
            ? <Link to="/review?lane=categories">Sort {plural(c.count, "payment")}</Link>
            : <span className="muted">All sorted</span>)}
        </span>
      </div>
      {open && (
        <ul className="payees" aria-label={`${c.name} payees`}>
          {c.payees.map((p) => (
            <PayeeRow key={p.key} p={p} widest={payeeWidest} categoryTotal={c.amount_paise}
              open={openPayee === p.key} onToggle={() => setOpenPayee(openPayee === p.key ? null : p.key)} />
          ))}
        </ul>
      )}
    </li>
  );
}

function PayeeRow({ p, widest, categoryTotal, open, onToggle }: {
  p: ReportPayee; widest: number; categoryTotal: number; open: boolean; onToggle: () => void;
}) {
  return (
    <li>
      <div className="prow">
        <button type="button" className="lname lname--payee" aria-expanded={open} onClick={onToggle}>
          <span className="caret" aria-hidden="true">›</span>
          <span className="lname__text">
            <span className="lname__name">{p.name}</span>
            <span className="lname__sub">{plural(p.transactions.length, "payment")}</span>
          </span>
        </button>
        <Bar amount={p.amount_paise} widest={widest} label={`${p.name}: ${formatPaise(p.amount_paise)}`} />
        <span className="amt num r">{formatPaise(p.amount_paise)}</span>
        <span className="share num r">{share(p.amount_paise, categoryTotal)}</span>
        <span />
      </div>
      {open && (
        <ul className="txns" aria-label={`${p.name} payments`}>
          {p.transactions.map((t) => (
            <li key={t.id} className="txn">
              <span className="txn__date">{formatDate(t.transaction_date)}</span>
              <span className="txn__where">
                {t.account_label ?? "No account yet"}
                <span className="muted">{PROVENANCE[t.provenance]}</span>
                {t.needs_review && <span className="chip">Needs review</span>}
              </span>
              <span className="amt num r">
                {formatPaise(t.amount_paise)}{t.kind === "refund" && <span className="muted"> refund</span>}
              </span>
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}

function Moved({ report, cash, word }: { report: Report; cash: boolean; word: string }) {
  return (
    <section className="sheet sheet--ruled moved" aria-labelledby="moved-h">
      <div className="moved__head">
        <h2 id="moved-h">Money in and moved</h2>
        <p className="muted">None of this is counted as spending</p>
      </div>
      <div className="frow frow--total"><span>Money in</span><span className="num">{formatPaise(report.money_in_paise)}</span></div>
      {report.income.map((p) => (
        <div key={p.key} className="frow frow--minor"><span>{p.name}</span><span className="num">{formatPaise(p.amount_paise)}</span></div>
      ))}
      <h3>Moved between your own accounts</h3>
      {report.transfers.length === 0 && <p className="muted">No transfers this {word}.</p>}
      {report.transfers.map((t) => (
        <div key={t.id} className="frow">
          <span>
            {TRANSFER[t.kind](t)}
            {cash && t.kind === "card_bill" && <span className="frow__sub">Counted in cash out</span>}
            <span className="frow__sub">{t.from_label ?? "An account not named yet"} to {t.to_label ?? "an account not named yet"}, {formatDate(t.transaction_date)}</span>
          </span>
          <span className="num">{formatPaise(t.amount_paise)}</span>
        </div>
      ))}
      {report.reversed.length > 0 && (
        <>
          <h3>Failed payments</h3>
          {report.reversed.map((t) => (
            <div key={t.id} className="frow">
              <span><s>{t.payee_raw ?? "Payment"}</s>
                <span className="frow__sub">{formatDate(t.transaction_date)}. Reversed, so it never left your account</span></span>
              <span className="num"><s>{formatPaise(t.amount_paise)}</s></span>
            </div>
          ))}
        </>
      )}
    </section>
  );
}
