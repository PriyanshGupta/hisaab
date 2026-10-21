// Payee names (PRD §5.1 Loop 1, §5.6): the corrections people made to payee names, for the admin to turn into
// patterns everyone shares. Each shows the raw text the alerts print, the name chosen, how many transactions
// carry that text and who corrected it. Promote applies the pattern to everyone's history at once, in the
// database alone: nothing is fetched from Gmail.
import { type FormEvent, useState } from "react";

import { describeError } from "../api/client";
import { type CapturedCorrection, useCorrections, useDismissCorrection, usePromote } from "../api/payeeNames";

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

function andList(items: string[]): string {
  return items.length <= 1 ? items.join("") : `${items.slice(0, -1).join(", ")} and ${items.at(-1)}`;
}

export function PayeeNamesPage() {
  const list = useCorrections();
  const [done, setDone] = useState<string | null>(null);
  return (
    <div className="admin-payees">
      <header className="page-head"><h1 className="page-title">Payee names</h1></header>
      <p className="admin__lead">
        When someone corrects a payee, the correction waits here. Promote it to name that text for everyone, past
        transactions included.
      </p>
      {done && <p className="admin__done" role="status">{done}</p>}
      {list.isError && <p className="form-error" role="alert">{describeError(list.error)}</p>}
      {list.data && (list.data.length === 0
        ? <div className="empty">
            <p className="empty__lead">No corrections waiting.</p>
            <p className="empty__body">They appear when someone changes the payee of a transaction whose alert printed
              a name Hisaab doesn't know.</p>
          </div>
        : list.data.map((c) => <CorrectionSheet key={`${c.key}-${c.payee_id}`} c={c} onDone={setDone} />))}
    </div>
  );
}

function CorrectionSheet({ c, onDone }: { c: CapturedCorrection; onDone: (s: string) => void }) {
  const promote = usePromote();
  const dismiss = useDismissCorrection();
  const [editing, setEditing] = useState(false);
  const [match, setMatch] = useState("exact");
  const [pattern, setPattern] = useState(c.raw);
  const [name, setName] = useState(c.payee_name);
  const headId = `corr-${c.key}-${c.payee_id}`;
  const busy = promote.isPending || dismiss.isPending;
  const reported = andList(c.reporters.map((r) => `${r.user} (${r.count})`));

  const send = (body: Parameters<typeof promote.mutate>[0], shown: string) => promote.mutate(body, {
    onSuccess: (r) => onDone(`Promoted. ${plural(r.named, "transaction", "transactions")} for ${plural(r.users, "person", "people")} now read ${shown}. Nothing was fetched from Gmail.`),
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const same = name.trim() === c.payee_name;
    send({ match_type: match, pattern, ...(same ? { payee_id: c.payee_id } : { payee_name: name.trim() }) }, name.trim());
  };

  return (
    <section className="sheet sheet--ruled admin-payee" aria-labelledby={headId}>
      <h2 id={headId} className="admin-payee__raw">{c.raw}</h2>
      <p className="admin-payee__name">{c.payee_name}</p>
      <p className="muted">{plural(c.shared, "transaction carries", "transactions carry")} this text. Reported by {reported}.</p>
      {editing ? (
        <form className="admin-payee__edit" onSubmit={submit}>
          <label className="field"><span className="field__label">Match</span>
            <select className="field__input" value={match} onChange={(e) => setMatch(e.target.value)}>
              <option value="exact">The whole text</option>
              <option value="contains">Contains</option>
              <option value="regex">Regular expression</option>
            </select></label>
          <label className="field"><span className="field__label">Pattern</span>
            <input className="field__input" value={pattern} onChange={(e) => setPattern(e.target.value)} required /></label>
          <label className="field"><span className="field__label">Payee name</span>
            <input className="field__input" value={name} onChange={(e) => setName(e.target.value)} required /></label>
          <div className="actions">
            <button className="button button--quiet" type="button" onClick={() => setEditing(false)}>Cancel</button>
            <button className="button button--primary" type="submit" disabled={busy}>Promote this pattern</button>
          </div>
        </form>
      ) : (
        <div className="actions">
          <button className="button button--primary" type="button" disabled={busy}
            onClick={() => send({ match_type: "exact", pattern: c.raw, payee_id: c.payee_id }, c.payee_name)}>Promote</button>
          <button className="button button--quiet" type="button" onClick={() => setEditing(true)}>Edit before promoting</button>
          <button className="button button--quiet" type="button" disabled={busy}
            onClick={() => dismiss.mutate({ key: c.key, payee_id: c.payee_id })}>Dismiss</button>
        </div>
      )}
      {(promote.isError || dismiss.isError) && (
        <p className="form-error" role="alert">{describeError(promote.error ?? dismiss.error)}</p>
      )}
    </section>
  );
}
