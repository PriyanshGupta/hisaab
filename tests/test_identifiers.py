"""Identifier normalization: the same canonical form on write and on lookup."""

import pytest

from hisaab.accounts.identifiers import IdentifierError, normalize, normalize_for_lookup


@pytest.mark.parametrize("kind,raw,expected", [
    ("last4", "5512", "5512"),
    ("last4", "XX5512", "5512"),
    ("last4", "**** 5512", "5512"),
    ("last4", "xxxx-xxxx-5512", "5512"),
    ("card_last4", "4321 0000 1234 8830", "8830"),
    ("vpa", "UPI@YBL", "upi@ybl"),
    ("vpa", "  Asha.K@okHDFCbank ", "asha.k@okhdfcbank"),
    ("vpa", "9876543210@paytm", "9876543210@paytm"),
    ("other", "  HDFC   Bank  Loan ", "hdfc bank loan"),
])
def test_normalize_on_write(kind: str, raw: str, expected: str) -> None:
    assert normalize(kind, raw) == expected


@pytest.mark.parametrize("kind,raw", [
    ("last4", "940"),          # fewer than 4 digits
    ("last4", "abcd"),
    ("card_last4", ""),
    ("vpa", "no-at-sign"),
    ("vpa", "two@@signs"),
    ("vpa", "spa ce@ybl"),
    ("vpa", "@ybl"),
    ("other", "   "),
    ("bogus", "x"),
])
def test_normalize_rejects_invalid(kind: str, raw: str) -> None:
    with pytest.raises(IdentifierError):
        normalize(kind, raw)


@pytest.mark.parametrize("raw,expected", [
    ("UPI@YBL", "upi@ybl"),            # case: the most likely cross-user bug
    ("  upi@ybl\n", "upi@ybl"),
    ("XX5512", "5512"),
    ("5512", "5512"),
    ("**** **** **** 8830", "8830"),
    ("HDFC Bank Loan", "hdfc bank loan"),
    ("ending 6203", "6203"),                     # a model's reading of "card ending 6203": the same card as XX6203
    ("Card no. XX1234", "1234"),
    ("FASTag Wallet 90000012345678", "5678"),    # words before an account number: its last 4, like the bare number
    ("Example Bank Credit Card", "example bank credit card"),
    ("Loan 123", "loan 123"),                    # fewer than 4 digits: not a number to match on
])
def test_lookup_form_matches_write_form(raw: str, expected: str) -> None:
    assert normalize_for_lookup(raw) == expected


def test_an_other_identifier_ending_in_a_number_is_stored_as_lookup_finds_it() -> None:
    assert normalize("other", "FASTag Wallet 90000012345678") == normalize_for_lookup("90000012345678") == "5678"


def test_lookup_of_garbage_returns_none() -> None:
    assert normalize_for_lookup("   ") is None
