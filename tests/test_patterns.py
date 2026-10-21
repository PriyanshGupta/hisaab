"""Template patterns run on google-re2 with save-time checks and a per-match timeout (tech-design §4.5)."""

import time

import pytest
from re2 import _Regexp

from hisaab.parsing import patterns
from hisaab.parsing.patterns import Match, PatternError, PatternTimeout, compile_pattern, search


def test_a_normal_pattern_compiles_and_search_returns_the_capture() -> None:
    compiled: _Regexp = compile_pattern(r"ending\s+(\d{4})", groups=1)
    found: Match | None = search(compiled, "account ending 1234 today")
    assert found is not None and found.group(1) == "1234"


def test_no_match_is_none() -> None:
    assert search(compile_pattern(r"(\d+)"), "no digits here") is None


@pytest.mark.parametrize(
    ("pattern", "problem"),
    [
        (r"(?=x)(a)", "RE2 cannot compile it"),  # lookahead: the backtracking hazard RE2 refuses
        (r"(a)\1", "RE2 cannot compile it"),  # backreference
        (r"(a", "RE2 cannot compile it"),
        ("", "empty"),
        ("(" + "a" * 499 + ")", "limit is 500"),
    ],
)
def test_unsafe_or_broken_patterns_are_rejected_at_save_time(pattern: str, problem: str) -> None:
    with pytest.raises(PatternError, match=problem):
        compile_pattern(pattern)


def test_the_length_cap_is_inclusive() -> None:
    assert compile_pattern("(" + "a" * 498 + ")")  # exactly 500 characters


def test_a_field_pattern_needs_a_capture_group() -> None:
    with pytest.raises(PatternError, match="capture group"):
        compile_pattern(r"ending \d{4}", groups=1)


def test_catastrophic_backtracking_shape_runs_in_linear_time() -> None:
    # (a+)+b against a run of a's takes exponential time in Python's re; RE2 shrugs.
    started: float = time.perf_counter()
    assert search(compile_pattern(r"(a+)+b"), "a" * 40) is None
    assert time.perf_counter() - started < 0.05


def test_a_match_that_overruns_its_deadline_raises_instead_of_blocking() -> None:
    huge: str = "a" * 20_000_000  # ~40 ms of linear work for RE2
    started: float = time.perf_counter()
    with pytest.raises(PatternTimeout):
        search(compile_pattern(r"(a+)+b"), huge, timeout=0.001)
    assert time.perf_counter() - started < 0.03  # the caller got control back before RE2 finished


def test_the_default_deadline_is_100ms() -> None:
    assert patterns.MATCH_TIMEOUT_SECONDS == 0.1


def test_matches_is_a_plain_yes_or_no_and_a_timeout_counts_as_no(monkeypatch: pytest.MonkeyPatch) -> None:
    assert patterns.matches(compile_pattern(r"Debit"), "Debit alert") is True
    assert patterns.matches(compile_pattern(r"Credit"), "Debit alert") is False
    monkeypatch.setattr(patterns, "MATCH_TIMEOUT_SECONDS", 0.001)
    assert patterns.matches(compile_pattern(r"(a+)+b"), "a" * 20_000_000) is False
