"""Leg validity per kind: PRD §3.2, amended 2026-10-11 so refunds may land in cash.

A leg is an account type ("bank" / "credit_card" / "cash"), UNRESOLVED (an
identifier review will resolve: some internal account), or None (external).
"""

import pytest

from hisaab.transactions.legs import UNRESOLVED, LegError, validate_legs

BANK, CARD, CASH = "bank", "credit_card", "cash"


def ok(kind: str, from_leg: str | None, to_leg: str | None, same_account: bool = False) -> None:
    validate_legs(kind, from_leg, to_leg, same_account=same_account)


def bad(kind: str, from_leg: str | None, to_leg: str | None, same_account: bool = False) -> None:
    with pytest.raises(LegError):
        validate_legs(kind, from_leg, to_leg, same_account=same_account)


# expense: bank / card / cash -> external
@pytest.mark.parametrize("src", [BANK, CARD, CASH, UNRESOLVED])
def test_expense_from_any_internal_to_external(src: str | None) -> None:
    ok("expense", src, None)


@pytest.mark.parametrize("from_leg,to_leg", [(None, None), (BANK, CASH), (None, BANK)])
def test_expense_needs_an_internal_from_and_external_to(from_leg: str | None, to_leg: str | None) -> None:
    bad("expense", from_leg, to_leg)


# income: external -> bank / cash (never a card: card credits are refunds, §3.8)
@pytest.mark.parametrize("dst", [BANK, CASH, UNRESOLVED])
def test_income_from_external_into_bank_or_cash(dst: str | None) -> None:
    ok("income", None, dst)


@pytest.mark.parametrize("from_leg,to_leg", [(None, CARD), (BANK, CASH), (None, None)])
def test_income_rejects_card_internal_source_or_no_destination(from_leg: str | None, to_leg: str | None) -> None:
    bad("income", from_leg, to_leg)


# transfer: internal -> a different internal account; the paying account may be
# unknown (None): a card payment received from an account the app can't see (ruling 2026-10-11)
@pytest.mark.parametrize("from_leg,to_leg", [(BANK, CARD), (BANK, CASH), (CASH, BANK), (UNRESOLVED, CARD), (None, CARD),
                                             (None, BANK)])
def test_transfer_between_two_internal_accounts(from_leg: str | None, to_leg: str | None) -> None:
    ok("transfer", from_leg, to_leg)


@pytest.mark.parametrize("from_leg,to_leg,same", [(BANK, None, False), (None, None, False), (BANK, BANK, True)])
def test_transfer_needs_a_destination_distinct_from_its_source(from_leg: str | None, to_leg: str | None, same: bool) -> None:
    bad("transfer", from_leg, to_leg, same_account=same)


# refund: external -> bank / card / cash (cash added by ruling)
@pytest.mark.parametrize("dst", [BANK, CARD, CASH, UNRESOLVED])
def test_refund_from_external_into_bank_card_or_cash(dst: str | None) -> None:
    ok("refund", None, dst)


@pytest.mark.parametrize("from_leg,to_leg", [(BANK, None), (BANK, CARD), (None, None)])
def test_refund_must_come_from_outside_into_an_account(from_leg: str | None, to_leg: str | None) -> None:
    bad("refund", from_leg, to_leg)


# adjustment: external -> any internal account
@pytest.mark.parametrize("dst", [BANK, CARD, CASH])
def test_adjustment_from_external_into_any_account(dst: str | None) -> None:
    ok("adjustment", None, dst)


@pytest.mark.parametrize("from_leg,to_leg", [(BANK, None), (None, None)])
def test_adjustment_needs_only_a_destination_account(from_leg: str | None, to_leg: str | None) -> None:
    bad("adjustment", from_leg, to_leg)


# lending: reserved in the schema, not implemented in v0
def test_lending_is_refused() -> None:
    with pytest.raises(LegError, match="not supported"):
        validate_legs("lending", BANK, None)


def test_unknown_kind_is_refused() -> None:
    bad("gift", BANK, None)
