"""The mailbox module: the one place that knows the transport is the Gmail API."""

import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from hisaab.config import PROJECT_ROOT
from hisaab.ingest.mailbox import (
    AttachmentMeta,
    Grant,
    Mailbox,
    MailboxAuthError,
    MailboxError,
    Message,
    OAuthClient,
    authorization_url,
    exchange_code,
)
from hisaab.parsing.email_interpreter import EmailArtifact, interpret, parse_eml
from hisaab.parsing.measure import Templates, load_seed_templates
from hisaab.parsing.templates import select_email_candidates
from tests.gmail_fake import FakeGmail, make_eml

CLIENT: OAuthClient = OAuthClient("client-id.apps.example", "client-secret", "http://localhost:8765/oauth/callback")
SEED_DIR: Path = PROJECT_ROOT / "src/hisaab/parsing/seed"
FIXTURE_EMAILS: list[Path] = sorted((PROJECT_ROOT / "fixtures").rglob("*.eml"))
OCT_10: int = int(datetime(2026, 10, 10, 3, 45, tzinfo=UTC).timestamp() * 1000)


def open_box(fake: FakeGmail, sleeps: list[float] | None = None) -> Mailbox:
    slept: list[float] = sleeps if sleeps is not None else []
    return Mailbox.open(CLIENT, fake.refresh_token, http=fake, sleep=slept.append)


def test_authorization_url_asks_for_read_only_offline_access() -> None:
    url: str = authorization_url(CLIENT, state="signed-state")
    q: dict[str, list[str]] = parse_qs(urlsplit(url).query)
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert q["scope"] == ["https://www.googleapis.com/auth/gmail.readonly"]
    assert (q["access_type"], q["prompt"], q["response_type"]) == (["offline"], ["consent"], ["code"])
    assert (q["client_id"], q["redirect_uri"], q["state"]) == ([CLIENT.client_id], [CLIENT.redirect_uri], ["signed-state"])


def test_exchange_code_returns_the_address_and_an_opaque_credential() -> None:
    fake: FakeGmail = FakeGmail()
    assert exchange_code(CLIENT, "auth-code", http=fake) == Grant("asha@example.test", fake.refresh_token)


def test_search_pages_through_ids_for_the_senders_since_a_time() -> None:
    fake: FakeGmail = FakeGmail(page_size=2)
    for i in range(5):
        fake.add(f"m{i}", make_eml("alerts@bank.example", f"Alert {i}", "<p>hi</p>"), OCT_10 + i)
    since: datetime = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
    pages: list[list[str]] = list(open_box(fake).search(["alerts@bank.example", "cards@bank.example"], since))
    assert pages == [["m4", "m3"], ["m2", "m1"], ["m0"]]
    assert fake.queries[0] == f"(from:alerts@bank.example OR from:cards@bank.example) after:{int(since.timestamp())}"


def test_search_keeps_gmails_estimate_of_the_total_for_progress() -> None:
    fake: FakeGmail = FakeGmail(page_size=2)
    for i in range(5):
        fake.add(f"m{i}", make_eml("alerts@bank.example", f"Alert {i}", "<p>hi</p>"), OCT_10 + i)
    box: Mailbox = open_box(fake)
    assert box.estimate is None
    next(iter(box.search(["alerts@bank.example"], datetime(2026, 10, 8, tzinfo=UTC))))
    assert box.estimate == 5


def test_search_with_no_senders_never_queries_the_whole_mailbox() -> None:
    fake: FakeGmail = FakeGmail()
    with pytest.raises(ValueError, match="sender"):
        list(open_box(fake).search([], datetime(2026, 10, 8, tzinfo=UTC)))
    assert fake.queries == []


def test_fetch_returns_content_and_attachment_metadata_but_never_the_file() -> None:
    fake: FakeGmail = FakeGmail()
    pdf: bytes = b"%PDF-1.7 statement bytes that must never be fetched"
    fake.add("m1", make_eml("alerts@bank.example", "Your statement", "<p>Statement attached</p>", plain="Statement attached",
                            attachment=("statement.pdf", pdf)), OCT_10)
    message: Message = open_box(fake).fetch("m1")
    assert message.message_id == "m1"
    assert message.received_at == datetime(2026, 10, 10, 3, 45, tzinfo=UTC)
    assert message.attachments == (AttachmentMeta("statement.pdf", len(pdf), "application/pdf"),)
    assert b"PDF-1.7" not in message.content and not any("/attachments/" in c for c in fake.calls)
    artifact: EmailArtifact = parse_eml(message.content)
    assert (artifact.sender, artifact.subject) == ("alerts@bank.example", "Your statement")
    assert "Statement attached" in artifact.body


