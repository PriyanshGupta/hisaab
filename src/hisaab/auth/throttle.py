"""Sign-in throttling: after 10 wrong passwords in a row, a username is refused
for 15 minutes.

The server is reachable by anything on the home network (tech-design §9.6),
so without this a device could guess a family member's password nonstop.
Attempts refused while locked are not counted, so the lock ends 15 minutes
after the 10th failure instead of being kept alive by the attacker. Unknown
usernames lock exactly like real ones; otherwise the lockout itself would
reveal which usernames exist.

State is in memory, which is right for the app's single process; a restart
clears it.
"""

import threading
import time
from collections.abc import Callable

MAX_FAILURES: int = 10
LOCKOUT_SECONDS: int = 15 * 60


class LoginThrottle:
    def __init__(self, clock: Callable[[], float] = time.monotonic, max_tracked: int = 10_000) -> None:
        self._clock: Callable[[], float] = clock
        self._max_tracked: int = max_tracked
        self._entries: dict[str, tuple[int, float]] = {}  # username -> (failures in a row, last failure)
        self._lock: threading.Lock = threading.Lock()

    def is_locked(self, username: str) -> bool:
        with self._lock:
            entry: tuple[int, float] | None = self._entries.get(username)
            return (entry is not None and entry[0] >= MAX_FAILURES
                    and self._clock() - entry[1] < LOCKOUT_SECONDS)

    def record_failure(self, username: str) -> None:
        with self._lock:
            now: float = self._clock()
            count, last = self._entries.get(username, (0, now))
            if now - last >= LOCKOUT_SECONDS:
                count = 0  # the earlier failures are old news
            self._entries[username] = (count + 1, now)
            if len(self._entries) > self._max_tracked:
                self._prune(now)

    def record_success(self, username: str) -> None:
        with self._lock:
            self._entries.pop(username, None)

    def _prune(self, now: float) -> None:
        self._entries = {u: e for u, e in self._entries.items() if now - e[1] < LOCKOUT_SECONDS}
        # shortcut: under a spray of >10k distinct usernames in 15 minutes the oldest
        # entries are dropped, which can end a lock early; add per-IP limits if the
        # app is ever exposed beyond the LAN.
        if len(self._entries) > self._max_tracked:
            newest: list[tuple[str, tuple[int, float]]] = sorted(
                self._entries.items(), key=lambda kv: kv[1][1])[-self._max_tracked:]
            self._entries = dict(newest)
