"""The email fingerprint (tech-design §4.9): sender, masked subject, static phrases, structural bucket.

Two alerts of one format must share a fingerprint whatever their amounts, dates, references and names;
a different format (credit instead of debit, a redesign, another sender) must not.
"""

from email.message import EmailMessage

from hisaab.parsing.email_interpreter import EmailArtifact, parse_eml
from hisaab.parsing.fingerprint import Fingerprint, fingerprint, mask_subject
from tests.gmail_fake import make_eml

BANK: str = "alerts@lantern.example"
SKELETON: str = """<html><body><table><tr><td><p>Dear {who},</p>
<p>Rs.{amount} has been {verb} from account **{last4} to VPA {vpa} {payee} on {day}.</p>
<p>Your UPI transaction reference number is {ref}.</p>
<p>If you did not authorise this, please call us at once.</p></td></tr></table></body></html>"""
REDESIGN: str = """<html><body><div><h1>Alert</h1><table><tr><td>x</td></tr></table><table><tr><td>
<span>Rs.{amount} has been {verb} from account **{last4} to VPA {vpa} {payee} on {day}.</span>
<span>Your UPI transaction reference number is {ref}.</span></td></tr></table>
<span>If you did not authorise this, please call us at once.</span></div></body></html>"""


def alert(amount: str = "1,250.00", verb: str = "debited", last4: str = "4321", vpa: str = "corner@okbank",
          payee: str = "Corner Grocer", day: str = "05-10-26", ref: str = "612345678901", who: str = "Asha Rao",
          skeleton: str = SKELETON, sender: str = BANK, subject: str = "You have done a UPI txn") -> EmailArtifact:
    html: str = skeleton.format(amount=amount, verb=verb, last4=last4, vpa=vpa, payee=payee, day=day, ref=ref, who=who)
    return parse_eml(make_eml(sender, subject, html))


def test_two_alerts_of_one_format_share_a_fingerprint() -> None:
    one: Fingerprint = fingerprint(alert())
    two: Fingerprint = fingerprint(alert(amount="89.50", last4="9876", vpa="ravi.k@upibank", payee="RAVI KUMAR",
                                         day="17-09-26", ref="698765432109", who="Vikram Shah"))
    assert one.value == two.value


def test_a_credit_alert_in_the_same_skeleton_is_another_format() -> None:
    debit: Fingerprint = fingerprint(alert())
    credit: Fingerprint = fingerprint(alert(verb="credited"))
    assert debit.bucket == credit.bucket and debit.value != credit.value


def test_a_redesign_with_the_same_words_is_another_format() -> None:
    old: Fingerprint = fingerprint(alert())
    new: Fingerprint = fingerprint(alert(skeleton=REDESIGN))
    assert old.static_lines == new.static_lines
    assert old.bucket != new.bucket and old.value != new.value


def test_another_sender_is_another_format() -> None:
    assert fingerprint(alert()).value != fingerprint(alert(sender="alerts@harbour.example")).value


def test_static_lines_keep_fixed_lower_case_phrases_and_no_names() -> None:
    assert fingerprint(alert()).static_lines == ["has been debited from account", "transaction reference number is",
                                                 "you did not authorise this please call us at once"]


def test_the_subject_is_masked() -> None:
    assert mask_subject("Rs.1,250.00 debited on 05-10-26 from a/c 1234") == "₹# debited on <date> from a/c #"
    assert mask_subject("Statement for 2026-09-30 ready") == "Statement for <date> ready"
    assert mask_subject("INR 500 paid") == "₹# paid"


def test_the_subject_takes_part() -> None:
    assert fingerprint(alert()).value != fingerprint(alert(subject="Money left your account")).value
    assert fingerprint(alert(subject="Rs.10 debited")).value == fingerprint(alert(subject="Rs.2,000.00 debited")).value


def test_a_plain_text_email_has_the_text_bucket() -> None:
    msg: EmailMessage = EmailMessage()
    msg["From"], msg["Subject"], msg["Date"] = BANK, "Alert", "Sat, 10 Oct 2026 09:15:00 +0530"
    msg.set_content("Dear Asha,\nRs.10 has been debited from account 1234.\nThank you for banking with us.")
    text_only: Fingerprint = fingerprint(parse_eml(msg.as_bytes()))
    assert text_only.bucket == "text"
    assert text_only.static_lines == ["has been debited from account", "you for banking with us"]
