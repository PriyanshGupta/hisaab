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

from typing import TYPE_CHECKING, Any

from hisaab.parsing import patterns
from hisaab.parsing.statement_spec import StatementSpec
from hisaab.parsing.transforms import TransformError

if TYPE_CHECKING:  # statement_interpreter imports this module; annotations are lazy, so no cycle at runtime
    from hisaab.parsing.statement_interpreter import StatementResult


def declared_balances(spec: StatementSpec, text: str) -> dict[str, int | None]:
    found: dict[str, int | None] = {"opening": None, "closing": None}
    for name, ex in spec.declared.items():
        try:
            m: patterns.Match | None = patterns.search(ex.pattern, text)
            captured: str | None = m.group(1) if m else None
            value: object = ex.transform(captured) if captured else None
            if isinstance(value, int):  # inr_to_paise, enforced by statement_spec
                found[name] = value
        except (patterns.PatternTimeout, TransformError):
            pass
    return found


def validate(spec: StatementSpec, text: str, result: StatementResult, *, template_id: str | None = None,
             template_name: str | None = None, tolerance_paise: int = 100) -> dict[str, Any]:
    return arithmetic(spec.account_kind, declared_balances(spec, text), result, template_id=template_id,
                      template_name=template_name, tolerance_paise=tolerance_paise)


def arithmetic(account_kind: str, declared: dict[str, int | None], result: StatementResult, *,
               template_id: str | None = None, template_name: str | None = None,
               tolerance_paise: int = 100) -> dict[str, Any]:
    """The check and its diagnostic, given the statement's declared balances however they were read
    (a template's patterns, or the model's reading held to the printed text in tier 3)."""
    credits: int = sum(r.amount for r in result.rows if r.direction == "credit")
    debits: int = sum(r.amount for r in result.rows if r.direction == "debit")
    computed_closing: int | None = None
    gap: int | None = None
    outcome: str = "unavailable"
    if declared["opening"] is not None and declared["closing"] is not None:
        change: int = debits - credits if account_kind == "credit_card" else credits - debits
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
