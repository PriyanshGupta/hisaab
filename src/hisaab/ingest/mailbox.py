"""The mailbox: the ONLY module that knows mail arrives through the Gmail API.

Its interface is all the rest of the app sees:
- authorization_url / exchange_code: connect an inbox, returning a Grant whose
  `credential` is an opaque string to store (encrypted) and hand back to open()
- Mailbox.open(client, credential), then
  - search(senders, since): pages of message ids from those senders since then
  - fetch(message_id): a Message: RFC 822 content, received time, and the
    metadata of its attachments (filename, size, type), which comes back with
    the message at no extra cost
- MailboxAuthError: the grant is dead (revoked, expired, or invalid); the user
  must reconnect. MailboxError: anything else, after retries.

Message ids are opaque strings to every caller. IMAP with an app password is the
fallback if Gmail's refresh token does not survive (build-plan slice 0); the swap
replaces this file and the connect screen, nothing in the sync job.

Why format=full and not format=raw: raw is the whole message, PDF statements
included, and attachments are metadata-only (tech-design §5.1). full returns the
part tree with text bodies inline and files as an attachmentId this code never
requests. `content` is rebuilt from the headers and text parts parse_eml reads,
with a boundary derived from the content, so the same message gives the same
bytes (and extraction-cache hash) on every sync.

Every HTTP call has a timeout (the job queue cannot stop a hung socket); 429s
and 5xx back off exponentially, 5 retries, then fail cleanly (§5.6).
"""

import base64
import hashlib
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from typing import Any, TypeAlias
from urllib.parse import quote, urlencode

AUTH_URL: str = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL: str = "https://oauth2.googleapis.com/token"
API: str = "https://gmail.googleapis.com/gmail/v1/users/me"
SCOPE: str = "https://www.googleapis.com/auth/gmail.readonly"
TIMEOUT_S: float = 30.0
RETRIES: int = 5
RETRYABLE: frozenset[int] = frozenset({0, 429, 500, 502, 503, 504})  # 0: no response (network, timeout)
PAGE_SIZE: int = 100
KEPT_HEADERS: tuple[str, ...] = ("From", "To", "Subject", "Date")

# How this module reaches Gmail: (method, url, body, headers) -> (status, body). Opaque to callers,
# who only pass it through; tests pass a fake Gmail. An IMAP mailbox would define its own.
Transport: TypeAlias = Callable[[str, str, bytes | None, dict[str, str]], tuple[int, bytes]]


class MailboxError(RuntimeError):
    pass


class MailboxAuthError(MailboxError):
    pass


@dataclass(frozen=True)
class OAuthClient:
    client_id: str
    client_secret: str
    redirect_uri: str


@dataclass(frozen=True)
class Grant:
    email_address: str
    credential: str  # opaque: store encrypted, pass back to Mailbox.open


@dataclass(frozen=True)
class AttachmentMeta:
    filename: str
    size: int  # bytes
    mime_type: str


@dataclass(frozen=True)
class Message:
    message_id: str  # opaque
    received_at: datetime  # UTC
    content: bytes  # RFC 822: headers and text parts, never attachments
    attachments: tuple[AttachmentMeta, ...]


def default_transport(method: str, url: str, body: bytes | None, headers: dict[str, str]) -> tuple[int, bytes]:
    request: urllib.request.Request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            status: int = response.status
            return status, response.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        return 0, b""


def _call(http: Transport, sleep: Callable[[float], None], method: str, url: str, body: bytes | None,
          headers: dict[str, str]) -> tuple[int, Any]:
    for attempt in range(RETRIES + 1):
        status, raw = http(method, url, body, headers)
        if status not in RETRYABLE:
            try:
                return status, json.loads(raw or b"{}")
            except ValueError:
                return status, {}
        if attempt < RETRIES:
            sleep(2 ** attempt)
    raise MailboxError(f"Gmail unavailable: HTTP {status or 'no response'} after {RETRIES} retries")


def _token(http: Transport, sleep: Callable[[float], None], client: OAuthClient, form: dict[str, str]) -> dict[str, Any]:
    body: bytes = urlencode({**form, "client_id": client.client_id, "client_secret": client.client_secret}).encode()
    status, answer = _call(http, sleep, "POST", TOKEN_URL, body, {"Content-Type": "application/x-www-form-urlencoded"})
    if status == 400 and answer.get("error") == "invalid_grant":
        raise MailboxAuthError(f"invalid_grant: {answer.get('error_description', 'grant expired or revoked')}")
    if status != 200 or "access_token" not in answer:
        raise MailboxError(f"Google token endpoint answered HTTP {status}: {answer.get('error', 'no access token')}")
    token: dict[str, Any] = answer
    return token


def authorization_url(client: OAuthClient, state: str) -> str:
    return f"{AUTH_URL}?" + urlencode({
        "client_id": client.client_id, "redirect_uri": client.redirect_uri, "response_type": "code",
        "scope": SCOPE, "access_type": "offline", "prompt": "consent",  # consent: always return a refresh token
        "state": state,
    })


def exchange_code(client: OAuthClient, code: str, *, http: Transport = default_transport,
                  sleep: Callable[[float], None] = time.sleep) -> Grant:
    token: dict[str, Any] = _token(http, sleep, client, {
        "grant_type": "authorization_code", "code": code, "redirect_uri": client.redirect_uri})
    if not token.get("refresh_token"):
        raise MailboxError("Google returned no refresh token; remove the app's access in your Google account and retry")
    box: Mailbox = Mailbox(client, token["refresh_token"], token["access_token"], http, sleep)
    return Grant(box.email_address(), token["refresh_token"])


