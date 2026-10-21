// The template promotion queue (PRD §6.8, tech-design §4.10 step 4; slice 22). Admin only.
//
// Replay proves consistency, not correctness: a template that copies a misreading passes. So review is
// the email beside the readings. Each example's text is the text the patterns read, with the values
// Haiku extracted marked where they occur (the page's one bold element: you see where each value came
// from), beside a ledger table of Haiku's reading and the template's. A mismatch says "No match" in
// words and carries the red margin rule. Approve only when replay passed; Reject discards a candidate
// that passed but reads something wrong. Demoted templates close the page, then the formats treated as
// not transactions (their emails skip Haiku), each of which can be read again.
import { useState } from "react";

import { describeError } from "../api/client";
import {
  type Approved,
  type Candidate,
  type CandidateExample,
  type Excluded,
  type ReplayField,
  type TemplateInUse,
  useApproveTemplate,
  useEditSpec,
  useIncludeFormat,
  useRejectTemplate,
  useTemplateQueue,
  useTemplatesInUse,
} from "../api/templates";
import { formatDate, formatInstantDate } from "../dates";
import { formatPaise } from "../money";
import "../styles/templates.css";

const LABELS: Record<string, string> = {
  outcome: "Kind of email", amount: "Amount", currency: "Currency", direction: "Money in or out",
  account_identifier: "Account", counterparty_identifier: "Other side", payee_raw: "Payee",
  reference_number: "Reference", date: "Date", time: "Time", balance: "Balance",
};
// The values worth finding in the email: the ones a misreading would hide in.
const MARKED: readonly string[] = ["account_identifier", "counterparty_identifier", "payee_raw", "reference_number"];

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

function show(field: string, value: unknown): string {
  if (value === null || value === undefined || value === "") return "Not read";
  if ((field === "amount" || field === "balance") && typeof value === "number") return formatPaise(value);
  if (field === "date" && typeof value === "string") return formatDate(value);
  if (field === "direction") return value === "debit" ? "Money out" : value === "credit" ? "Money in" : String(value);
  if (field === "outcome") return value === "transaction" ? "A transaction" : "Not a transaction";
  return String(value);
}

