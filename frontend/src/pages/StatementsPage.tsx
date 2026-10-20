// Statement upload (tech-design §7, PRD §4.6): read a PDF with its password, look at what it says,
// then merge. The decision (merge or discard) heads the preview, under the account it goes to; the
// evidence for it follows. The preview's one bold element is the statement's own arithmetic, set as a ledger sum
// (opening, money in, money out, a double rule, what the rows come to against what the statement
// says). Everything around it stays quiet. A statement is the least recoverable input in the app
// (its content is discarded on merge and it supersedes emails), so the preview never stays silent:
// an unchecked statement says it is unchecked, and every unreadable row shows its raw cells.
import { type FormEvent, useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router";

import { type Account, useAccounts } from "../api/accounts";
import { describeError } from "../api/client";
import { useJob } from "../api/gmail";
import {
  type MatchRow,
  type PreviewRow,
  type StatementPreview,
  useDiscardStatement,
  useForgetStatement,
  useMergeStatement,
  useOpenStatements,
  usePickAccount,
  useStatement,
  useUploadStatement,
} from "../api/statements";
import { formatDate, formatInstant } from "../dates";
import { formatPaise } from "../money";
import {
  type Diagnostic,
  type Kind,
  type UnparseableRow,
  checkLines,
  describeCheck,
  readBy,
} from "../statements/diagnostic";
import "../styles/statements.css";

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

export function StatementsPage() {
  const [params, setParams] = useSearchParams();
  const open = params.get("open");
  const show = (id: string | null) => setParams(id ? { open: id } : {});
  return (
    <div className="statements">
      <header className="page-head"><h1 className="page-title">Statements</h1></header>
      {open ? <Preview key={open} id={open} onClose={() => show(null)} /> : (
        <>
          <OpenPreviews />
          <UploadForm onRead={(id) => show(id)} />
        </>
      )}
    </div>
  );
}

function UploadForm({ onRead }: { onRead: (id: string) => void }) {
  const upload = useUploadStatement();
  const [file, setFile] = useState<File | null>(null);
  const [password, setPassword] = useState("");
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (file) upload.mutate({ file, password }, { onSuccess: (p) => onRead(p.id) });
  };
  const sentence = upload.isError ? describeError(upload.error) : null;
  return (
    <form className="stmt-upload" onSubmit={submit} aria-label="Upload a statement">
      <p className="stmt-upload__lead">
        Upload a bank or card statement. Hisaab reads every row and shows you what it found before anything is saved.
      </p>
      <label className="field">
        <span className="field__label">Statement PDF</span>
        <input className="field__input stmt-upload__file" type="file" accept="application/pdf,.pdf"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
      </label>
      <div className="field">
        <label className="field__label" htmlFor="stmt-password">Password</label>
        <input className="field__input" id="stmt-password" type="password" autoComplete="off" value={password}
          onChange={(e) => setPassword(e.target.value)} aria-describedby="stmt-password-help" />
        <span className="field__help" id="stmt-password-help">Used once to open the file. Never saved.</span>
      </div>
      <div className="actions">
        <button className="button button--primary" type="submit" disabled={!file || upload.isPending}>
          {upload.isPending ? "Reading…" : "Read statement"}
        </button>
      </div>
      {upload.isPending && (
        <p className="stmt-upload__wait" role="status">
          A layout Hisaab doesn't know yet is read by Claude, which can take a minute or two.
        </p>
      )}
      {sentence && (
        <p className="form-error" role="alert">
          {sentence}
          {/scan|layout/i.test(sentence) && <> <Link to="/add">Add its transactions by hand.</Link></>}
        </p>
      )}
    </form>
  );
}

