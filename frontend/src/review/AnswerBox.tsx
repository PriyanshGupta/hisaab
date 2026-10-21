// The review queue's one input: a type-ahead list, keyboard first. Typing
// filters, ↑/↓ move, Enter picks the highlighted option, Esc closes. A
// suggestion (the payee text exactly as shown) leads the list until the user
// types, so the common answer is one Enter. Options added at the end
// ("Create …", "Add an account …") are always offered.
import { type KeyboardEvent, useId, useState } from "react";

export type Option = { id: string; label: string };

export function AnswerBox({ label, options, onPick, onClose, create, suggested, extras = [], note }: {
  label: string;
  options: Option[];
  onPick: (option: Option, typed: string) => void;
  onClose: () => void;
  create?: (typed: string) => Option; // offered when the text matches nothing exactly
  suggested?: Option | undefined; // first, until the user types
  extras?: Option[];
  note?: string | undefined;
}) {
  const id = useId();
  const [text, setText] = useState("");
  const [active, setActive] = useState(0);
  const typed = text.trim();
  const shown = options.filter((o) => o.label.toLowerCase().includes(typed.toLowerCase()));
  const exact = shown.some((o) => o.label.toLowerCase() === typed.toLowerCase());
  const all = [...(suggested && !typed ? [suggested] : []), ...shown,
    ...(create && typed && !exact ? [create(typed)] : []), ...extras];
  const current = Math.min(active, all.length - 1);

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    e.stopPropagation(); // the list's own keys (arrows, Enter, Space, 1–4) stay out of the box
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      setActive((i) => Math.max(0, Math.min(all.length - 1, i + (e.key === "ArrowDown" ? 1 : -1))));
    } else if (e.key === "Enter") {
      e.preventDefault();
      const pick = all[current];
      if (pick) onPick(pick, typed);
    } else if (e.key === "Escape") {
      e.preventDefault();
      onClose();
    }
  };

  return (
    <div className="answer">
      {/* Cancel beside the search: leaving without an answer must not need the Escape key (no keyboard on a phone) */}
      <div className="answer__bar">
        <input className="field__input answer__input" role="combobox" aria-label={label} aria-expanded="true"
          aria-controls={`${id}-list`} aria-autocomplete="list"
          aria-activedescendant={all[current] ? `${id}-${current}` : undefined} autoFocus autoComplete="off"
          spellCheck={false} value={text} placeholder="Type to search"
          onChange={(e) => { setText(e.target.value); setActive(0); }} onKeyDown={onKeyDown} />
        <button className="button button--secondary" type="button" onClick={onClose}>Cancel</button>
      </div>
      {note && <p className="answer__note">{note}</p>}
      <ul className="answer__list" role="listbox" id={`${id}-list`} aria-label={label}>
        {all.map((o, i) => (
          <li key={`${o.id}-${i}`} id={`${id}-${i}`} role="option" aria-selected={i === current}
            className="answer__option" onMouseEnter={() => setActive(i)} onClick={() => onPick(o, typed)}>
            {o.label}
          </li>
        ))}
        {all.length === 0 && <li className="answer__empty" role="presentation">No match</li>}
      </ul>
    </div>
  );
}
