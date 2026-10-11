"""Synthetic statements matching the RULED and LINES specs in test_statement_spec.py."""

from tests.pdf_factory import make_pdf

EDGES = [30, 80, 250, 330, 400, 470, 560]  # Date | Narration | Ref | Withdrawal | Deposit | Balance
LABELS = ["Date", "Narration", "Ref", "Withdrawal", "Deposit", "Balance"]


def ruled_page(rows, *, first=False, summary=None, footer=True, labels=LABELS, header_top=100):
    """rows: tuples of cell texts (6 cells; "" for empty). Returns a factory page dict."""
    texts = []
    if first:
        texts += [(40, 30, "Example Bank"), (40, 45, "Statement of account"), (40, 60, "Account No : 123456781234")]
    texts += [(EDGES[i] + 3, header_top, label) for i, label in enumerate(labels)]
    top = header_top + 15
    for row in rows:
        texts += [(EDGES[i] + 3, top, cell) for i, cell in enumerate(row) if cell]
        top += 15
    for line in summary or []:
        texts.append((40, top, line))
        top += 15
    if footer:
        texts.append((40, 700, "Example Bank Ltd, registered office"))
    return {"texts": texts, "vlines": [(x, header_top - 5, top) for x in EDGES]}


def ruled_pdf(*pages, password=None):
    return make_pdf(list(pages), password)


def card_page(lines, *, first=False):
    """lines: (text, rupee_amount_or_None, plus_sign) -> a card line "<text> [+] ₹ <amount>"."""
    texts, rupees = [], []
    if first:
        texts += [(40, 30, "Example Card Statement"), (40, 45, "Card No. 4321XXXXXXXX5678"),
                  (40, 60, "Previous Dues"), (120, 60, "1,000.00"), (40, 75, "Total Due"), (120, 75, "1,450.00")]
        rupees += [(110, 60, "C"), (110, 75, "C")]
    texts += [(40, 100, "DATE & TIME"), (200, 100, "DESCRIPTION"), (450, 100, "AMOUNT")]
    top = 115
    for text, amount, plus in lines:
        texts.append((40, top, text))
        if amount:
            if plus:
                texts.append((430, top, "+"))
            rupees.append((440, top, "C"))
            texts.append((450, top, amount))
        top += 15
    texts.append((40, 700, "Page 1 of 1"))
    return {"texts": texts, "rupee_texts": rupees}
