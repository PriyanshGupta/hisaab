"""Pattern execution for templates (tech-design §4.5, PRD §8.5).

Every template pattern runs on google-re2, never Python's `re`. RE2 matches in
linear time, so a badly formed pattern (an LLM-proposed one, say) cannot hang
the parser through catastrophic backtracking. RE2 refuses the features that
make backtracking necessary (lookarounds, backreferences) at compile time; no
fixture pattern needs them.

Two further guards:
- `compile_pattern` is the save-time check: compiles under RE2, length cap, and
  a capture group for field patterns. A template that fails it is never stored.
- `search` has a 100 ms deadline per match (PRD §10.1), a backstop against
  pathological input *length*. google-re2 releases the GIL while matching, so
  the match runs on a worker thread and the caller stops waiting at the deadline.
"""

import concurrent.futures
from typing import TypeAlias

import re2

Pattern: TypeAlias = re2._Regexp  # a compiled RE2 pattern
Match: TypeAlias = re2._Match

MAX_PATTERN_LENGTH: int = 500
MATCH_TIMEOUT_SECONDS: float = 0.1

_OPTIONS: re2.Options = re2.Options()
_OPTIONS.log_errors = False  # RE2 otherwise prints every rejected pattern to stderr
# shortcut: a match that overruns keeps its worker until RE2 finishes (linear, so it does);
# 4 workers absorb that at household volume. Revisit if timeouts ever show up in diagnostics.
_POOL: concurrent.futures.ThreadPoolExecutor = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="re2")


class PatternError(ValueError):
    """A pattern a template may not use (message names the problem)."""


class PatternTimeout(Exception):
    """A match ran past its deadline; the field counts as failed."""


def compile_pattern(pattern: object, *, groups: int = 0) -> Pattern:
    if not isinstance(pattern, str) or not pattern:
        raise PatternError("pattern is empty")
    if len(pattern) > MAX_PATTERN_LENGTH:
        raise PatternError(f"pattern is {len(pattern)} characters; the limit is {MAX_PATTERN_LENGTH}")
    try:
        compiled: Pattern = re2.compile(pattern, _OPTIONS)
    except re2.error as e:
        reason: str = e.args[0].decode() if e.args and isinstance(e.args[0], bytes) else str(e)
        raise PatternError(f"RE2 cannot compile it: {reason}") from None
    if compiled.groups < groups:
        raise PatternError("pattern needs a capture group (...) around the value")
    return compiled


def search(compiled: Pattern, text: str, timeout: float | None = None) -> Match | None:
    """compiled.search(text), or PatternTimeout after `timeout` seconds (default 100 ms)."""
    future: concurrent.futures.Future[Match | None] = _POOL.submit(compiled.search, text)
    try:
        return future.result(timeout=MATCH_TIMEOUT_SECONDS if timeout is None else timeout)
    except concurrent.futures.TimeoutError:
        raise PatternTimeout("pattern timed out") from None


def matches(compiled: Pattern, text: str) -> bool:
    """Whether it matches within the deadline; a pattern that runs out of time counts as no match."""
    try:
        return search(compiled, text) is not None
    except PatternTimeout:
        return False