def test_a_text_body_served_by_attachment_id_is_fetched() -> None:
    fake: FakeGmail = FakeGmail(inline_limit=10)  # Gmail hands big bodies out by attachmentId
    fake.add("m1", make_eml("alerts@bank.example", "Alert", "<p>Rs. 450.00 debited</p>"), OCT_10)
    assert "Rs. 450.00 debited" in parse_eml(open_box(fake).fetch("m1").content).body


def test_the_same_message_rebuilds_to_the_same_bytes() -> None:
    fake: FakeGmail = FakeGmail()
    fake.add("m1", make_eml("alerts@bank.example", "Alert", "<p>html</p>", plain="plain"), OCT_10)
    box: Mailbox = open_box(fake)
    assert box.fetch("m1").content == box.fetch("m1").content  # content_hash must be stable across syncs


def test_rate_limits_back_off_then_succeed() -> None:
    fake: FakeGmail = FakeGmail()
    sleeps: list[float] = []
    box: Mailbox = open_box(fake, sleeps)
    fake.fail_next = [429, 503]
    assert list(box.search(["a@bank.example"], datetime(2026, 10, 8, tzinfo=UTC))) == []
    assert sleeps == [1, 2]


def test_rate_limits_give_up_after_five_retries() -> None:
    fake: FakeGmail = FakeGmail()
    sleeps: list[float] = []
    box: Mailbox = open_box(fake, sleeps)
    fake.fail_next = [429] * 6
    with pytest.raises(MailboxError, match="429"):
        list(box.search(["a@bank.example"], datetime(2026, 10, 8, tzinfo=UTC)))
    assert sleeps == [1, 2, 4, 8, 16]


def gmail_403(reason: str) -> tuple[int, bytes]:
    return 403, json.dumps({"error": {"code": 403, "status": "PERMISSION_DENIED",
                                      "errors": [{"reason": reason, "domain": "usageLimits"}]}}).encode()


@pytest.mark.parametrize("reason", ["rateLimitExceeded", "userRateLimitExceeded"])
def test_gmails_403_rate_limits_back_off_like_429(reason: str) -> None:
    fake: FakeGmail = FakeGmail()
    fake.add("m1", make_eml("a@bank.example", "Alert", "<p>x</p>"), OCT_10)
    sleeps: list[float] = []
    box: Mailbox = open_box(fake, sleeps)
    fake.fail_next = [gmail_403(reason)]
    assert box.fetch("m1").message_id == "m1"
    assert sleeps == [1]


def test_another_403_is_not_retried_and_names_gmails_reason() -> None:
    fake: FakeGmail = FakeGmail()
    sleeps: list[float] = []
    box: Mailbox = open_box(fake, sleeps)
    fake.fail_next = [gmail_403("insufficientPermissions")]
    with pytest.raises(MailboxError, match="403 insufficientPermissions"):
        box.fetch("m1")
    assert sleeps == []


def test_a_revoked_grant_is_an_auth_error() -> None:
    fake: FakeGmail = FakeGmail(revoked=True)
    with pytest.raises(MailboxAuthError, match="invalid_grant"):
        open_box(fake)


def test_an_expired_access_token_is_refreshed_once() -> None:
    fake: FakeGmail = FakeGmail()
    fake.add("m1", make_eml("a@bank.example", "Alert", "<p>x</p>"), OCT_10)
    box: Mailbox = open_box(fake)
    fake.fail_next = [401]
    assert box.fetch("m1").message_id == "m1"


def test_revocation_mid_sync_is_an_auth_error() -> None:
    fake: FakeGmail = FakeGmail()
    box: Mailbox = open_box(fake)
    fake.revoked = True
    with pytest.raises(MailboxAuthError):
        list(box.search(["a@bank.example"], datetime(2026, 10, 8, tzinfo=UTC)))


