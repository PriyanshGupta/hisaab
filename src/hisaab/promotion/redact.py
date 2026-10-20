"""Fixture redaction (tech-design §4.10 step 5; CLAUDE.md "Redact before any fixture is committed"). Pure.

A promoted format's three examples become fixtures. Before they are written:

- The message is rebuilt with only From (the bank's bare address), Subject, Date and the one body part
  the interpreter reads. To, Cc, Received, DKIM, Message-ID and every other header are dropped.
- HTML loses every attribute (links, image URLs, tracking ids) and its scripts and styles; the text the
  patterns read is unchanged.
- In the body, the subject and the expected reading, these are replaced:
    the identifiers the model extracted (account, counterparty, payee, reference), and the bare digits
    of a masked account wherever they stand alone ("type BLOCK CC 4321"),
    the owner's names, the recipients' addresses, and a capitalised name after Dear / Hi / Hello,
    any other email address or VPA (also %40-encoded), masked card/account numbers (XX1234, **1234),
    and digit runs of 6 or more (customer ids, OTPs, references, phone numbers).
  Amounts and dates stay: measurement needs them, and they identify no one. A number standing in an
  amount (Rs.4321.00) is never replaced, even when the same digits are also an identifier.

A replacement keeps the shape (digits become other digits, letters X or x, everything else stays) and
depends only on the value, so the same card number is the same fake in every file and in the expected
JSON, and a template's patterns still match the copy. One regex pass, known values first (longest first),
so nothing is replaced twice.
"""

import hashlib
import html
import re
from email import policy
from email.message import EmailMessage, MIMEPart
from email.parser import BytesParser
from email.utils import getaddresses, parseaddr
from html.parser import HTMLParser
from typing import Any

from hisaab.parsing.transforms import HIDDEN_TAGS

IDENTIFIER_FIELDS: tuple[str, ...] = ("account_identifier", "counterparty_identifier", "payee_raw",
                                      "reference_number")
_ADDRESS: str = r"[\w.+-]+(?:@|%40)[\w-]+(?:\.[\w-]+)*"  # %40: an address inside a link's query string
_MASKED: str = r"(?:[Xx*•]{2,}[\s-]?)(?P<masked>\d{2,})"
_DIGIT_RUN: str = r"(?<!\d)\d{6,}(?!\d)"  # customer ids, OTPs, references, account and phone numbers
# A capitalised name after a greeting, which the extracted values may not include ("Dear Asha KAPOOR,").
_GREETING: re.Pattern[str] = re.compile(r"\b(?P<greet>(?:Dear|Hi|Hello)\s+)(?P<who>[A-Z][\w.'-]*(?:\s+[A-Z][\w.'-]*){0,3})")
_GENERIC: frozenset[str] = frozenset({"customer", "cardholder", "cardmember", "sir", "madam", "member", "user",
                                      "valued customer", "sir/madam"})
_CURRENCY_BEFORE: re.Pattern[str] = re.compile(r"(?:Rs\.?|INR|₹)\s*$", re.IGNORECASE)
_NUMBER_AROUND: re.Pattern[str] = re.compile(r"[.,]\d")


def fake(value: str) -> str:
    """Same shape, other characters, the same answer for the same value (case aside)."""
    digest: bytes = hashlib.sha256(value.lower().encode()).digest()
    out: list[str] = []
    for i, ch in enumerate(value):
        if ch.isdigit():
            out.append(str(digest[i % len(digest)] % 10))
        elif ch.isalpha():
            out.append("X" if ch.isupper() else "x")
        else:
            out.append(ch)
    return "".join(out)


