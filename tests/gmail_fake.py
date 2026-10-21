"""A fake Gmail (OAuth token endpoint + Gmail API v1) for the mailbox and sync tests.

It answers the HTTP calls hisaab.ingest.mailbox makes, through the same
injectable `http(method, url, body, headers) -> (status, bytes)` function the
real transport implements. payload_from_eml turns an .eml into the shape
Gmail's messages.get?format=full returns: a tree of parts with transfer-decoded
bodies base64url-encoded, and attachments as an attachmentId plus size only.

Synthetic data only, except where a test feeds it a fixture read at runtime.
"""

import base64
import html
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parseaddr
from typing import Any, cast
from urllib.parse import SplitResult, parse_qs, urlsplit

TOKEN_URL: str = "https://oauth2.googleapis.com/token"
API: str = "https://gmail.googleapis.com/gmail/v1/users/me"
# Text parts larger than this come back by attachmentId, as Gmail does for big bodies.
INLINE_LIMIT: int = 10_000_000


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


@dataclass
class FakeGmail:
    address: str = "asha@example.test"
    refresh_token: str = "1//fake-refresh-token"
    page_size: int = 2  # small, so paging is exercised
    revoked: bool = False
    messages: dict[str, tuple[int, dict[str, Any]]] = field(default_factory=dict)  # id -> (internalDate ms, payload)
    blobs: dict[str, bytes] = field(default_factory=dict)  # attachmentId -> bytes (text bodies only are served)
    fail_next: list[int | tuple[int, bytes]] = field(default_factory=list)  # (status[, body]) before answering normally
    queries: list[str] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    broken_ids: set[str] = field(default_factory=set)  # messages.get answers 404 for these
    inline_limit: int = INLINE_LIMIT
    honor_query: bool = False  # filter messages.list by from:/-from:/after: and the words (the sweep tests)
    formats: list[str] = field(default_factory=list)  # messages.get format of every call
    metadata_headers: list[list[str]] = field(default_factory=list)
    texts: dict[str, tuple[str, str, str]] = field(default_factory=dict)  # id -> (from, subject, plain text)
    _next_blob: int = 0

    def add(self, message_id: str, raw: bytes, internal_date_ms: int) -> None:
        self.messages[message_id] = (internal_date_ms, self.payload_from_eml(raw))
        msg: EmailMessage = BytesParser(policy=policy.default).parsebytes(raw)
        part: Any = msg.get_body(preferencelist=("plain", "html"))
        text: str = re.sub(r"<[^>]+>", " ", part.get_content() if part is not None else "")
        self.texts[message_id] = (str(msg["From"] or ""), str(msg["Subject"] or ""), " ".join(text.split()))

    def _matches(self, message_id: str, q: str) -> bool:
        """Enough of Gmail's search for the tests: from:/-from: (bare addresses), after:<epoch>, and a leading
        (a OR "b c") group of words matched case-insensitively against subject and text."""
        sender, subject, text = self.texts[message_id]
        address: str = parseaddr(sender)[1].lower()
        if int(self.messages[message_id][0] / 1000) <= int(m.group(1)) if (m := re.search(r"after:(\d+)", q)) else False:
            return False
        if int(self.messages[message_id][0] / 1000) >= int(m.group(1)) if (m := re.search(r"before:(\d+)", q)) else False:
            return False
        if address in {x.lower() for x in re.findall(r"-from:(\S+)", q)} or ("-from:me" in q and address == self.address):
            return False
        wanted: list[str] = [x.lower() for x in re.findall(r"(?<!-)from:(\S+?)\)?(?:\s|$)", q)]
        if wanted and address not in wanted:
            return False
        group: re.Match[str] | None = re.match(r"\(([^)]*)\)", q)
        if group and "from:" not in group.group(1):
            words: list[str] = [w.strip('"').lower() for w in re.split(r"\s+OR\s+", group.group(1))]
            return any(w in f"{subject} {text}".lower() for w in words)
        return True

    def payload_from_eml(self, raw: bytes) -> dict[str, Any]:
        return self._part(BytesParser(policy=policy.default).parsebytes(raw), "")

    def _part(self, part: EmailMessage, part_id: str) -> dict[str, Any]:
        node: dict[str, Any] = {
            "partId": part_id, "mimeType": part.get_content_type(), "filename": part.get_filename() or "",
            "headers": [{"name": k, "value": str(v)} for k, v in part.items()],
        }
        if part.is_multipart():
            node["body"] = {"size": 0}
            node["parts"] = [self._part(p, f"{part_id}.{i}" if part_id else str(i))
                             for i, p in enumerate(cast(Iterator[EmailMessage], part.iter_parts()))]
            return node
        data: bytes = cast(bytes, part.get_payload(decode=True) or b"")  # decode=True on a leaf: bytes
        if node["filename"] or len(data) > self.inline_limit:
            self._next_blob += 1
            blob_id: str = f"att-{self._next_blob}"
            if not node["filename"]:
                self.blobs[blob_id] = data  # a big text body: servable. Files are never served.
            node["body"] = {"attachmentId": blob_id, "size": len(data)}
        else:
            node["body"] = {"size": len(data), "data": b64url(data)}
        return node

    def __call__(self, method: str, url: str, body: bytes | None, headers: dict[str, str]) -> tuple[int, bytes]:
        parts: SplitResult = urlsplit(url)
        path: str = f"{parts.scheme}://{parts.netloc}{parts.path}"
        query: dict[str, list[str]] = parse_qs(parts.query)
        self.calls.append(f"{method} {parts.path}")
        if self.fail_next:
            failure: int | tuple[int, bytes] = self.fail_next.pop(0)
            return failure if isinstance(failure, tuple) else (failure, b"{}")
        if path == TOKEN_URL:
            form: dict[str, list[str]] = parse_qs((body or b"").decode())
            if self.revoked:
                return 400, json.dumps({"error": "invalid_grant", "error_description": "Token has been expired or revoked."}).encode()
            if form["grant_type"] == ["authorization_code"]:
                return 200, json.dumps({"access_token": "access-1", "refresh_token": self.refresh_token,
                                        "expires_in": 3599}).encode()
            assert form["refresh_token"] == [self.refresh_token]
            return 200, json.dumps({"access_token": "access-1", "expires_in": 3599}).encode()
        if headers.get("Authorization") != "Bearer access-1" or self.revoked:
            return 401, b'{"error": {"code": 401}}'
        if path == f"{API}/profile":
            return 200, json.dumps({"emailAddress": self.address}).encode()
        if path == f"{API}/messages":
            self.queries.append(query["q"][0])
            ids: list[str] = sorted(self.messages, key=lambda m: -self.messages[m][0])  # newest first, as Gmail
            if self.honor_query:
                ids = [i for i in ids if self._matches(i, query["q"][0])]
            start: int = int(query.get("pageToken", ["0"])[0])
            page: list[str] = ids[start:start + self.page_size]
            answer: dict[str, Any] = {"messages": [{"id": m, "threadId": m} for m in page]} if page else {}
            answer["resultSizeEstimate"] = len(ids)  # Gmail's is approximate; the fake's is exact
            if start + self.page_size < len(ids):
                answer["nextPageToken"] = str(start + self.page_size)
            return 200, json.dumps(answer).encode()
        if path.startswith(f"{API}/messages/"):
            rest: list[str] = path.removeprefix(f"{API}/messages/").split("/")
            if len(rest) == 3 and rest[1] == "attachments":
                if rest[2] not in self.blobs:
                    raise AssertionError(f"attachment {rest[2]} fetched: files must never be downloaded")
                return 200, json.dumps({"size": len(self.blobs[rest[2]]), "data": b64url(self.blobs[rest[2]])}).encode()
            if rest[0] in self.broken_ids or rest[0] not in self.messages:
                return 404, b'{"error": {"code": 404}}'
            self.formats.append(query.get("format", [""])[0])
            internal, payload = self.messages[rest[0]]
            if query.get("format") == ["metadata"]:  # headers asked for, and Gmail's escaped plain-text preview
                asked: list[str] = query.get("metadataHeaders", [])
                self.metadata_headers.append(asked)
                kept: list[dict[str, str]] = [h for h in payload["headers"] if h["name"] in asked]
                return 200, json.dumps({"id": rest[0], "internalDate": str(internal),
                                        "snippet": html.escape(self.texts[rest[0]][2][:200]),
                                        "payload": {"headers": kept}}).encode()
            assert query.get("format") == ["full"]
            return 200, json.dumps({"id": rest[0], "internalDate": str(internal), "payload": payload}).encode()
        return 404, b"{}"


def make_eml(sender: str, subject: str, html: str, plain: str | None = None,
             attachment: tuple[str, bytes] | None = None, date: str = "Sat, 10 Oct 2026 09:15:00 +0530") -> bytes:
    msg: EmailMessage = EmailMessage()
    msg["From"], msg["To"], msg["Subject"], msg["Date"] = sender, "asha@example.test", subject, date
    if plain is not None:
        msg.set_content(plain)
        msg.add_alternative(html, subtype="html")
    else:
        msg.set_content(html, subtype="html")
    if attachment is not None:
        msg.add_attachment(attachment[1], maintype="application", subtype="pdf", filename=attachment[0])
    return msg.as_bytes()
