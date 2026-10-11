"""gmail_connections (tech-design §3.3, §5.5): one per user, the grant encrypted at rest.

The credential is whatever the mailbox module handed back (a refresh token
today, an app password if IMAP replaces it): opaque here, encrypted with the
.env key before it is stored.

Reconnecting the same inbox keeps last_successful_sync_at, so the next sync
catches up on everything since; a different inbox starts with no clock.
"""

from sqlalchemy.orm import Session

from hisaab.config import Settings
from hisaab.crypto import decrypt, encrypt
from hisaab.ingest.mailbox import Grant, OAuthClient
from hisaab.models import GmailConnection, utc_now

CALLBACK_PATH: str = "/oauth/callback"


class GmailNotConfigured(RuntimeError):
    pass


def oauth_client(settings: Settings) -> OAuthClient:
    """The OAuth client from .env (the Desktop client created in slice 0). The redirect is the
    loopback address tech-design §5.5 registers, whatever address the browser used for the app."""
    if not (settings.google_oauth_client_id and settings.google_oauth_client_secret):
        raise GmailNotConfigured("Gmail is not set up: add HISAAB_GOOGLE_OAUTH_CLIENT_ID and "
                                 "HISAAB_GOOGLE_OAUTH_CLIENT_SECRET to .env")
    return OAuthClient(settings.google_oauth_client_id, settings.google_oauth_client_secret.get_secret_value(),
                       f"http://localhost:{settings.port}{CALLBACK_PATH}")


def encryption_key(settings: Settings) -> str:
    if settings.encryption_key is None:
        raise GmailNotConfigured("Gmail is not set up: add HISAAB_ENCRYPTION_KEY to .env")
    return settings.encryption_key.get_secret_value()


def save_grant(session: Session, user_id: str, grant: Grant, key: str) -> GmailConnection:
    now: str = utc_now()
    blob: bytes = encrypt(key, grant.credential)
    row: GmailConnection | None = session.get(GmailConnection, user_id)
    if row is None:
        row = GmailConnection(user_id=user_id, email_address=grant.email_address, refresh_token_enc=blob,
                              status="connected", connected_at=now, updated_at=now)
        session.add(row)
    else:
        if row.email_address != grant.email_address:
            row.last_successful_sync_at = None
        row.email_address, row.refresh_token_enc, row.status = grant.email_address, blob, "connected"
        row.connected_at = row.updated_at = now
    session.flush()
    return row


def credential(row: GmailConnection, key: str) -> str:
    """Raises crypto.DecryptionFailed if the key changed since the grant was stored."""
    return decrypt(key, row.refresh_token_enc)


def mark_needs_reauth(session: Session, user_id: str) -> None:
    row: GmailConnection | None = session.get(GmailConnection, user_id)
    if row is not None:
        row.status, row.updated_at = "needs_reauth", utc_now()
