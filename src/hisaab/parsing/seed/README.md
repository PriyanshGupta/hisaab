# Seed template specs

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

| `hdfc_cc_mandate_paid.json` | `credit_cards/hdfc_cc_mandate_paid_01.eml`, `_02.eml` | Same sender and subject again: HDFC confirms each card e-mandate payment ("Your X bill, set up through E-mandate (Auto payment), has been successfully paid…") **after** sending a separate charge alert for the same money. The alert is the record, so this is `non_transactional`. Recorded as well, every subscription counted twice, and its "Mandate ID" (the same every month) looked like a reference number and merged months of confirmations into one phantom charge. Older emails say "SI Hub ID" where newer ones say "Mandate ID" (`_02`). Exported from the owner's inbox (2026-10-19), where every confirmation had its own charge alert. |
| `icici_cc_purchase.json` | `credit_cards/icici_cc_purchase_01.eml`, `_02.eml` | ICICI moved this alert from `credit_cards@icicibank.com` (2025) to `credit_cards@icici.bank.in` (2026) without changing the format, so `match.sender` lists both. The date reads like "Mon DD, YYYY", parsed with `date:%b %d, %Y`. The "Available Credit Limit" is **not** the balance: the email says it covers every card the primary holder has, supplementary cards included. A foreign-currency alert won't say "INR", so the amount fails, the template declines, and the email goes to tier 2, which sends non-INR amounts to review (PRD §10.3). The repayment alert has its own subject. |

Neither HDFC alert states a time or a balance, so both are declared `{"required": false}`.
The sender is `alerts@hdfcbank.bank.in`, as in the fixtures (the docs say `hdfcbank.net`).

## Statements

`artifact_type: "statement"` specs (tech-design §4.6, amended 2026-10-11: rows are
built from words in `ruled` or `line_pattern` mode instead of pdfplumber's table
finder). Run them the same way; a password-protected PDF reads its password from
an environment variable: `try-template spec.json x.pdf --password-env STMT_PW`.

| File | Fixture | Notes |
|---|---|---|
| `hdfc_acct_statement.json` | `statements/hdfc_acct_statement_01.pdf` | `ruled`: the column dividers are 1pt rectangles. The PDF squashes words ("HDFCBANKLIMITED", "Statementof account"). The header row is on page 1 only, so `resumes_after` restarts the table after each page's header block. Narrations wrap at a fixed width, sometimes across a page break. Opening and closing come from the STATEMENT SUMMARY row at the end, which also prints the debit and credit counts the fixture test checks the rows against. Amounts use Western grouping. |
| `hdfc_cc_statement.json` | `statements/hdfc_cc_statement_01.pdf` | `line_pattern`: there are no rules, and page 1's table sits at different x positions from the later pages. ₹ is drawn by a symbol font as "C"; the PDF reader maps it back. A leading "+" marks a credit (`signed_single_column`, `unsigned_means: debit`). Date and time share a cell. Domestic and International tables each have a header; a cardholder line after each header is skipped. Opening = PREVIOUS STATEMENT DUES, closing = TOTAL AMOUNT DUE. HDFC's own summary differs by a few paise from its components, which is inside the ₹1 tolerance. |
