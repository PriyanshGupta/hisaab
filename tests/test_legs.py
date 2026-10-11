"""Leg validity per kind: PRD §3.2, amended 2026-10-11 so refunds may land in cash.

A leg is an account type ("bank" / "credit_card" / "cash"), UNRESOLVED (an
identifier review will resolve: some internal account), or None (external).
"""

import pytest

from hisaab.transactions.legs import UNRESOLVED, LegError, validate_legs

BANK, CARD, CASH = "bank", "credit_card", "cash"


def ok(kind, from_leg, to_leg, same_account=False):
    validate_legs(kind, from_leg, to_leg, same_account=same_account)


def bad(kind, from_leg, to_leg, same_account=False):
    with pytest.raises(LegError):
        validate_legs(kind, from_leg, to_leg, same_account=same_account)


# expense: bank / card / cash -> external
@pytest.mark.parametrize("src", [BANK, CARD, CASH, UNRESOLVED])
def test_expense_from_any_internal_to_external(src):
    ok("expense", src, None)


@pytest.mark.parametrize("from_leg,to_leg", [(None, None), (BANK, CASH), (None, BANK)])
def test_expense_needs_an_internal_from_and_external_to(from_leg, to_leg):
    bad("expense", from_leg, to_leg)


# income: external -> bank / cash (never a card: card credits are refunds, §3.8)
@pytest.mark.parametrize("dst", [BANK, CASH, UNRESOLVED])
def test_income_from_external_into_bank_or_cash(dst):
    ok("income", None, dst)


@pytest.mark.parametrize("from_leg,to_leg", [(None, CARD), (BANK, CASH), (None, None)])
def test_income_rejects_card_internal_source_or_no_destination(from_leg, to_leg):
    bad("income", from_leg, to_leg)


# transfer: internal -> a different internal account
@pytest.mark.parametrize("from_leg,to_leg", [(BANK, CARD), (BANK, CASH), (CASH, BANK), (UNRESOLVED, CARD)])
def test_transfer_between_two_internal_accounts(from_leg, to_leg):
    ok("transfer", from_leg, to_leg)


@pytest.mark.parametrize("from_leg,to_leg,same", [(BANK, None, False), (None, BANK, False), (BANK, BANK, True)])
def test_transfer_needs_two_distinct_internal_legs(from_leg, to_leg, same):
    bad("transfer", from_leg, to_leg, same_account=same)


# refund: external -> bank / card / cash (cash added by ruling)
@pytest.mark.parametrize("dst", [BANK, CARD, CASH, UNRESOLVED])
def test_refund_from_external_into_bank_card_or_cash(dst):
    ok("refund", None, dst)


@pytest.mark.parametrize("from_leg,to_leg", [(BANK, None), (BANK, CARD), (None, None)])
def test_refund_must_come_from_outside_into_an_account(from_leg, to_leg):
    bad("refund", from_leg, to_leg)


# adjustment: external -> any internal account
@pytest.mark.parametrize("dst", [BANK, CARD, CASH])
def test_adjustment_from_external_into_any_account(dst):
    ok("adjustment", None, dst)


@pytest.mark.parametrize("from_leg,to_leg", [(BANK, None), (None, None)])
def test_adjustment_needs_only_a_destination_account(from_leg, to_leg):
    bad("adjustment", from_leg, to_leg)


# lending: reserved in the schema, not implemented in v0
def test_lending_is_refused():
    with pytest.raises(LegError, match="not supported"):
        validate_legs("lending", BANK, None)


def test_unknown_kind_is_refused():
    bad("gift", BANK, None)
