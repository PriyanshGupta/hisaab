"""Secrets at rest (tech-design §9.1, §9.6): the key lives in .env, never in the database."""

import pytest
from cryptography.fernet import Fernet

from hisaab.crypto import DecryptionFailed, decrypt, encrypt

KEY: str = Fernet.generate_key().decode()


def test_round_trip_and_ciphertext_hides_the_plaintext() -> None:
    blob: bytes = encrypt(KEY, "1//refresh-token-value")
    assert b"refresh-token-value" not in blob
    assert decrypt(KEY, blob) == "1//refresh-token-value"


def test_wrong_key_is_a_clear_failure() -> None:
    blob: bytes = encrypt(KEY, "secret")
    with pytest.raises(DecryptionFailed):
        decrypt(Fernet.generate_key().decode(), blob)


def test_a_malformed_key_is_a_clear_failure() -> None:
    with pytest.raises(DecryptionFailed, match="HISAAB_ENCRYPTION_KEY"):
        encrypt("not-a-fernet-key", "secret")
