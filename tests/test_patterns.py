"""Template patterns run on google-re2 with save-time checks and a per-match timeout (tech-design §4.5)."""

import time

import pytest

from hisaab.parsing import patterns
from hisaab.parsing.patterns import PatternError, PatternTimeout, compile_pattern, search


def test_a_normal_pattern_compiles_and_search_returns_the_capture():
    compiled = compile_pattern(r"ending\s+(\d{4})", groups=1)
    assert search(compiled, "account ending 1234 today").group(1) == "1234"


def test_no_match_is_none():
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
def test_unsafe_or_broken_patterns_are_rejected_at_save_time(pattern, problem):
    with pytest.raises(PatternError, match=problem):
        compile_pattern(pattern)


def test_the_length_cap_is_inclusive():
    assert compile_pattern("(" + "a" * 498 + ")")  # exactly 500 characters


def test_a_field_pattern_needs_a_capture_group():
    with pytest.raises(PatternError, match="capture group"):
        compile_pattern(r"ending \d{4}", groups=1)


def test_catastrophic_backtracking_shape_runs_in_linear_time():
    # (a+)+b against a run of a's takes exponential time in Python's re; RE2 shrugs.
    started = time.perf_counter()
    assert search(compile_pattern(r"(a+)+b"), "a" * 40) is None
    assert time.perf_counter() - started < 0.05


def test_a_match_that_overruns_its_deadline_raises_instead_of_blocking():
    huge = "a" * 20_000_000  # ~40 ms of linear work for RE2
    started = time.perf_counter()
    with pytest.raises(PatternTimeout):
        search(compile_pattern(r"(a+)+b"), huge, timeout=0.001)
    assert time.perf_counter() - started < 0.03  # the caller got control back before RE2 finished


def test_the_default_deadline_is_100ms():
    assert patterns.MATCH_TIMEOUT_SECONDS == 0.1
