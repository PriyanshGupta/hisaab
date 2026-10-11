"""Password hashing with Argon2id (argon2-cffi defaults: RFC 9106 low-memory profile).

The encoded hash carries its own random salt and parameters, so a per-user salt
needs no extra column, and old hashes keep verifying after the parameters are
raised. verify_password reports when a hash should be upgraded; login rehashes
then.
"""

import threading
from functools import cache

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher()
# Each Argon2 run takes 64 MiB. The web server runs up to 40 requests at once,
# so without this cap a burst of logins could claim ~2.5 GiB of the laptop's RAM.
_argon2_slots = threading.BoundedSemaphore(2)


def hash_password(password: str) -> str:
    with _argon2_slots:
        return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> tuple[bool, bool]:
    """(matches, needs_rehash). Never raises on a wrong password or malformed hash."""
    try:
        with _argon2_slots:
            _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False, False
    return True, _hasher.check_needs_rehash(stored_hash)


@cache
def _dummy_hash() -> str:
    return _hasher.hash("hisaab-timing-equalizer")


def burn_verify_time(password: str) -> None:
    """Spend the same time a real verify would, for logins with an unknown username.

    Without this, an unknown username answers ~50 ms faster than a wrong password
    for a real one, which reveals which usernames exist.
    """
    verify_password(_dummy_hash(), password)
