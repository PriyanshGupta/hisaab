// Auto-categorize (slice 17): payees, not transactions. One click files the
// payees the model is confident about under the user's own categories, as
// rules. They appear in the lane's Sorted list like any other categorized
// payee, marked "auto-categorized" and changeable there; the whole batch can be
// undone here. The model's confidence decides what is applied but is never
// shown (user, 2026-10-19). Its outcome can be dismissed (user, 2026-10-21): the batch's id is remembered in this
// browser, so the same message stays hidden after a reload and the next run shows its own.
import { useState } from "react";

import { describeError } from "../api/client";
import { type AutoResult, useAutoCategorize, useLatestBatch, useUndoBatch } from "../api/review";

const payeesWord = (n: number) => `${n} ${n === 1 ? "payee" : "payees"}`;
const DISMISSED_KEY = "hisaab.review.dismissedAutoBatch";

function readDismissed(): string | null {
  try {
    return localStorage.getItem(DISMISSED_KEY);
  } catch {
    return null;  // storage blocked: the message just shows again
  }
}

function writeDismissed(batchId: string) {
  try {
    localStorage.setItem(DISMISSED_KEY, batchId);
  } catch {
    // storage blocked: dismissed for this visit only
  }
}

export function AutoCategorize() {
  const run = useAutoCategorize();
  const latest = useLatestBatch();
  const undo = useUndoBatch();
  const [result, setResult] = useState<AutoResult | null>(null);
  const [dismissed, setDismissed] = useState<string | null>(readDismissed);
  const stored = latest.data && latest.data.applied.length > 0 && latest.data.batch_id !== dismissed ? latest.data : null;
  const shown = result ?? stored;
  const dismiss = () => {
    if (shown?.batch_id) {
      writeDismissed(shown.batch_id);
      setDismissed(shown.batch_id);
    }
    setResult(null);
  };

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
        undoing={undo.isPending} onDismiss={dismiss} />}
      {undo.isError && <p className="form-error" role="alert">{describeError(undo.error)}</p>}
    </section>
  );
}

function Outcome({ result, fresh, onUndo, undoing, onDismiss }: {
  result: AutoResult; fresh: boolean; onUndo: () => void; undoing: boolean; onDismiss: () => void;
}) {
  const left = result.left > 0 ? ` ${result.left} ${result.left === 1 ? "is" : "are"} left for you — we couldn't categorize these.` : "";
  const nothing = fresh && !result.called;
  return (
    <div className="auto__outcome">
      <p className="auto__summary">
        {nothing ? "Nothing new to categorize." : `Categorized ${payeesWord(result.applied.length)} covering ${result.transactions} transactions.`}
        {left}
      </p>
      <span className="auto__actions">
        {!nothing && result.applied.length > 0 && (
          <button className="button button--quiet button--small" type="button" disabled={undoing} onClick={onUndo}>
            Undo auto-categorize
          </button>
        )}
        <button className="button button--quiet button--small" type="button" onClick={onDismiss}>Dismiss</button>
      </span>
    </div>
  );
}
