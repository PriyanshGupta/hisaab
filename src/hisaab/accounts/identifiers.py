"""Canonical forms for account identifiers (PRD §3.3).

Identifiers are matched exactly, never fuzzily: a wrong account is worse than
no account. "Exactly" only works if both sides are reduced to the same form,
so normalization runs on write (registering an identifier) and on lookup (a
parser's raw string). Case is the classic trap: one bank's alert says
"UPI@YBL", the user typed "upi@ybl", and without this the match silently fails
for one family member and not another.

Forms:
  last4 / card_last4  the last 4 digits, ignoring masking: "XX5512", "**** 5512" -> "5512"
  vpa                 trimmed, lower-cased UPI ID: "  Asha@OKHDFC " -> "asha@okhdfc"
  other               trimmed, inner whitespace collapsed, lower-cased
"""

import re

_VPA = re.compile(r"[a-z0-9._-]+@[a-z0-9.-]+")
# Digits plus the masking characters banks print around them.
_MASKED_DIGITS = re.compile(r"[0-9Xx*•\-\s]+")


class IdentifierError(ValueError):
    """Raw input that cannot be an identifier of the given kind (message is user-facing)."""


def _last4(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    if len(digits) < 4:
        raise IdentifierError("Enter the last 4 digits of the account or card number.")
    return digits[-4:]


def _vpa(raw: str) -> str:
    value = raw.strip().lower()
    if not _VPA.fullmatch(value):
        raise IdentifierError(f"'{raw.strip()}' isn't a UPI ID. A UPI ID looks like name@bank.")
    return value


def _other(raw: str) -> str:
    value = " ".join(raw.split()).lower()
    if not value:
        raise IdentifierError("The identifier is empty.")
    return value


_BY_KIND = {"last4": _last4, "card_last4": _last4, "vpa": _vpa, "other": _other}


def normalize(kind: str, raw: str) -> str:
    """Canonical form of `raw` as an identifier of `kind`; raises IdentifierError if invalid."""
    if kind not in _BY_KIND:
        raise IdentifierError(f"Unknown identifier kind '{kind}'.")
    return _BY_KIND[kind](raw)


def normalize_for_lookup(raw: str) -> str | None:
    """Canonical form of a raw string whose kind is unknown (e.g. what a parser extracted).

    Contains '@' -> a UPI ID. Only digits and masking with at least 4 digits ->
    last 4 digits. Anything else -> 'other'. Returns None for input that can't
    be any identifier, so it simply matches nothing.
    """
    value = raw.strip()
    if not value:
        return None
    try:
        if "@" in value:
            return _vpa(value)
        if _MASKED_DIGITS.fullmatch(value) and len(re.sub(r"\D", "", value)) >= 4:
            return _last4(value)
        return _other(value)
    except IdentifierError:
        return None
