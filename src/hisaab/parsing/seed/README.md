# Seed email template specs

Hand-authored specs (tech-design §4.2), one JSON file per bank format. Slice 11
loads them into `parser_templates`; until then the tests load them directly.
Each was written against a real fixture in `fixtures/emails/`. Fixtures are
local-only, so the patterns use the bank's fixed wording and never values from
an email.

| File | Fixture | Notes |
|---|---|---|
| `hdfc_upi_debit.json` | `upi/hdfc_upi_debit_01.eml` | Subject "You have done a UPI txn" (there's an emoji before it, so the pattern is not anchored). The wording is "Rs.N is debited from your account ending NNNN towards VPA x (NAME) on dd-mm-yy". The docs' example ("has been debited from account") doesn't match the real email, and the fixture wins. |
| `hdfc_upi_credit.json` | `upi/hdfc_upi_credit_01.eml` | Subject "Account update for your HDFC Bank A/c". HDFC uses that subject for **other** emails too: the card e-mandate notice (`credit_cards/hdfc_cc_mandate_01.eml`) has the same sender and subject. The mandate says "INR N for MERCHANT", not "Rs.N has been successfully credited", so the amount pattern fails, which is a critical field. The template declines (§4.3 step 6) instead of turning a notice into a credit. |

Neither alert states a time or a balance, so both are declared `{"required": false}`.
The sender is `alerts@hdfcbank.bank.in`, as in the fixtures (the docs say `hdfcbank.net`).
