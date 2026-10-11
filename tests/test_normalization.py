"""Raw payee strings to Payees through priority-ordered patterns (tech-design §6.4). Synthetic strings."""

import pytest

from hisaab.payees.normalization import PatternRule, match_payee

RULES: list[PatternRule] = [
    PatternRule(id="1", match_type="contains", pattern="Swiggy", payee_id="p-swiggy", priority=0),
    PatternRule(id="2", match_type="contains", pattern="Swiggy Instamart", payee_id="p-instamart", priority=10),
    PatternRule(id="3", match_type="exact", pattern="Tea Stall", payee_id="p-tea", priority=0),
    PatternRule(id="4", match_type="regex", pattern=r"^UPI-UBER", payee_id="p-uber", priority=0),
    PatternRule(id="5", match_type="contains", pattern="Best Buy", payee_id="p-bestbuy", priority=0),
]


@pytest.mark.parametrize(
    ("raw", "payee"),
    [
        ("SWIGGY BANGALORE", "p-swiggy"),
        ("RAZ*SWIGGY BANGALORE KAIN", "p-swiggy"),
        ("SWIGGY INSTAMART BLR", "p-instamart"),  # priority, not "longest match", picks it
        ("SwiggyInstamart", "p-instamart"),  # squashed PDF text still matches
        ("tea stall", "p-tea"),
        ("  Tea   Stall ", "p-tea"),
        ("Tea Stall near office", None),  # exact means exact
        ("UPI-UBERINDIASYSTEMS", "p-uber"),
        ("PAYMENT UPI-UBER", None),  # regex anchors are honoured
        ("BestBuySanfordUs", "p-bestbuy"),
        ("Corner shop", None),
        ("", None),
    ],
)
def test_match_payee(raw: str, payee: str | None) -> None:
    assert match_payee(RULES, raw) == payee


def test_equal_priorities_resolve_by_id_so_the_answer_never_depends_on_row_order() -> None:
    a: PatternRule = PatternRule(id="a", match_type="contains", pattern="zom", payee_id="p-a", priority=0)
    b: PatternRule = PatternRule(id="b", match_type="contains", pattern="zomato", payee_id="p-b", priority=0)
    assert match_payee([a, b], "ZOMATO") == match_payee([b, a], "ZOMATO") == "p-a"


def test_a_pattern_of_only_punctuation_never_matches_everything() -> None:
    dash: PatternRule = PatternRule(id="x", match_type="contains", pattern="--", payee_id="p-x", priority=0)
    assert match_payee([dash], "ANYTHING") is None