def test_a_missing_message_is_a_mailbox_error_not_an_auth_error() -> None:
    fake: FakeGmail = FakeGmail()
    with pytest.raises(MailboxError) as raised:
        open_box(fake).fetch("gone")
    assert not isinstance(raised.value, MailboxAuthError)


@pytest.mark.skipif(not FIXTURE_EMAILS, reason="fixtures are local-only")
@pytest.mark.parametrize("path", FIXTURE_EMAILS, ids=lambda p: p.stem)
def test_real_alerts_parse_the_same_after_the_gmail_round_trip(path: Path) -> None:
    raw: bytes = path.read_bytes()
    fake: FakeGmail = FakeGmail()
    fake.add("m1", raw, OCT_10)
    rebuilt: bytes = open_box(fake).fetch("m1").content
    original: EmailArtifact = parse_eml(raw)
    again: EmailArtifact = parse_eml(rebuilt)
    assert again == original
    templates: Templates = load_seed_templates(SEED_DIR)
    assert interpret(select_email_candidates(templates.email, again.sender, again.subject), again).extraction == \
        interpret(select_email_candidates(templates.email, original.sender, original.subject), original).extraction


# --- discovery (slice 23): a query of words excluding senders, and metadata only -----------------------------

from hisaab.ingest.mailbox import DISCOVERY_TERMS, SNIPPET_CHARS, Peek  # noqa: E402


def test_discover_asks_for_the_words_excluding_senders_and_the_user_since_a_time() -> None:
    fake: FakeGmail = FakeGmail(page_size=2)
    for i in range(3):
        fake.add(f"m{i}", make_eml("alerts@harbour-bank.example", f"Rs.10 debited {i}", "<p>debited</p>"), OCT_10 + i)
    since: datetime = datetime(2026, 7, 20, tzinfo=UTC)
    ids, token = open_box(fake).discover(DISCOVERY_TERMS, ["alerts@bank.example", "cards@bank.example"], since)
    assert (ids, token) == (["m2", "m1"], "2")  # one page, and where the next starts
    assert fake.queries == [f"{DISCOVERY_TERMS} -from:alerts@bank.example -from:cards@bank.example -from:me "
                            f"after:{int(since.timestamp())}"]
    assert DISCOVERY_TERMS == '(debited OR credited OR "transaction alert" OR UPI OR "statement of account")'


def test_peek_reads_headers_and_gmails_preview_never_the_body() -> None:
    fake: FakeGmail = FakeGmail()
    body: str = "<p>" + "Rs.1,250.00 has been debited from your account. " * 10 + "</p>"
    fake.add("m1", make_eml('"Harbour Bank" <Alerts@Harbour-Bank.example>', "Debit alert", body), OCT_10)
    peek: Peek = open_box(fake).peek("m1")
    assert (peek.message_id, peek.sender, peek.subject) == ("m1", "alerts@harbour-bank.example", "Debit alert")
    assert peek.snippet.startswith("Rs.1,250.00 has been debited") and len(peek.snippet) <= SNIPPET_CHARS == 160
    assert peek.received_at == datetime.fromtimestamp(OCT_10 / 1000, UTC)
    assert fake.formats == ["metadata"] and fake.metadata_headers == [["From", "Subject"]]


def test_discover_keeps_the_query_short_by_dropping_the_last_exclusions() -> None:
    """Slice 23 review: the ignore list grows week after week; a query past Gmail's limits would fail every sweep.
    Exclusions are a speed-up (the sweep filters in code too), so the ones at the end of the list are dropped."""
    from hisaab.ingest.mailbox import MAX_QUERY_CHARS
    fake: FakeGmail = FakeGmail()
    exclude: list[str] = [f"news{i:03d}@long-newsletter-domain.example" for i in range(300)]
    open_box(fake).discover(DISCOVERY_TERMS, exclude, datetime(2026, 7, 20, tzinfo=UTC))
    query: str = fake.queries[0]
    assert len(query) <= MAX_QUERY_CHARS
    assert query.startswith(DISCOVERY_TERMS + " -from:news000@") and query.endswith("-from:me after:1784505600")
    assert "news299@" not in query
