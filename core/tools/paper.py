"""A page and a PDF from the same blocks (shared 2026-11-12, c32): the board
pack (c29) and the donor letters lay out one list of blocks, so the page,
the PDF and Tim always say the same thing.

A block is a tuple:

- ``("title", text)``: the heading, bold
- ``("text", text)``: a paragraph
- ``("small", text)``: a quiet line under the rest (the checks, a letterhead's address)
- ``("gap",)``: a little space
- ``("table", heading, rows)``: rows of ``(label, dollars, is_total)``; heading may be ""

Standard library only (invariant 9): the page is plain escaped HTML with no
script, and the PDF is written by hand in PDF 1.4 with the standard
Helvetica faces, which every reader carries, so no font ships and none is
guessed.
"""

from __future__ import annotations

from html import escape

Block = tuple

_CSS = (
    "body{font-family:-apple-system,Helvetica,Arial,sans-serif;max-width:46rem;margin:2rem auto;"
    "padding:0 1rem;color:#16233F;background:#FAF6EE}h1{font-size:1.4rem}h2{font-size:1.05rem;"
    "margin-top:1.6rem}table{width:100%;border-collapse:collapse}td{padding:.3rem 0;"
    "border-bottom:1px solid #E6E0D2}td.n{text-align:right;white-space:nowrap}"
    "tr.t td{font-weight:600}p.c{color:#55617A}div.g{height:1rem}p{margin:.4rem 0}"
)


def html(title: str, blocks: list[Block]) -> str:
    """The blocks as one plain page with no script."""
    body = []
    for b in blocks:
        if b[0] == "title":
            body.append(f"<h1>{escape(b[1])}</h1>")
        elif b[0] == "text":
            body.append(f"<p>{escape(b[1])}</p>")
        elif b[0] == "small":
            body.append(f'<p class="c">{escape(b[1])}</p>')
        elif b[0] == "gap":
            body.append('<div class="g"></div>')
        elif b[0] == "table":
            if b[1]:
                body.append(f"<h2>{escape(b[1])}</h2>")
            body.append("<table>")
            for label, dollars, total in b[2]:
                cls = ' class="t"' if total else ""
                body.append(
                    f'<tr{cls}><td>{escape(label)}</td><td class="n">{escape(dollars)}</td></tr>'
                )
            body.append("</table>")
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{escape(title)}</title><style>{_CSS}</style></head>"
        f"<body>{''.join(body)}</body></html>\n"
    )


# ---- the PDF -------------------------------------------------------------------------

_W, _H, _M = 612, 792, 54  # US Letter, 0.75 inch margins
_RIGHT = _W - _M


def _pdf_text(text: str) -> str:
    """A string the standard fonts can show: Latin-1, with PDF's escapes."""
    safe = text.encode("latin-1", "replace").decode("latin-1")
    return safe.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


_AFM = {c: 556 for c in "0123456789$"} | {",": 278, ".": 278, "-": 333, "/": 278, " ": 278}
"""Helvetica's own widths (its AFM, per 1,000 of the size), the same in the
bold face, for every character a dollar amount uses, so money right-aligns
exactly; any other character counts as 600, wider than Helvetica's lowercase,
so a label wraps early rather than late."""


def _width(text: str, size: float) -> float:
    """Measured width of ``text`` in Helvetica at ``size`` points."""
    return sum(_AFM.get(c, 600) for c in text) * size / 1000


def _wrap(text: str, size: float, room: float) -> list[str]:
    words, lines, line = text.split(), [], ""
    for w in words:
        trial = f"{line} {w}".strip()
        if _width(trial, size) > room and line:
            lines.append(line)
            line = w
        else:
            line = trial
    return [*lines, line] if line else lines


def pdf(blocks: list[Block]) -> bytes:
    """The blocks as a PDF, one or more Letter pages."""
    pages: list[list[str]] = [[]]
    y = _H - _M

    def need(height: float) -> None:
        nonlocal y
        if y - height < _M:
            pages.append([])
            y = _H - _M

    def text(x: float, size: float, s: str, bold: bool = False) -> None:
        font = "F2" if bold else "F1"
        pages[-1].append(f"BT /{font} {size} Tf {x:.1f} {y:.1f} Td ({_pdf_text(s)}) Tj ET")

    def line(size: float, s: str, bold: bool = False, gap: float = 4) -> None:
        nonlocal y
        for part in _wrap(s, size, _RIGHT - _M):
            need(size + gap)
            y -= size + gap
            text(_M, size, part, bold)

    for b in blocks:
        if b[0] == "title":
            line(16, b[1], bold=True)
        elif b[0] == "text":
            line(10, b[1], gap=6)
        elif b[0] == "small":
            y -= 2
            line(9, b[1])
        elif b[0] == "gap":
            y -= 8
        elif b[0] == "table":
            y -= 8
            if b[1]:
                line(12, b[1], bold=True, gap=8)
            for label, dollars, total in b[2]:
                room = _RIGHT - _M - _width(dollars, 10) - 12
                parts = _wrap(label, 10, room) or [""]
                need(15 * len(parts))
                for n, part in enumerate(parts):
                    y -= 15
                    text(_M, 10, part, total)
                    if n == 0:
                        text(_RIGHT - _width(dollars, 10), 10, dollars, total)
            y -= 4
    return _document(pages)


def _document(pages: list[list[str]]) -> bytes:
    """Laid-out pages as the bytes of a PDF 1.4 file."""
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "",  # the page tree, once the pages are numbered
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
    ]
    kids = []
    for ops in pages:
        stream = "\n".join(ops)
        objects.append(
            f"<< /Length {len(stream.encode('latin-1'))} >>\nstream\n{stream}\nendstream"
        )
        content = len(objects)
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {_W} {_H}] "
            f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents {content} 0 R >>"
        )
        kids.append(f"{len(objects)} 0 R")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for n, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(out)
