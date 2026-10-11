"""Change one amount inside a real statement PDF (a copy, in memory), for the
build-plan check that a corrupted amount makes validation fail with the right gap.

The fixture indexes its objects with a compressed cross-reference stream, so no
byte may move: the edited content stream is recompressed and padded back to its
original length (zlib stops at its own end marker and ignores the padding).
"""

import re
import zlib

_STREAM = re.compile(rb"(stream\r?\n)(.*?)(\r?\nendstream)", re.S)


def replace_drawn_text(data: bytes, old: str, new: str) -> bytes:
    """Replace the one place `old` is drawn as a PDF string literal with `new` (same length).
    Raises unless it is drawn exactly once, so the caller knows precisely what changed."""
    if len(old) != len(new):
        raise ValueError("old and new must be the same length")
    target, replacement = b"(" + old.encode() + b")", b"(" + new.encode() + b")"
    hits = 0

    def fix(m: re.Match) -> bytes:
        nonlocal hits
        packed = m.group(2)
        try:
            text = zlib.decompress(packed)
        except zlib.error:
            return m.group(0)
        if target not in text:
            return m.group(0)
        hits += text.count(target)
        edited = text.replace(target, replacement)
        for level in (9, 8, 7, 6, 5, 4, 3, 2, 1):
            repacked = zlib.compress(edited, level)
            if len(repacked) <= len(packed):
                return m.group(1) + repacked + b"\0" * (len(packed) - len(repacked)) + m.group(3)
        raise ValueError("edited stream does not fit back in place")

    out = _STREAM.sub(fix, data)
    if hits != 1:
        raise ValueError(f"'{old}' is drawn {hits} times, expected exactly once")
    assert len(out) == len(data)
    return out