function OpenPreviews() {
  const staged = useOpenStatements();
  if (!staged.data?.length) return null;
  return (
    <section className="stmt-open" aria-labelledby="stmt-open-title">
      <h2 className="stmt-open__title" id="stmt-open-title">Waiting for you to merge</h2>
      <ul className="stmt-open__list">
        {staged.data.map((s) => (
          <li key={s.id}>
            <Link to={`/statements?open=${s.id}`}>{`${s.account_name ?? "Account not picked"}, ${s.filename}`}</Link>
            <span className="muted"> Discarded automatically on {formatInstant(s.expires_at)}.</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

function Preview({ id, onClose }: { id: string; onClose: () => void }) {
  const statement = useStatement(id);
  if (statement.isPending) return <p className="muted">Loading…</p>;
  if (statement.isError) {
    return (
      <div className="stmt-gone">
        <p className="form-error" role="alert">{describeError(statement.error)}</p>
        <button className="button button--quiet" type="button" onClick={onClose}>Upload a statement</button>
      </div>
    );
  }
  return <PreviewBody p={statement.data} onClose={onClose} />;
}

function PreviewBody({ p, onClose }: { p: StatementPreview; onClose: () => void }) {
  const d = p.diagnostic as unknown as Diagnostic; // the §4.8 record, typed on the client (statements/diagnostic.ts)
  const kind = p.account_kind as Kind;
  const rows = p.new.length + p.matches.length;
  const [changing, setChanging] = useState(false);
  const picking = p.account_id === null || changing;
  return (
    <article className="stmt">
      <header className="stmt__head">
        {picking ? <AccountPicker p={p} onDone={() => setChanging(false)} /> : (
          <div className="stmt__title-row">
            <h2 className="stmt__title">{p.account_name}</h2>
            <button className="button button--quiet" type="button" onClick={() => setChanging(true)}>Change account</button>
          </div>
        )}
        <p className="stmt__meta">
          {p.filename}, {readBy(d, p.template_name)}, {plural(d.pages_processed, "page", "pages")}.
          {" "}Discarded automatically on {formatInstant(p.expires_at)} unless merged.
        </p>
        {!picking && <MergeActions p={p} d={d} rows={rows} onClose={onClose} />}
      </header>
      <Check d={d} kind={kind} />
      <RowGroup title={plural(p.new.length, "new transaction", "new transactions")} rows={p.new} />
      {p.matches.length > 0 && (
        <MatchGroup title={`${p.matches.length} already here. The statement's amounts will replace the email's.`}
          rows={p.matches} />
      )}
      <Unreadable rows={p.unparseable as UnparseableRow[]} />
    </article>
  );
}

/** Choosing a radio only selects (arrow keys move through them); "Use this account" commits. */
function AccountPicker({ p, onDone }: { p: StatementPreview; onDone: () => void }) {
  const accounts = useAccounts();
  const pick = usePickAccount(p.id);
  const [choice, setChoice] = useState<string | null>(p.account_id);
  const fits = (a: Account) => a.archived_at === null && a.type === p.account_kind;
  const use = () => {
    if (choice === null) return;
    if (choice === p.account_id) onDone();
    else pick.mutate(choice, { onSuccess: onDone });
  };
  return (
    <div className="stmt-pick">
      <h2 className="stmt__title" id="stmt-pick-title">Which account is this statement for?</h2>
      <p className="muted">
        {p.account_id === null ? "The statement's account number isn't one Hisaab knows, so matches show once you pick."
          : "Matches are worked out again for the account you pick."}
      </p>
      <div className="stmt-pick__choices" role="radiogroup" aria-labelledby="stmt-pick-title">
        {(accounts.data ?? []).filter(fits).map((a) => (
          <label key={a.id} className="stmt-pick__choice">
            <input type="radio" name="stmt-account" value={a.id} checked={choice === a.id}
              onChange={() => setChoice(a.id)} />
            {a.display_name}
          </label>
        ))}
      </div>
      <div className="actions">
        <button className="button button--primary" type="button" disabled={choice === null || pick.isPending}
          onClick={use}>{pick.isPending ? "Matching…" : "Use this account"}</button>
        {p.account_id !== null && <button className="button button--quiet" type="button" onClick={onDone}>Cancel</button>}
      </div>
      {pick.isError && <p className="form-error" role="alert">{describeError(pick.error)}</p>}
    </div>
  );
}

function Check({ d, kind }: { d: Diagnostic; kind: Kind }) {
  const lines = checkLines(d, kind);
  return (
    <section className={`stmt-check stmt-check--${d.outcome}`} aria-label="The statement's own check">
      {d.tier === 3 && (
        <p className="stmt-check__source">
          Every amount below was found printed in the PDF. Rows Claude read but Hisaab couldn't find there are
          under “couldn't be read”.
        </p>
      )}
      <p className="stmt-check__sentence">{describeCheck(d, kind)}</p>
      {lines.length > 0 ? (
        <table className="stmt-sum">
          <tbody>
            {lines.map((l) => (
              <tr key={l.label} className={l.total ? "stmt-sum__total" : l.label === "Gap" ? "stmt-sum__gap" : undefined}>
                <th scope="row"><span className="stmt-sum__sign" aria-hidden="true">{l.sign ?? ""}</span>{l.label}</th>
                <td>{formatPaise(l.paise)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="stmt-check__advice">Read through the rows before merging.</p>
      )}
      <Report d={d} />
    </section>
  );
}

function Report({ d }: { d: Diagnostic }) {
  const [open, setOpen] = useState(false);
  return (
    <details className="stmt-report" aria-label="Show the full parser report"
      onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}>
      <summary>{open ? "Hide the full parser report" : "Show the full parser report"}</summary>
      {d.unparseable_rows.length > 0 && <RawCells rows={d.unparseable_rows} />}
      <pre className="stmt-report__json">{JSON.stringify(d, null, 2)}</pre>
    </details>
  );
}

/** Each unreadable row cell by cell, empty cells drawn as empty boxes: a cell in the wrong column means a
 *  column-mapping problem, a row that should have joined the one above means a continuation problem. */
function RawCells({ rows }: { rows: UnparseableRow[] }) {
  return (
    <table className="stmt-raw">
      <caption>Rows that couldn't be read, as the PDF has them</caption>
      <tbody>
        {rows.map((r, i) => (
          <tr key={i}>
            <th scope="row">Page {r.page}<span className="stmt-raw__why">{r.reason}</span></th>
            {r.raw_cells.map((cell, j) => (
              <td key={j} className={cell ? undefined : "stmt-raw__empty"}>{cell || <span className="visually-hidden">empty</span>}</td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

const amount = (r: PreviewRow) => (
  <>{formatPaise(r.amount_paise)}{r.direction === "credit" && <span className="cr">Cr</span>}</>
);

function RowGroup({ title, rows }: { title: string; rows: PreviewRow[] }) {
  if (rows.length === 0) return null;
  const titleId = "stmt-new-title";
  return (
    <section className="stmt-group" aria-labelledby={titleId}>
      <h3 className="stmt-group__title" id={titleId}>{title}</h3>
      <table className="ledger stmt-rows">
        <tbody>
          {rows.map((r) => (
            <tr key={r.index}>
              <td className="stmt-rows__date">{formatDate(r.date)}</td>
              <td className="stmt-rows__what">
                <span className="stmt-rows__narration">{r.narration}</span>
                {r.payment && <span className="stmt-rows__note">Card bill payment</span>}
              </td>
              <td className="amount">{amount(r)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function MatchGroup({ title, rows }: { title: string; rows: MatchRow[] }) {
  return (
    <section className="stmt-group" aria-labelledby="stmt-match-title">
      <h3 className="stmt-group__title" id="stmt-match-title">{title}</h3>
      <table className="ledger stmt-rows">
        <tbody>
          {rows.map((r) => {
            const differs = r.current_amount_paise !== r.amount_paise;
            return (
              <tr key={r.index}>
                <td className="stmt-rows__date">{formatDate(r.date)}</td>
                <td className="stmt-rows__what">
                  <span className="stmt-rows__payee">{r.payee ?? r.narration}</span>
                  {r.payee && <span className="stmt-rows__note">{r.narration}</span>}
                </td>
                <td className="amount">
                  {amount(r)}
                  {differs && (
                    <span className={`stmt-rows__was${r.edited.includes("amount") ? " stmt-rows__was--kept" : ""}`}>
                      {r.edited.includes("amount") ? "Your edit stays: " : "was "}{formatPaise(r.current_amount_paise)}
                    </span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}

function Unreadable({ rows }: { rows: UnparseableRow[] }) {
  if (rows.length === 0) {
    return <p className="stmt-group stmt-group--clear">Every row was read.</p>;
  }
  return (
    <section className="stmt-group stmt-group--unread" aria-labelledby="stmt-unread-title">
      <h3 className="stmt-group__title" id="stmt-unread-title">{plural(rows.length, "row couldn't be read", "rows couldn't be read")}</h3>
      <p className="muted">They won't be merged. Add them by hand after merging if they're real.</p>
      <RawCells rows={rows} />
    </section>
  );
}

function MergeActions({ p, d, rows, onClose }: { p: StatementPreview; d: Diagnostic; rows: number; onClose: () => void }) {
  const merge = useMergeStatement(p.id);
  const discard = useDiscardStatement();
  const forget = useForgetStatement();
  const [jobId, setJobId] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const job = useJob(jobId);
  const done = job.data?.state === "succeeded";
  useEffect(() => { if (done) void forget(p.id); }, [done, forget, p.id]);

  const start = (acceptGap: boolean) => merge.mutate(acceptGap, { onSuccess: (id) => setJobId(id) });
  if (done) {
    const r = job.data?.result as { new: number; matched: number; needs_review: number } | null;
    return (
      <div className="stmt__actions stmt__actions--done" role="status">
        <p className="stmt__result">
          {r ? `${r.new} added, ${r.matched} updated, ${r.needs_review} ${r.needs_review === 1 ? "needs" : "need"} review.` : "Merged."}
        </p>
        <div className="actions">
          <Link className="button button--quiet" to="/review">Open Review</Link>
          <button className="button button--quiet" type="button" onClick={onClose}>Upload another statement</button>
        </div>
      </div>
    );
  }
  const busy = merge.isPending || (jobId !== null && job.data?.state !== "failed");
  const failed = d.outcome === "failed";
  const gap = formatPaise(Math.abs(d.gap_paise ?? 0));
  return (
    <div className="stmt__actions">
      {busy ? <p className="stmt__result" role="status">Merging…</p> : confirming ? (
        <div className="stmt-confirm">
          <p>The {gap} gap is kept on record. Merge anyway?</p>
          <div className="actions">
            <button className="button button--primary" type="button" onClick={() => start(true)}>Yes, merge anyway</button>
            <button className="button button--quiet" type="button" onClick={() => setConfirming(false)}>Cancel</button>
          </div>
        </div>
      ) : (
        <div className="actions">
          {p.account_id === null ? <p className="muted">Pick the account to merge.</p> : failed ? (
            <button className="button stmt__anyway" type="button" onClick={() => setConfirming(true)}>Merge anyway</button>
          ) : (
            <button className="button button--primary" type="button" disabled={rows === 0} onClick={() => start(false)}>
              {`Merge ${plural(rows, "row", "rows")} into ${p.account_name ?? ""}`}
            </button>
          )}
          <button className="button button--quiet" type="button" disabled={discard.isPending}
            onClick={() => discard.mutate(p.id, { onSuccess: onClose })}>Discard</button>
        </div>
      )}
      {(merge.isError || discard.isError || job.data?.state === "failed") && (
        <p className="form-error" role="alert">
          {job.data?.state === "failed" ? job.data.error : describeError(merge.error ?? discard.error)}
        </p>
      )}
    </div>
  );
}