class Mailbox:
    def __init__(self, client: OAuthClient, credential: str, access_token: str, http: Transport,
                 sleep: Callable[[float], None]) -> None:
        self._client: OAuthClient = client
        self._refresh_token: str = credential
        self._access: str = access_token
        self._http: Transport = http
        self._sleep: Callable[[float], None] = sleep

    @classmethod
    def open(cls, client: OAuthClient, credential: str, *, http: Transport = default_transport,
             sleep: Callable[[float], None] = time.sleep) -> "Mailbox":
        """Raises MailboxAuthError if the grant is no longer valid."""
        token: dict[str, Any] = _token(http, sleep, client, {"grant_type": "refresh_token", "refresh_token": credential})
        return cls(client, credential, token["access_token"], http, sleep)

    def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        url: str = f"{API}/{path}" + (f"?{urlencode(params)}" if params else "")
        for refreshed in (False, True):
            status, answer = _call(self._http, self._sleep, "GET", url, None,
                                   {"Authorization": f"Bearer {self._access}"})
            if status == 401 and not refreshed:  # access tokens last an hour; a long sync outlives one
                self._access = _token(self._http, self._sleep, self._client,
                                      {"grant_type": "refresh_token", "refresh_token": self._refresh_token})["access_token"]
                continue
            if status == 401:
                raise MailboxAuthError("Gmail refused the refreshed access token")
            if status != 200:
                raise MailboxError(f"Gmail answered HTTP {status} for {path.split('/')[0]}")
            result: dict[str, Any] = answer
            return result
        raise AssertionError("unreachable")

    def email_address(self) -> str:
        address: str = self._get("profile")["emailAddress"]
        return address

    def search(self, senders: Sequence[str], since: datetime) -> Iterator[list[str]]:
        """Pages of message ids (newest first) from any of `senders`, received after `since`."""
        if not senders:  # a query with no from: terms would match the whole mailbox
            raise ValueError("search needs at least one sender")
        query: str = "(" + " OR ".join(f"from:{s}" for s in senders) + f") after:{int(since.timestamp())}"
        params: dict[str, str] = {"q": query, "maxResults": str(PAGE_SIZE)}
        while True:
            answer: dict[str, Any] = self._get("messages", params)
            ids: list[str] = [m["id"] for m in answer.get("messages", [])]
            if ids:
                yield ids
            if not answer.get("nextPageToken"):
                return
            params = {**params, "pageToken": answer["nextPageToken"]}

    def fetch(self, message_id: str) -> Message:
        answer: dict[str, Any] = self._get(f"messages/{quote(message_id, safe='')}", {"format": "full"})
        received: datetime = datetime.fromtimestamp(int(answer["internalDate"]) / 1000, UTC)
        texts: dict[str, tuple[bytes, str]] = {}  # mime type -> (body bytes, charset)
        attachments: list[AttachmentMeta] = []
        for part in _leaves(answer["payload"]):
            body: dict[str, Any] = part.get("body", {})
            mime: str = part.get("mimeType", "")
            if part.get("filename"):
                attachments.append(AttachmentMeta(part["filename"], int(body.get("size", 0)), mime))
            elif mime in ("text/html", "text/plain") and mime not in texts:
                data: str | None = body.get("data")
                if data is None and body.get("attachmentId"):  # a big text body, not a file
                    data = self._get(f"messages/{quote(message_id, safe='')}/attachments/{body['attachmentId']}")["data"]
                texts[mime] = (_b64(data or ""), _charset(part))
        return Message(message_id, received, _rebuild(_headers(answer["payload"]), texts), tuple(attachments))


def _leaves(part: dict[str, Any]) -> Iterator[dict[str, Any]]:
    if part.get("parts"):
        for child in part["parts"]:
            yield from _leaves(child)
    else:
        yield part


def _headers(part: dict[str, Any]) -> dict[str, str]:
    return {h["name"].lower(): h["value"] for h in part.get("headers", [])}


def _charset(part: dict[str, Any]) -> str:
    content_type: str = _headers(part).get("content-type", "")
    for param in content_type.split(";")[1:]:
        name, _, value = param.strip().partition("=")
        if name.lower() == "charset" and value:
            charset: str = value.strip('"')
            try:
                "".encode(charset)
                return charset
            except LookupError:
                break
    return "utf-8"


def _b64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _rebuild(headers: dict[str, str], texts: dict[str, tuple[bytes, str]]) -> bytes:
    """Headers plus the text parts, bodies byte-for-byte as sent (base64, original charset):
    set_content(str) would rewrite line endings and append a newline."""
    msg: EmailMessage = EmailMessage()
    for name in KEPT_HEADERS:
        if headers.get(name.lower()):
            msg[name] = headers[name.lower()]
    present: list[tuple[str, tuple[bytes, str]]] = [(m, texts[m]) for m in ("text/plain", "text/html") if m in texts]
    if not present:
        present = [("text/plain", (b"", "utf-8"))]
    for i, (mime, (data, charset)) in enumerate(present):
        add: Callable[..., None] = msg.set_content if i == 0 else msg.add_alternative
        add(data, maintype="text", subtype=mime.split("/")[1], params={"charset": charset})  # bytes: base64, verbatim
    if len(present) > 1:  # the default boundary is random; derive it so the bytes are stable
        msg.set_boundary("hisaab-" + hashlib.sha256(b"\0".join(d for _, (d, _c) in present)).hexdigest()[:32])
    return msg.as_bytes()
