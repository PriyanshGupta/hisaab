"""Reading an uploaded statement (tech-design §4.7 steps 1-8, §7 steps 1-3).

The PDF's bytes and password exist only for the length of this call: nothing
here writes, and neither is kept (PRD §4.6, §3.7: the parsed rows are what the
preview and the merge use). Every refusal is a StatementError whose message is
a sentence for the person, and none of them repeats the password.

A layout no template detects, or one a template detects but whose columns don't
fit (§4.7), is read by the model (statements/tier3.py, ruling 2026-10-19). A known
layout never calls it.
"""

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.orm import Session

from hisaab.accounts.repository import resolve_identifier
from hisaab.config import Settings
from hisaab.llm import ContractError, LLMUnavailable, Transport
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
from hisaab.statements.tier3 import ModelRead, NotAStatement, read_with_model

ACCOUNT_KIND: dict[str, str] = {"bank": "bank", "credit_card": "credit_card"}  # account type -> spec account_kind


class StatementError(ValueError):
    """The statement can't be read or staged (message is user-facing)."""


@dataclass(frozen=True)
class ReadStatement:
    template: StatementTemplate | None  # None: read by the model (tier 3)
    result: StatementResult  # rows, unparseable rows, and the §4.8 diagnostic in result.validation
    account_id: str | None  # bound from the header; None: the preview asks which account
    account_kind: str  # bank | credit_card: the template's, or the model's reading


def read_statement(session: Session, user_id: str, data: bytes, password: str | None, *, settings: Settings,
                   transport: Transport | None, sleep: Callable[[float], None]) -> ReadStatement:
    try:
        pages: list[Page] = read_pdf(data, password)
    except PdfError as e:
        raise StatementError(str(e)) from None  # pdf.py's sentences; the password is never in them
    if not any(page.lines for page in pages):
        raise StatementError("This PDF has no text Hisaab can read (it may be a scan). Add its transactions by hand.")
    template: StatementTemplate | None = pick_statement_template(statement_candidates(session), pages)
    if template is not None:
        try:
            result: StatementResult = interpret_statement(template.spec, pages, template_id=template.id,
                                                          template_name=template.name)
            return _bound(session, user_id, template, result, result.account, template.spec.account_kind)
        except TemplateMismatch as e:  # §4.7: a template that doesn't fit falls through to tier 3
            read: ModelRead = _model(settings, pages, transport, sleep, {"template": template.name, "reason": str(e)})
            return _bound(session, user_id, None, read.result, read.account, read.account_kind)
    read = _model(settings, pages, transport, sleep, None)
    return _bound(session, user_id, None, read.result, read.account, read.account_kind)


def _model(settings: Settings, pages: list[Page], transport: Transport | None, sleep: Callable[[float], None],
           tried: dict[str, str] | None) -> ModelRead:
    try:
        return read_with_model(settings, pages, transport=transport, sleep=sleep, tried=tried)
    except LLMUnavailable as e:
        raise StatementError("This statement's layout isn't one Hisaab knows, and the model that reads new layouts "
                             f"isn't available right now ({e}). Try again later.") from None
    except ContractError as e:  # the field name only: the rest may quote the statement
        raise StatementError(f"The model's reading of this statement couldn't be used ({str(e).split(':', 1)[0]}). "
                             "Nothing was saved.") from None
    except NotAStatement as e:
        raise StatementError(str(e)) from None


def _bound(session: Session, user_id: str, template: StatementTemplate | None, result: StatementResult,
           identifier: tuple[str, str] | None, kind: str) -> ReadStatement:
    account: Account | None = resolve_identifier(session, user_id, identifier[1]) if identifier else None
    if account is not None and ACCOUNT_KIND.get(account.type) != kind:
        account = None  # e.g. a card whose last 4 equal a bank statement's account: ask, never guess
    return ReadStatement(template, result, account.id if account else None, kind)
