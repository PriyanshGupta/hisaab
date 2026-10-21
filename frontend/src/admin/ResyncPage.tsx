// Re-sync a date range (tech-design §5.2; PRD §6.7): fetch that range's emails from Gmail again and re-read
// them, for every inbox or one. Per field: what someone edited and what a statement set stay; the rest takes the
// new reading. It rewrites real data, so it asks first, naming the range and how many transactions may change.
import { type FormEvent, useState } from "react";
import { useLocation, useSearchParams } from "react-router";

import { describeError } from "../api/client";
import { type ResyncPreview, useResyncPreview, useStartResync } from "../api/resync";
import { useUsers } from "../api/users";
import { formatDate, todayIso } from "../dates";

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

function daysBefore(iso: string, days: number): string {
  const d = new Date(`${iso}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() - days);
  return d.toISOString().slice(0, 10);
}

export function ResyncPage() {
  const { search } = useLocation();
  return <ResyncForm key={search} />;  // a new range from the banner starts the form afresh, even on this page
}

function ResyncForm() {
  const [params] = useSearchParams();
  const today = todayIso();
  const [start, setStart] = useState(params.get("from") ?? daysBefore(today, 6));
  const [end, setEnd] = useState(params.get("to") ?? today);
  const [who, setWho] = useState("");
  const users = useUsers();
  const preview = useResyncPreview();
  const run = useStartResync();
  const [asked, setAsked] = useState<ResyncPreview | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const range = { start, end, user_id: who || null };
  const scope = who ? `${users.data?.find((u) => u.id === who)?.display_name ?? "one person"}'s inbox` : "all inboxes";

  const ask = (e: FormEvent) => {
    e.preventDefault();
    setDone(null);
    preview.mutate(range, { onSuccess: setAsked });
  };
  const confirm = () => run.mutate(range, { onSuccess: (n) => {
    setAsked(null);
    setDone(n === 0 ? "No connected inbox to re-sync." : `Re-sync started for ${plural(n, "inbox", "inboxes")}. Changes appear as each one finishes.`);
  } });

  return (
    <div className="admin-resync">
      <header className="page-head"><h1 className="page-title">Re-sync</h1></header>
      <p className="admin__lead">
        Fetches a range of emails from Gmail again and reads them with today's templates. Fields someone edited and
        values from a statement stay as they are; everything else takes the new reading.
      </p>
      <form className="admin-resync__form" onSubmit={ask}>
        <label className="field"><span className="field__label">From</span>
          <input className="field__input" type="date" value={start} max={end} required
            onChange={(e) => { setStart(e.target.value); setAsked(null); }} /></label>
        <label className="field"><span className="field__label">To</span>
          <input className="field__input" type="date" value={end} min={start} required
            onChange={(e) => { setEnd(e.target.value); setAsked(null); }} /></label>
        <label className="field"><span className="field__label">Inbox</span>
          <select className="field__input" value={who} onChange={(e) => { setWho(e.target.value); setAsked(null); }}>
            <option value="">All inboxes</option>
            {users.data?.map((u) => <option key={u.id} value={u.id}>{u.display_name}</option>)}
          </select></label>
        <div className="actions"><button className="button button--primary" type="submit" disabled={preview.isPending}>
          Re-sync…</button></div>
      </form>
      {preview.isError && <p className="form-error" role="alert">{describeError(preview.error)}</p>}
      {done && <p className="admin__done" role="status">{done}</p>}
      {asked && (
        <div className="admin-resync__confirm sheet sheet--ruled" role="dialog" aria-modal="true" aria-labelledby="resync-confirm-h">
          <h2 id="resync-confirm-h">
            Re-sync {formatDate(start)} to {formatDate(end)} for {scope}?
          </h2>
          <p className="admin-resync__count">
            {asked.inboxes === 0 ? "No connected inbox is in this choice."
              : `${plural(asked.transactions, "transaction", "transactions")} may change.`}
          </p>
          <p>Fields someone edited and values from a statement stay as they are. A backup is taken first. This rewrites
            data; restoring that backup is the only undo.</p>
          {run.isError && <p className="form-error" role="alert">{describeError(run.error)}</p>}
          <div className="actions">
            <button className="button button--quiet" type="button" onClick={() => setAsked(null)}>Cancel</button>
            <button className="button button--primary" type="button" disabled={run.isPending || asked.inboxes === 0}
              onClick={confirm}>Re-sync</button>
          </div>
        </div>
      )}
    </div>
  );
}