/** "email 2: reference_number differs" -> "Email 2: reference differs"; other problems as written. */
function problem(text: string): string {
  const m = /^email (\d+): (\w+) differs$/.exec(text);
  if (m) return `Email ${m[1]}: ${(LABELS[m[2] ?? ""] ?? m[2] ?? "").toLowerCase()} differs`;
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function approvedSentence(name: string, r: Approved): string {
  let s = `Approved ${name}. Re-read ${plural(r.reread, "email", "emails")} with it`;
  s += r.flagged ? `; ${r.flagged} went to Review because it read them differently.` : ".";
  if (r.waiting) s += ` ${plural(r.waiting, "email it couldn't read goes", "emails it couldn't read go")} back to Haiku on the next sync.`;
  return `${s} ${plural(r.exported.length, "example", "examples")} exported to fixtures.`;
}

export function TemplatesPage() {
  const queue = useTemplateQueue();
  const [done, setDone] = useState<string | null>(null);

  return (
    <div className="templates">
      <header className="page-head">
        <h1 className="page-title">Templates</h1>
      </header>
      <p className="templates__lead">
        When Haiku has read three emails of a format no template knows, Sonnet proposes one. Check each value
        against the email itself before approving: a template that copies a misreading still passes.
      </p>
      {done && <p className="templates__done" role="status">{done}</p>}
      {queue.isError && <p className="form-error" role="alert">{describeError(queue.error)}</p>}
      {queue.data && (queue.data.candidates.length === 0
        ? <div className="empty">
            <p className="empty__lead">No templates waiting.</p>
            <p className="empty__body">A format shows up here after three emails Haiku had to read.</p>
          </div>
        : queue.data.candidates.map((c) => <CandidateSheet key={c.id} c={c} onApproved={setDone} />))}
      <InUse />
      {queue.data && queue.data.demoted.length > 0 && (
        <section className="templates__demoted" aria-labelledby="demoted-h">
          <h2 id="demoted-h">Demoted</h2>
          <p className="muted">These stopped matching their emails, which go back to Haiku until a new template is approved.</p>
          <ul>
            {queue.data.demoted.map((d) => (
              <li key={d.id} className="templates__gone">
                <span className="templates__name">{d.name}</span>
                <span className="muted">
                  Stopped after {plural(d.consecutive_failures, "failed read", "failed reads")}
                  {d.demoted_at ? `, ${formatInstantDate(d.demoted_at)}` : ""}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}
      {queue.data && queue.data.excluded.length > 0 && <ExcludedList formats={queue.data.excluded} />}
    </div>
  );
}

function InUse() {
  const inUse = useTemplatesInUse();
  if (!inUse.data || inUse.data.length === 0) return null;
  return (
    <section className="templates__in-use" aria-labelledby="in-use-h">
      <h2 id="in-use-h">Templates in use</h2>
      <p className="muted">Editing one raises a notice above to re-sync the emails it read.</p>
      <ul>{inUse.data.map((t) => <InUseRow key={t.id} t={t} />)}</ul>
    </section>
  );
}

function InUseRow({ t }: { t: TemplateInUse }) {
  const edit = useEditSpec();
  const [text, setText] = useState<string | null>(null);
  const [notJson, setNotJson] = useState(false);
  const [saved, setSaved] = useState(false);
  const where = t.editable ? "Learned here" : "Ships with the app";
  const save = () => {
    let spec: Record<string, unknown>;
    try {
      spec = JSON.parse(text ?? "") as Record<string, unknown>;
    } catch {
      setNotJson(true);
      return;
    }
    setNotJson(false);
    edit.mutate({ id: t.id, spec }, { onSuccess: () => { setText(null); setSaved(true); } });
  };
  return (
    <li className="templates__use" role="group" aria-label={t.name}>
      <span className="templates__use-head">
        <span className="templates__name">{t.name}</span>
        <span className="muted">{where}, read {plural(t.emails_read, "email", "emails")}</span>
      </span>
      {t.editable ? (text === null
        ? <button className="button button--quiet" type="button" onClick={() => { setText(JSON.stringify(t.spec, null, 2)); setSaved(false); }}>Edit</button>
        : <div className="templates__use-edit">
            <label className="field"><span className="field__label">Spec for {t.name}</span>
              <textarea className="field__input templates__spec-box" rows={14} value={text} spellCheck={false}
                onChange={(e) => setText(e.target.value)} /></label>
            <div className="actions">
              <button className="button button--quiet" type="button" onClick={() => { setText(null); setNotJson(false); }}>Cancel</button>
              <button className="button button--primary" type="button" disabled={edit.isPending} onClick={save}>Save</button>
            </div>
          </div>)
        : <p className="muted templates__use-note">Ships with the app: its file is in the repository, and the next
            start picks up a change to it.</p>}
      {saved && <p className="templates__use-saved" role="status">Saved.</p>}
      {(notJson || edit.isError) && (
        <p className="form-error" role="alert">{notJson ? "That spec isn't valid JSON." : describeError(edit.error)}</p>
      )}
    </li>
  );
}

function ExcludedList({ formats }: { formats: Excluded[] }) {
  const include = useIncludeFormat();
  return (
    <section className="templates__demoted" aria-labelledby="excluded-h">
      <h2 id="excluded-h">Treated as not transactions</h2>
      <p className="muted">
        Haiku read two emails of each of these as moving no money, so their emails are no longer sent to it. If one
        of them does report payments, read it again: its next emails go back to Haiku.
      </p>
      <ul>
        {formats.map((x) => (
          <li key={x.id} className="templates__gone templates__gone--action">
            <span>
              <span className="templates__name">{x.subject_masked}</span>
              <span className="muted"> {x.sender}, seen {plural(x.sightings, "time", "times")}</span>
            </span>
            <button className="button button--quiet" type="button" disabled={include.isPending}
              onClick={() => include.mutate(x.id)}>Read again</button>
          </li>
        ))}
      </ul>
      {include.isError && <p className="form-error" role="alert">{describeError(include.error)}</p>}
    </section>
  );
}

function CandidateSheet({ c, onApproved }: { c: Candidate; onApproved: (sentence: string) => void }) {
  const approve = useApproveTemplate();
  const reject = useRejectTemplate();
  const headId = `cand-${c.id}`;
  const busy = approve.isPending || reject.isPending;
  const count = c.examples.length === 3 ? "all three" : `all ${c.examples.length}`;

  return (
    <section className="sheet sheet--ruled cand" aria-labelledby={headId}>
      <h2 id={headId} className="cand__head">
        <span className="cand__subject">{c.subject_masked}</span>
        <span className="cand__sender">{c.sender}</span>
      </h2>
      <p className="cand__seen">Seen {plural(c.sightings, "time", "times")} since {formatInstantDate(c.first_seen_at)}</p>
      {c.passed
        ? <p className="cand__verdict cand__verdict--pass">Reads {count} emails exactly as Haiku did.</p>
        : <div className="cand__verdict cand__verdict--fail">
            <p>Didn't pass. It can't be approved; Sonnet tries again when the next email of this format arrives.</p>
            <ul>{c.problems.map((p) => <li key={p}>{problem(p)}</li>)}</ul>
          </div>}
      {c.examples.map((e, i) => <ExampleBlock key={e.source_id} n={i + 1} e={e} />)}
      <details className="cand__spec">
        <summary>Show spec</summary>
        <pre>{JSON.stringify(c.spec, null, 2)}</pre>
      </details>
      <div className="actions cand__actions">
        <button className="button button--primary" type="button" disabled={!c.passed || busy}
          aria-describedby={c.passed ? undefined : `${headId}-why`}
          onClick={() => approve.mutate(c.id, { onSuccess: (r) => onApproved(approvedSentence(c.name, r)) })}>
          Approve template
        </button>
        {c.status === "validated" && (
          <button className="button button--quiet" type="button" disabled={busy} onClick={() => reject.mutate(c.id)}>
            Reject
          </button>
        )}
        {!c.passed && <span id={`${headId}-why`} className="muted">Only a template that reads every email right can be approved.</span>}
      </div>
      {(approve.isError || reject.isError) && (
        <p className="form-error" role="alert">{describeError(approve.error ?? reject.error)}</p>
      )}
    </section>
  );
}

function ExampleBlock({ n, e }: { n: number; e: CandidateExample }) {
  const values = e.fields.filter((f) => MARKED.includes(f.field) && typeof f.expected === "string" && f.expected)
    .map((f) => f.expected as string);
  return (
    <div className="cand__example" role="group" aria-label={`Email ${n}`}>
      <p className="cand__example-head">
        Email {n}{e.received_at ? `, received ${formatInstantDate(e.received_at)}` : ""}
      </p>
      <div className="cand__pair">
        <div className="cand__text">
          {e.text === null ? <p className="muted">This email is no longer stored.</p> : <p>{marked(e.text, values)}</p>}
        </div>
        <table className="ledger cand__table">
          <thead>
            <tr><th scope="col">Field</th><th scope="col">Haiku read</th><th scope="col">Template read</th><th scope="col"><span className="visually-hidden">Same?</span></th></tr>
          </thead>
          <tbody>{e.fields.map((f) => <FieldRow key={f.field} f={f} />)}</tbody>
        </table>
      </div>
    </div>
  );
}

function FieldRow({ f }: { f: ReplayField }) {
  return (
    <tr className={f.match ? undefined : "cand__miss"}>
      <th scope="row">{LABELS[f.field] ?? f.field}</th>
      <td>{show(f.field, f.expected)}</td>
      <td>{show(f.field, f.got)}</td>
      <td className="cand__same">{f.match ? "Match" : "No match"}</td>
    </tr>
  );
}

/** The text with each value marked where it occurs (longest first, so a name inside a longer one stays whole). */
function marked(text: string, values: string[]) {
  const unique = [...new Set(values)].sort((a, b) => b.length - a.length);
  if (unique.length === 0) return text;
  const parts = text.split(new RegExp(`(${unique.map((v) => v.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})`));
  return parts.map((part, i) => (unique.includes(part) ? <mark key={i}>{part}</mark> : part));
}
