"""A minimal PDF writer for tests: text at given positions, vertical rules, and
optional RC4 password protection (PDF 1.4 standard security handler, V1/R2).

Statement fixtures are real and local-only, so the committed tests build their
own statements with this instead. Coordinates are pdfplumber's: x from the
left, `top` from the top of a 600x800 page.
"""

import hashlib
import struct
from typing import Any, TypeAlias

WIDTH: int = 600
HEIGHT: int = 800
PageSpec: TypeAlias = dict[str, Any]  # {"texts": [(x, top, text)], "vlines": [...], "rupee_texts": [...]}
_PAD: bytes = bytes.fromhex("28BF4E5E4E758A4164004E56FFFA01082E2E00B6D0683E802F0CA9FE6453697A")


def _rc4(key: bytes, data: bytes) -> bytes:
    s: list[int] = list(range(256))
    j: int = 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) % 256
        s[i], s[j] = s[j], s[i]
    out, i, j = bytearray(), 0, 0
    for byte in data:
        i = (i + 1) % 256
        j = (j + s[i]) % 256
        s[i], s[j] = s[j], s[i]
        out.append(byte ^ s[(s[i] + s[j]) % 256])
    return bytes(out)


def _escape(text: str) -> bytes:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)").encode("latin-1")


def _content(page: PageSpec) -> bytes:
    ops: list[bytes] = []
    for x, top, bottom in page.get("vlines", []):
        ops.append(f"0.5 w {x} {HEIGHT - top} m {x} {HEIGHT - bottom} l S".encode())
    for font, items in (("F1", page.get("texts", [])), ("F2", page.get("rupee_texts", []))):
        for x, top, text in items:
            ops.append(b"BT /" + font.encode() + b" 9 Tf " + f"{x} {HEIGHT - top - 9}".encode()
                       + b" Td (" + _escape(text) + b") Tj ET")
    return b"\n".join(ops)


def make_pdf(pages: list[PageSpec], password: str | None = None) -> bytes:
    """pages: [{"texts": [(x, top, text)], "vlines": [(x, top, bottom)], "rupee_texts": [(x, top, "C")]}].
    rupee_texts use a font named ITFRupee, like the HDFC card statement's ₹ glyph."""
    n: int = len(pages)
    objs: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [" + b" ".join(f"{5 + 2 * i} 0 R".encode() for i in range(n))
           + f"] /Count {n} >>".encode(),
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        4: (f"<< /Type /Font /Subtype /Type1 /BaseFont /ITFRupee /FontDescriptor {5 + 2 * n} 0 R "
            "/FirstChar 67 /LastChar 67 /Widths [600] >>").encode(),
        5 + 2 * n: (b"<< /Type /FontDescriptor /FontName /ITFRupee /Flags 32 /FontBBox [0 0 600 700] "
                    b"/ItalicAngle 0 /Ascent 700 /Descent 0 /CapHeight 700 /StemV 80 >>"),
    }
    streams: dict[int, bytes] = {}
    for i, page in enumerate(pages):
        page_id, content_id = 5 + 2 * i, 6 + 2 * i
        objs[page_id] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {WIDTH} {HEIGHT}] "
                         f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents {content_id} 0 R >>").encode()
        streams[content_id] = _content(page)

    file_id: bytes = hashlib.md5(b"hisaab-test").digest()
    trailer_extra: bytes = b""
    key: bytes | None = None
    if password is not None:
        p_value: int = -4  # all permissions
        padded: bytes = (password.encode("latin-1") + _PAD)[:32]
        owner: bytes = _rc4(hashlib.md5(padded).digest()[:5], padded)  # owner password = user password
        key = hashlib.md5(padded + owner + struct.pack("<i", p_value) + file_id).digest()[:5]
        user: bytes = _rc4(key, _PAD)
        enc_id: int = 6 + 2 * n
        objs[enc_id] = (b"<< /Filter /Standard /V 1 /R 2 /O <" + owner.hex().encode() + b"> /U <"
                        + user.hex().encode() + b"> /P " + str(p_value).encode() + b" >>")
        trailer_extra = f" /Encrypt {enc_id} 0 R".encode()

    out: bytearray = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for num in sorted(set(objs) | set(streams)):
        offsets[num] = len(out)
        if num in streams:
            data: bytes = streams[num]
            if key is not None:
                data = _rc4(hashlib.md5(key + struct.pack("<I", num)[:3] + b"\x00\x00").digest()[:10], data)
            out += f"{num} 0 obj\n<< /Length {len(data)} >>\nstream\n".encode() + data + b"\nendstream\nendobj\n"
        else:
            out += f"{num} 0 obj\n".encode() + objs[num] + b"\nendobj\n"
    xref: int = len(out)
    size: int = max(offsets) + 1
    out += f"xref\n0 {size}\n0000000000 65535 f \n".encode()
    for num in range(1, size):
        out += f"{offsets[num]:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {size} /Root 1 0 R /ID [<{file_id.hex()}> <{file_id.hex()}>]".encode()
            + trailer_extra + f" >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(out)
