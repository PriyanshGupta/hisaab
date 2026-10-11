"""Arithmetic validation of a parsed statement (tech-design §4.8).

    bank:         opening + credits - debits = closing
    credit card:  opening + debits - credits = closing    (debt is positive, ruling 2026-10-10)

opening and closing are the statement's OWN declared figures, read by the
spec's declared_balances patterns. The check never infers a balance from the
rows (a running-balance column, the last row's balance): that compares the rows
with themselves and passes on corrupted input, the PRD §7.10 tautology. When
either declared figure is missing, the outcome is "unavailable", never a pass.

Outcomes: passed (|gap| <= tolerance, ₹1 by default per PRD §10.1), failed
(gap = declared closing - computed closing, so a positive gap means the rows
come to less than the statement says), unavailable.
"""

from hisaab.parsing import patterns
from hisaab.parsing.transforms import TransformError


def declared_balances(spec, text: str) -> dict:
    found = {"opening": None, "closing": None}
    for name, ex in spec.declared.items():
        try:
            m = patterns.search(ex.pattern, text)
            if m and m.group(1):
                found[name] = ex.transform(m.group(1))
        except (patterns.PatternTimeout, TransformError):
            pass
    return found


def validate(spec, text: str, result, *, template_id=None, template_name=None, tolerance_paise: int = 100) -> dict:
    declared = declared_balances(spec, text)
    credits = sum(r.amount for r in result.rows if r.direction == "credit")
    debits = sum(r.amount for r in result.rows if r.direction == "debit")
    computed_closing = gap = None
    outcome = "unavailable"
    if declared["opening"] is not None and declared["closing"] is not None:
        change = debits - credits if spec.account_kind == "credit_card" else credits - debits
        computed_closing = declared["opening"] + change
        gap = declared["closing"] - computed_closing
        outcome = "passed" if abs(gap) <= tolerance_paise else "failed"
    return {
        "outcome": outcome,
        "template_id": template_id, "template_name": template_name,
        "pages_processed": result.pages_processed,
        "rows_extracted": len(result.rows), "rows_skipped": result.rows_skipped,
        "rows_unparseable": len(result.unparseable),
        "declared": declared,
        "computed": {"credits": credits, "debits": debits, "closing": computed_closing},
        "gap_paise": gap,
        "unparseable_rows": result.unparseable,
    }
