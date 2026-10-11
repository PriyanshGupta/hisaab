# Seed email template specs

Hand-authored specs (tech-design §4.2), one JSON file per bank format, written
with the authoring harness:

    python -m hisaab template-skeleton fixtures/emails/.../x.eml > spec.json
    python -m hisaab try-template spec.json fixtures/emails/.../x.eml --text   # edit, rerun Slice 11
loads them into `parser_templates`; until then the tests load them directly.
Each was written against a real fixture in `fixtures/emails/`. Fixtures are
local-only, so the patterns use the bank's fixed wording and never values from
an email.

| File | Fixture | Notes |
|---|---|---|
| `hdfc_upi_debit.json` | `upi/hdfc_upi_debit_01.eml` | Subject "You have done a UPI txn" (there's an emoji before it, so the pattern is not anchored). The wording is "Rs.N is debited from your account ending NNNN towards VPA x (NAME) on dd-mm-yy". The docs' example ("has been debited from account") doesn't match the real email, and the fixture wins. |
| `hdfc_upi_credit.json` | `upi/hdfc_upi_credit_01.eml` | Subject "Account update for your HDFC Bank A/c". HDFC uses that subject for **other** emails too: the card e-mandate notice (`credit_cards/hdfc_cc_mandate_01.eml`) has the same sender and subject. The mandate says "INR N for MERCHANT", not "Rs.N has been successfully credited", so the amount pattern fails, which is a critical field. The template declines (§4.3 step 6) instead of turning a notice into a credit. |

| `icici_cc_purchase.json` | `credit_cards/icici_cc_purchase_01.eml`, `_02.eml` | ICICI moved this alert from `credit_cards@icicibank.com` (2025) to `credit_cards@icici.bank.in` (2026) without changing the format, so `match.sender` lists both. The date reads like "Mon DD, YYYY", parsed with `date:%b %d, %Y`. The "Available Credit Limit" is **not** the balance: the email says it covers every card the primary holder has, supplementary cards included. A foreign-currency alert won't say "INR", so the amount fails, the template declines, and the email goes to tier 2, which sends non-INR amounts to review (PRD §10.3). The repayment alert has its own subject. |

Neither HDFC alert states a time or a balance, so both are declared `{"required": false}`.
The sender is `alerts@hdfcbank.bank.in`, as in the fixtures (the docs say `hdfcbank.net`).
