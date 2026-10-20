"""The email fingerprint (tech-design §4.9): which format an email is, for counting sightings only.

    sender          exact address
    subject_masked  amounts -> ₹#, dates -> <date>, other digit runs -> #
    static_lines    the fixed phrases: after masking numbers, amounts, dates, VPAs and addresses,
                    the runs of two or more lower-case words in each text segment, in order
    bucket          a hash of the HTML tag names in first-seen order, plus table and paragraph
                    counts in bands ("text" for an email with no HTML)

Computed from the email alone, before any model reads it, so a format already known to be
non-transactional can skip the model (ruling 2026-10-10). Names are kept out by keeping only
lower-case words: "Dear Asha Rao", "RAVI KUMAR" and "Corner Grocer" vanish, "has been debited
from account" stays. A lower-case merchant name would split a format; capitalised fixed words
("UPI", "Your") are dropped too, which only groups more loosely. Over-grouping is the safe side:
it fails replay validation visibly, where under-grouping never proposes anything (§4.9).
"""

import hashlib
import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser

from hisaab.parsing.email_interpreter import EmailArtifact

MAX_LINES: int = 60
_AMOUNT: re.Pattern[str] = re.compile(r"(?:Rs\.?|INR|₹)\s*\d[\d,]*(?:\.\d+)?", re.IGNORECASE)
_DATE: re.Pattern[str] = re.compile(r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}[-/ ](?:\d{1,2}|[A-Za-z]{3})[-/ ]\d{2,4})\b")
_DIGITS: re.Pattern[str] = re.compile(r"\d[\d,]*")
_ADDRESS: re.Pattern[str] = re.compile(r"\S+@\S+")  # email addresses and VPAs
_WORD: re.Pattern[str] = re.compile(r"[a-z][a-z'’-]*")
_HTML_TAG: re.Pattern[str] = re.compile(r"<[a-zA-Z][^>]*>")
_EDGE_PUNCTUATION: str = ".,;:!?()[]{}\"'“”‘’"


@dataclass(frozen=True)
class Fingerprint:
    value: str  # sha256 over the four parts: format_sightings.fingerprint
    sender: str
    subject_masked: str
    static_lines: list[str]
    bucket: str


def mask_subject(subject: str) -> str:
    return " ".join(_DIGITS.sub("#", _DATE.sub("<date>", _AMOUNT.sub("₹#", subject))).split())


def fingerprint(artifact: EmailArtifact) -> Fingerprint:
    subject: str = mask_subject(artifact.subject)
    if _HTML_TAG.search(artifact.body):
        reader: _Structure = _Structure()
        reader.feed(artifact.body)
        reader.close()
        segments: list[str] = reader.texts
        bucket: str = hashlib.sha256("|".join(
            [*reader.tags, f"t{_band(reader.counts.get('table', 0))}", f"p{_band(reader.counts.get('p', 0))}"]
        ).encode()).hexdigest()[:16]
    else:
        segments, bucket = artifact.body.splitlines(), "text"
    lines: list[str] = [phrase for segment in segments for phrase in _phrases(segment)][:MAX_LINES]
    value: str = hashlib.sha256(json.dumps([artifact.sender, subject, lines, bucket]).encode()).hexdigest()
    return Fingerprint(value, artifact.sender, subject, lines, bucket)


def _phrases(segment: str) -> list[str]:
    masked: str = _DIGITS.sub("#", _DATE.sub("<date>", _AMOUNT.sub("₹#", _ADDRESS.sub("@", segment))))
    found: list[str] = []
    run: list[str] = []
    for token in [*masked.split(), ""]:  # the empty sentinel ends the last run
        word: str = token.strip(_EDGE_PUNCTUATION)
        if word and _WORD.fullmatch(word):
            run.append(word)
            continue
        if len(run) >= 2:
            found.append(" ".join(run))
        run = []
    return found


def _band(n: int) -> str:
    return "0" if n == 0 else "1" if n == 1 else "2-3" if n <= 3 else "4-7" if n <= 7 else "8+"


class _Structure(HTMLParser):
    """Text nodes (scripts and styles skipped), distinct tag names in first-seen order, and tag counts."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.texts: list[str] = []
        self.tags: list[str] = []
        self.counts: dict[str, int] = {}
        self._skip: int = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in self.counts:
            self.tags.append(tag)
        self.counts[tag] = self.counts.get(tag, 0) + 1
        self._skip += tag in ("script", "style")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip and data.strip():
            self.texts.append(data)
