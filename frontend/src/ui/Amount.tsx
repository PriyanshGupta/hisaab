// An amount with its direction plain to see (user, 2026-10-21): money in (a credit) is green and tagged CR, the
// mark bank statements use, and a screen reader hears "credit"; money out stays ink. One component, so the
// Transactions list, Review and a statement preview all say it the same way.
import { formatPaise } from "../money";

export function Amount({ paise, credit = false }: { paise: number; credit?: boolean }) {
  return (
    <span className={credit ? "amt amt--credit" : "amt"}>
      {formatPaise(paise)}
      {credit && (
        <>
          <span className="cr" aria-hidden="true">CR</span>
          <span className="visually-hidden"> credit</span>
        </>
      )}
    </span>
  );
}
