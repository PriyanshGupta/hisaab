// Auto-categorize (slice 17): payees, not transactions. One click files the
// payees the model is confident about under the user's own categories, as
// rules. They appear in the lane's Sorted list like any other categorized
// payee, marked "auto-categorized" and changeable there; the whole batch can be
// undone here. The model's confidence decides what is applied but is never
// shown (user, 2026-10-19).
import { useState } from "react";

import { describeError } from "../api/client";
import { type AutoResult, useAutoCategorize, useLatestBatch, useUndoBatch } from "../api/review";

const payeesWord = (n: number) => `${n} ${n === 1 ? "payee" : "payees"}`;

export function AutoCategorize() {
  const run = useAutoCategorize();
  const latest = useLatestBatch();
  const undo = useUndoBatch();
  const [result, setResult] = useState<AutoResult | null>(null);
  const shown = result ?? (latest.data && latest.data.applied.length > 0 ? latest.data : null);

  return (
    <section className="auto" aria-label="Auto-categorize">
      <div className="auto__head">
        <button className="button button--secondary" type="button" disabled={run.isPending} aria-busy={run.isPending}
          onClick={() => run.mutate(undefined, { onSuccess: setResult })}>
          {run.isPending ? "Categorizing… this can take a minute or two" : "Auto-categorize"}
        </button>
        <span className="muted">Files payees under your categories where the match is clear. You can change or undo it.</span>
      </div>
      {run.isError && <p className="form-error" role="alert">{describeError(run.error)}</p>}
      {shown && <Outcome result={shown} fresh={shown === result}
        onUndo={() => shown.batch_id && undo.mutate(shown.batch_id, { onSuccess: () => setResult(null) })}
        undoing={undo.isPending} />}
      {undo.isError && <p className="form-error" role="alert">{describeError(undo.error)}</p>}
    </section>
  );
}

function Outcome({ result, fresh, onUndo, undoing }: {
  result: AutoResult; fresh: boolean; onUndo: () => void; undoing: boolean;
}) {
  const left = result.left > 0 ? ` ${result.left} ${result.left === 1 ? "is" : "are"} left for you — we couldn't categorize these.` : "";
  if (fresh && !result.called) return <p className="auto__summary">Nothing new to categorize.{left}</p>;
  return (
    <div className="auto__outcome">
      <p className="auto__summary">
        {`Categorized ${payeesWord(result.applied.length)} covering ${result.transactions} transactions.`}{left}
      </p>
      {result.applied.length > 0 && (
        <button className="button button--quiet button--small" type="button" disabled={undoing} onClick={onUndo}>
          Undo auto-categorize
        </button>
      )}
    </div>
  );
}
