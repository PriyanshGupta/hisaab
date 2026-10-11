"""Which accounts a transaction of each kind may move money between (PRD §3.2).

A transaction has a from-leg and a to-leg. Each is an account (named here by
its type), UNRESOLVED (an identifier the parser saw but no account matched yet;
review will resolve it to one of the user's own accounts), or None (external:
outside the user's net worth).

    kind        from                 to
    expense     bank / card / cash   external
    income      external             bank / cash
    transfer    internal             internal, a different account
    refund      external             bank / card / cash   (cash: ruling 2026-10-11)
    adjustment  external             any internal account
    lending     schema only in v0: refused

Income never lands on a card: card credits (cashback, rewards) are refunds (§3.8).
The database enforces only the transfer rule (a CHECK); this is the rest.
"""

UNRESOLVED = "unresolved"
INTERNAL = {"bank", "credit_card", "cash", UNRESOLVED}

_RULES = {  # kind -> (allowed from-legs, allowed to-legs); None means external
    "expense": (INTERNAL, {None}),
    "income": ({None}, {"bank", "cash", UNRESOLVED}),
    "transfer": (INTERNAL, INTERNAL),
    "refund": ({None}, {"bank", "credit_card", "cash", UNRESOLVED}),
    "adjustment": ({None}, INTERNAL),
}

_DESCRIBE = {
    "expense": "An expense is paid from one of your accounts to someone outside.",
    "income": "Income comes from outside into a bank account or cash.",
    "transfer": "A transfer moves money between two different accounts of yours.",
    "refund": "A refund comes from outside into a bank account, card or cash.",
    "adjustment": "An adjustment goes into one of your accounts.",
}


class LegError(ValueError):
    """The accounts don't fit the kind (message is user-facing)."""


def validate_legs(kind: str, from_leg: str | None, to_leg: str | None, *, same_account: bool = False) -> None:
    if kind == "lending":
        raise LegError("Lending is not supported yet; record it as an expense.")
    if kind not in _RULES:
        raise LegError(f"Unknown kind '{kind}'.")
    allowed_from, allowed_to = _RULES[kind]
    if from_leg not in allowed_from or to_leg not in allowed_to or (kind == "transfer" and same_account):
        raise LegError(_DESCRIBE[kind])