def redact(raw: bytes, expected: dict[str, Any], names: list[str]) -> tuple[bytes, dict[str, Any]]:
    msg: EmailMessage = BytesParser(policy=policy.default).parsebytes(raw)
    sender: str = parseaddr(str(msg["From"] or ""))[1].strip().lower()
    recipients: list[tuple[str, str]] = getaddresses([str(v) for v in msg.get_all("To", []) + msg.get_all("Cc", [])])
    people: list[str] = [*names, *(name for name, _ in recipients if name)]
    known: set[str] = {v for k in IDENTIFIER_FIELDS if isinstance(v := expected.get(k), str) and v.strip()}
    # the digits of a masked identifier stand alone too ("XX4321" in one line, "type BLOCK CC 4321" in another)
    known |= {d for k in ("account_identifier", "counterparty_identifier")
              if isinstance(v := expected.get(k), str) and len(d := re.sub(r"\D", "", v)) >= 4}
    known |= {p for p in people if p.strip()} | {w for p in people for w in p.split() if len(w) >= 3}
    known |= {addr for _, addr in recipients if addr}
    alternatives: list[str] = [rf"(?<!\w){re.escape(k)}(?!\w)" for k in sorted(known, key=len, reverse=True)]
    pattern: re.Pattern[str] = re.compile("|".join([*alternatives, _ADDRESS, _MASKED, _DIGIT_RUN]), re.IGNORECASE)

    def replace(m: re.Match[str]) -> str:
        if m.group("masked") is not None:
            return m.group(0)[: m.start("masked") - m.start()] + fake(m.group("masked"))
        if _in_amount(m):  # "Rs.4321.00" is money, even when 4321 is also the card's last digits
            return m.group(0)
        return m.group(0) if m.group(0).lower() == sender else fake(m.group(0))

    def greeting(m: re.Match[str]) -> str:
        who: str = m.group("who")
        return m.group(0) if who.lower() in _GENERIC else m.group("greet") + fake(who)

    def scrub(text: str) -> str:
        return pattern.sub(replace, _GREETING.sub(greeting, text))

    part: MIMEPart | None = msg.get_body(preferencelist=("html", "plain"))
    body: str = part.get_content() if part is not None else ""
    if part is not None and part.get_content_subtype() == "html":
        body = _bare_html(body)
    out: EmailMessage = EmailMessage()
    out["From"], out["Subject"] = sender, scrub(str(msg["Subject"] or ""))
    if msg["Date"] is not None:
        out["Date"] = str(msg["Date"])
    out.set_content(scrub(body), subtype=part.get_content_subtype() if part is not None else "plain")
    redacted: dict[str, Any] = {k: scrub(v) if k in IDENTIFIER_FIELDS and isinstance(v, str) else v
                                for k, v in expected.items()}
    return out.as_bytes(), redacted


def _in_amount(m: re.Match[str]) -> bool:
    """A run of digits that is part of a sum of money or a decimal: Rs.4321.00, ₹ 125000, 1,25,000.50."""
    if not m.group(0).replace(",", "").isdigit():
        return False
    text: str = m.string
    before: str = text[max(0, m.start() - 6):m.start()]
    return bool(_CURRENCY_BEFORE.search(before) or _NUMBER_AROUND.match(text, m.end())
                or (m.start() >= 2 and _NUMBER_AROUND.fullmatch(text[m.start() - 2:m.start()][::-1])))


class _Bare(HTMLParser):
    """The HTML with every attribute dropped (links, image URLs and tracking ids carry addresses and ids the
    patterns never read) and the parts strip_html hides (scripts, styles) left out. The text is unchanged,
    so a template reads the copy as it read the original."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.hidden: int = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in HIDDEN_TAGS:
            self.hidden += 1
        elif not self.hidden:
            self.out.append(f"<{tag}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if not self.hidden and tag not in HIDDEN_TAGS:
            self.out.append(f"<{tag}>")

    def handle_endtag(self, tag: str) -> None:
        if tag in HIDDEN_TAGS:
            self.hidden = max(0, self.hidden - 1)
        elif not self.hidden:
            self.out.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.out.append(html.escape(data, quote=False))


def _bare_html(body: str) -> str:
    parser: _Bare = _Bare()
    parser.feed(body)
    parser.close()
    return "".join(parser.out)
