"""Builds a tiny real PDF for importer tests: each run of text is drawn at a stated x, y and size,
so a test controls the layout data pypdf reports (issue #208 round 5). Invented text only."""

from __future__ import annotations

BULLET = "•"


def _escape(text: str) -> str:
    out = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return out.replace(BULLET, "\\225")  # the bullet in WinAnsiEncoding


def make_pdf(runs: list[tuple], *, size: float = 10) -> bytes:
    """One Letter page; `runs` are (x, y, text), (x, y, text, size) or (x, y, text, size, bold)
    in points, drawn in Helvetica (Helvetica-Bold when bold) at that size (default `size`)."""
    ops = "\n".join(f"BT /F{2 if len(r) > 4 and r[4] else 1} {r[3] if len(r) > 3 else size} Tf "
                    f"{r[0]} {r[1]} Td ({_escape(r[2])}) Tj ET" for r in runs)
    stream = ops.encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R /F2 6 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1, xref)
    return bytes(out)


def bullet(x: float, y: float, words: str, *, gap: float = 12) -> list[tuple[float, float, str]]:
    """A bullet mark at x and its words `gap` points to the right, as a word processor draws it."""
    return [(x, y, BULLET), (x + gap, y, words)]


def margin(x: float, y: float, words: str) -> tuple[float, float, str]:
    """A line exactly as wide as `words` (the same words in reverse order), so the text block's
    right edge sits where `words` ends: what the other full lines of a real page show."""
    return (x, y, " ".join(reversed(words.split())))
