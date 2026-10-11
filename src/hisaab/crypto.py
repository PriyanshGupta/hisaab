"""Encryption at rest for Gmail grants (tech-design §9.1, §9.6).

Fernet (AES-128-CBC + HMAC-SHA256) with the key from HISAAB_ENCRYPTION_KEY in
.env, held outside the database, so a copied .db file alone exposes no grant.
"""

from cryptography.fernet import Fernet, InvalidToken

KEY_HELP: str = ("HISAAB_ENCRYPTION_KEY must be a Fernet key. Generate one with: .venv/bin/python -c "
                 "'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'")


class DecryptionFailed(ValueError):
    pass


def _fernet(key: str) -> Fernet:
    try:
        return Fernet(key.encode())
    except ValueError:
        raise DecryptionFailed(KEY_HELP) from None


def encrypt(key: str, plaintext: str) -> bytes:
    return _fernet(key).encrypt(plaintext.encode())


def decrypt(key: str, blob: bytes) -> str:
    try:
        return _fernet(key).decrypt(blob).decode()
    except InvalidToken:  # a different key than the one that encrypted it
        raise DecryptionFailed("stored secret cannot be decrypted with the current HISAAB_ENCRYPTION_KEY") from None
