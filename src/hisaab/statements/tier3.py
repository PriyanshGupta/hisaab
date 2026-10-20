"""Tier 3 (tech-design §4.7, §4.11): a statement no template reads, read by the model and held to the PDF.

    ask       every page as column-kept text (pdf.layout_text) -> llm.extract_statement, validated per row
    hold      a row's amount and date must be printed on the page the model names; the declared opening
              and closing must both be printed somewhere in the statement. Otherwise the row can't be read (it shows, with the reason,
              in the preview's third group and is never merged) and the balance is dropped, so the check is
              "unavailable", never a pass on figures the model produced (PRD §7.10; ruling 2026-10-19)
    check     the same §4.8 arithmetic as a template (validation.arithmetic)

The result is an ordinary StatementResult: staging, preview and merge treat it like a template's.
Nothing is cached: every upload is a deliberate re-parse (amendment 7, ruling 2026-10-19).
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from hisaab.config import Settings
from hisaab.llm import Transport, extract_statement
from hisaab.parsing.pdf import Page, layout_text
from hisaab.parsing.statement_interpreter import StatementResult, StatementRow, UnparseableRow
from hisaab.parsing.validation import arithmetic

# 1,24,500.00 / 124,500.00 / 124500.00, also with a Cr/Dr marker attached ("500.00Cr": no word boundary there)
_AMOUNT: re.Pattern[str] = re.compile(r"\d[\d,]*\.\d{2}(?!\d)")
_MONTHS: tuple[str, ...] = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")


class NotAStatement(Exception):
    """The model says this file isn't a bank or card statement (message says why)."""


@dataclass(frozen=True)
class ModelRead:
    result: StatementResult
    account_kind: str  # bank | credit_card
    account: tuple[str, str] | None  # (identifier kind, last 4) for the registry; None: ask the user


def _printed(text: str) -> set[int]:
    """Every amount printed in the text, in paise."""
    found: set[int] = set()
    for m in _AMOUNT.finditer(text):
        rupees, paise = m.group(0).replace(",", "").split(".")
        found.add(int(rupees) * 100 + int(paise))
    return found


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


def _date_forms(iso: str) -> set[str]:
    """The ways an Indian statement prints this date, squashed (no spaces, lower case) like the page text."""
    year, month, day = (int(x) for x in iso.split("-"))
    mon: str = _MONTHS[month - 1]
    forms: set[str] = set()
    for d in {f"{day:02d}", str(day)}:
        for y in (f"{year % 100:02d}", str(year)):
            for m in {f"{month:02d}", str(month)}:
                forms |= {f"{d}/{m}/{y}", f"{d}-{m}-{y}", f"{d}.{m}.{y}"}
            forms |= {f"{d}{mon}{y}", f"{d}-{mon}-{y}", f"{d}/{mon}/{y}"}
        forms.add(f"{mon}{d},{year}")
    return forms


def _rupees(paise: int) -> str:
    return f"{paise // 100:,}.{paise % 100:02d}"


def read_with_model(settings: Settings, pages: list[Page], *, transport: Transport | None,
                    sleep: Callable[[float], None], tried: dict[str, str] | None = None) -> ModelRead:
    """Raises NotAStatement, or llm.LLMUnavailable / llm.ContractError from the call."""
    answer: dict[str, Any] = extract_statement(settings, [layout_text(p) for p in pages], transport=transport,
                                               sleep=sleep)
    if answer["outcome"] == "not_a_statement":
        raise NotAStatement("This file doesn't look like a bank or card statement.")
    kind: str | None = answer["account_kind"]
    if kind is None:
        raise NotAStatement("This file doesn't say whether it's a bank or a card statement.")
    printed: dict[int, set[int]] = {p.number: _printed(p.text) for p in pages}
    squashed: dict[int, str] = {p.number: _squash(p.text) for p in pages}
    anywhere: set[int] = set().union(*printed.values())
    rows: list[StatementRow] = []
    unparseable: list[UnparseableRow] = []
    for r in answer["rows"]:
        cells: list[str] = [r["date"], r["narration"], _rupees(r["amount"])]
        if r["amount"] not in printed[r["page"]]:
            unparseable.append({"page": r["page"], "raw_cells": cells,
                                "reason": f"the amount {_rupees(r['amount'])} isn't printed on page {r['page']}"})
            continue
        if not any(form in squashed[r["page"]] for form in _date_forms(r["date"])):
            # a day/month swap or a wrong year passes the amounts and the arithmetic, and misdates the row
            unparseable.append({"page": r["page"], "raw_cells": cells,
                                "reason": f"the date {r['date']} isn't printed on page {r['page']}"})
            continue
        rows.append(StatementRow(page=r["page"], date=r["date"], time=f"{r['time']}:00" if r["time"] else None,
                                 narration=r["narration"], reference=r["reference"], direction=r["direction"],
                                 amount=r["amount"], balance=r["balance"],
                                 payment=r["bill_payment"] and kind == "credit_card" and r["direction"] == "credit"))
    unparseable += [{"page": u["page"], "raw_cells": [u["text"]], "reason": u["reason"]} for u in answer["unreadable"]]
    # Kept only as a pair, both printed: a lone figure could be any number on the page (minimum due, a running
    # balance), and a closing balance on its own would still become a balance observation at merge.
    both_printed: bool = answer["opening"] in anywhere and answer["closing"] in anywhere
    declared: dict[str, int | None] = {n: answer[n] if both_printed else None for n in ("opening", "closing")}
    result: StatementResult = StatementResult(None, rows, unparseable, rows_skipped=0, pages_processed=len(pages))
    result.validation = {**arithmetic(kind, declared, result), "tier": 3, "model": settings.statement_model,
                         "institution": answer["institution"],
                         "declared_dropped": [n for n in ("opening", "closing")
                                              if answer[n] is not None and declared[n] is None],
                         **({"template_tried": tried} if tried else {})}
    last4: str | None = answer["account_last4"]
    account: tuple[str, str] | None = (("card_last4" if kind == "credit_card" else "last4"), last4) if last4 else None
    return ModelRead(result, kind, account)
