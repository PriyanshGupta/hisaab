"""Reading an uploaded statement (tech-design §4.7 steps 1-8, §7 steps 1-3).

The PDF's bytes and password exist only for the length of this call: nothing
here writes, and neither is kept (PRD §4.6, §3.7: the parsed rows are what the
preview and the merge use). Every refusal is a StatementError whose message is
a sentence for the person, and none of them repeats the password.

Unknown layouts are refused rather than sent to the model: statement tier 3 is
its own piece of work after slice 19 (ruling 2026-10-19).
"""

from dataclasses import dataclass

from sqlalchemy.orm import Session

from hisaab.accounts.repository import resolve_identifier
from hisaab.models import Account
from hisaab.parsing.pdf import Page, PdfError, read_pdf
from hisaab.parsing.statement_interpreter import (
    StatementResult,
    StatementTemplate,
    TemplateMismatch,
    interpret_statement,
    pick_statement_template,
)
from hisaab.parsing.templates import statement_candidates

ACCOUNT_KIND: dict[str, str] = {"bank": "bank", "credit_card": "credit_card"}  # account type -> spec account_kind


class StatementError(ValueError):
    """The statement can't be read or staged (message is user-facing)."""


@dataclass(frozen=True)
class ReadStatement:
    template: StatementTemplate
    result: StatementResult  # rows, unparseable rows, and the §4.8 diagnostic in result.validation
    account_id: str | None  # bound from the header; None: the preview asks which account


def read_statement(session: Session, user_id: str, data: bytes, password: str | None) -> ReadStatement:
    try:
        pages: list[Page] = read_pdf(data, password)
    except PdfError as e:
        raise StatementError(str(e)) from None  # pdf.py's sentences; the password is never in them
    if not any(page.lines for page in pages):
        raise StatementError("This PDF has no text Hisaab can read (it may be a scan). Add its transactions by hand.")
    template: StatementTemplate | None = pick_statement_template(statement_candidates(session), pages)
    if template is None:
        raise StatementError("No statement layout Hisaab knows matches this file.")
    try:
        result: StatementResult = interpret_statement(template.spec, pages, template_id=template.id,
                                                      template_name=template.name)
    except TemplateMismatch as e:
        raise StatementError(f"The “{template.name}” layout didn't fit this file: {e}.") from None
    account: Account | None = resolve_identifier(session, user_id, result.account[1]) if result.account else None
    if account is not None and ACCOUNT_KIND.get(account.type) != template.spec.account_kind:
        account = None  # e.g. a card whose last 4 equal a bank statement's account: ask, never guess
    return ReadStatement(template, result, account.id if account else None)
