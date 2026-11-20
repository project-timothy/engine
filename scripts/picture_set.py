"""Drawing a made-up eval set of document PICTURES, shared by the scripts
that build one (``scripts/receipt_read_set.py``, card R1;
``scripts/letter_read_set.py`` and ``scripts/statement_read_set.py``, card R2).

Each document is drawn as a page (PDFium, already pinned, renders it),
roughed up the way phone photos are (faded print, a hand-written slip, a
crooked shot, two pages), and written as a PNG, or as an image-only PDF of
several pages. Nothing has a text layer: the model reads the picture, which
is what a page shows it.

**Built on a Mac.** PDFium draws the same page with different pixels on a
Mac and on Linux (CI's Linux, 2026-11-18), so a set's pictures are its
script's as built on macOS. Two checks hold a set: everywhere, every file
matches its ``manifest.json`` (the SHA-256 of each file the script wrote)
and the plain JSON matches a rebuild byte for byte; on a Mac, each picture
draws exactly what a rebuild draws (:func:`drawn`), however zlib packed it.
Rebuilding on another system draws new pictures: run the real-model eval
again before committing them.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import sys
import zlib
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.llm.rasterize import _png  # noqa: E402

SCALE = 2.6  # points to pixels: a receipt about 570 pixels wide


def _escape(line: str) -> bytes:
    raw = line.encode("cp1252")
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def _pdf(pages: list[bytes], sizes: list[tuple[float, float]], resources: list[bytes]) -> bytes:
    """A PDF from content streams, one per page (a correct xref table)."""
    n = len(pages)
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>"]
    kids = " ".join(f"{3 + i} 0 R" for i in range(n))
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode())
    for i, (w, h) in enumerate(sizes):
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {w:.0f} {h:.0f}] /Contents "
            f"{3 + n + i} 0 R /Resources ".encode()
            + resources[i]
            + b" >>"
        )
    for stream in pages:
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    return _assemble(objects)


def _assemble(objects: list[bytes]) -> bytes:
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


_FONTS = (
    b"<< /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding "
    b"/WinAnsiEncoding >> /F2 << /Type /Font /Subtype /Type1 /BaseFont /Courier-Bold "
    b"/Encoding /WinAnsiEncoding >> /F3 << /Type /Font /Subtype /Type1 /BaseFont "
    b"/Times-Italic /Encoding /WinAnsiEncoding >> >> >>"
)


def _page_stream(
    lines: list[str], look: str, rng: random.Random, width: int = 0
) -> tuple[bytes, float, float]:
    """One page as a content stream, and its size in points: a till
    receipt's width unless ``width`` names a wider sheet (a letter, a
    statement)."""
    hand = look == "hand"
    size, lead = (15, 26) if hand else (10, 15)
    width = width or (260 if hand else 220)
    height = 40 + lead * len(lines)
    grey = 0.55 if look == "faded" else 0.0
    ops = [b"1 g 0 0 %d %d re f" % (width, height), b"%.2f g" % grey, b"BT"]
    y = height - 30
    for i, line in enumerate(lines):
        font = b"/F3" if hand else (b"/F2" if i == 0 or "TOTAL" in line.upper() else b"/F1")
        if hand:  # a slanted, wandering hand: each line its own small tilt and drift
            angle = rng.uniform(-0.05, 0.05)
            x = 16 + rng.uniform(-4, 8)
            ops.append(b"%s %d Tf" % (font, size + rng.choice((0, 1, 2))))
            ops.append(b"1 %.3f %.3f 1 %.1f %.1f Tm" % (angle, 0.18 - angle, x, y))
        else:
            ops.append(b"%s %d Tf" % (font, size))
            ops.append(b"1 0 0 1 12 %.1f Tm" % y)
        ops.append(b"(" + _escape(line) + b") Tj")
        y -= lead
    ops.append(b"ET")
    return b"\n".join(ops), float(width), float(height)


def _render(pdf: bytes, look: str, rng: random.Random) -> SimpleNamespace:
    """The page as a greyscale bitmap, roughed up for its look."""
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(pdf)
    try:
        bitmap = document[0].render(scale=SCALE, grayscale=True)
        width, height, stride = bitmap.width, bitmap.height, bitmap.stride
        raw = bytes(bitmap.buffer)
    finally:
        document.close()
    pixels = bytearray(b"".join(raw[y * stride : y * stride + width] for y in range(height)))
    if look == "faded":  # thermal paper gone pale: ink lifts toward the paper
        pixels = bytearray(
            pixels.translate(bytes(min(255, 110 + v * 145 // 255) for v in range(256)))
        )
    if look == "tilted":
        pixels, width, height = _tilt(pixels, width, height, rng.uniform(0.04, 0.07))
    # the phone's shot: the page a little grey, and specks
    pixels = bytearray(pixels.translate(bytes(min(v, 236) for v in range(256))))
    for _ in range(width * height // 180):
        at = rng.randrange(len(pixels))
        pixels[at] = max(0, pixels[at] - rng.randrange(40, 120))
    return SimpleNamespace(width=width, height=height, mode="L", stride=width, buffer=bytes(pixels))


def _tilt(pixels: bytearray, width: int, height: int, angle: float):
    """The page shot crooked: a shear each way, which reads as a small turn."""
    pad = int(abs(angle) * max(width, height)) + 4
    w, h = width + 2 * pad, height + 2 * pad
    out = bytearray(b"\xd8" * (w * h))  # the table under the receipt
    for y in range(height):
        dx = pad + int((y - height / 2) * angle)
        row = pixels[y * width : (y + 1) * width]
        for x in range(0, width, 1):
            ty = pad + y + int((x - width / 2) * -angle)
            out[ty * w + dx + x] = row[x]
    return out, w, h


def _image_pdf(bitmaps: list[SimpleNamespace]) -> bytes:
    """A scanned bill: each page one greyscale image, no text layer."""
    n = len(bitmaps)
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>"]
    kids = " ".join(f"{3 + i} 0 R" for i in range(n))
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode())
    for i, bm in enumerate(bitmaps):
        w, h = bm.width / SCALE, bm.height / SCALE
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {w:.1f} {h:.1f}] /Contents "
            f"{3 + n + i} 0 R /Resources << /XObject << /Im0 {3 + 2 * n + i} 0 R >> >> >>".encode()
        )
    for bm in bitmaps:
        w, h = bm.width / SCALE, bm.height / SCALE
        stream = f"q {w:.1f} 0 0 {h:.1f} 0 0 cm /Im0 Do Q".encode()
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    for bm in bitmaps:
        data = zlib.compress(bm.buffer, 9)
        objects.append(
            b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceGray "
            b"/BitsPerComponent 8 /Filter /FlateDecode /Length %d >>\nstream\n"
            % (bm.width, bm.height, len(data))
            + data
            + b"\nendstream"
        )
    return _assemble(objects)


def document(name: str, look: str, lines: list[str], *, width: int = 0) -> tuple[str, bytes]:
    """(file name, bytes) for one document: a PNG, or an image-only PDF when
    a ``"\\f"`` line breaks it into pages."""
    rng = random.Random(name)  # noqa: S311  noise for a picture, not a secret
    pages: list[list[str]] = [[]]
    for line in lines:
        if line == "\f":
            pages.append([])
        else:
            pages[-1].append(line)
    bitmaps = []
    for chunk in pages:
        stream, w, h = _page_stream(chunk, look, rng, width)
        bitmaps.append(_render(_pdf([stream], [(w, h)], [_FONTS]), look, rng))
    if len(bitmaps) > 1:
        return f"{name}.pdf", _image_pdf(bitmaps)
    return f"{name}.png", _png(bitmaps[0])


_STREAM = re.compile(rb"<<(.*?)>>\nstream\n(.*?)\nendstream", re.S)


def drawn(name: str, data: bytes) -> object:
    """What a file of the set holds, independent of how zlib packed it.

    A PNG: its header and its pixel rows (every IDAT chunk joined and
    inflated). A PDF: every stream's dictionary without its /Length, and its
    contents inflated where it is /FlateDecode, plus the file around the
    streams with each object offset left out (an offset moves with a stream's
    packed length). Anything else: its bytes."""
    if name.endswith(".png"):
        header, rows, at = b"", b"", 8
        while at < len(data):
            size = int.from_bytes(data[at : at + 4], "big")
            kind, body = data[at + 4 : at + 8], data[at + 8 : at + 8 + size]
            if kind == b"IHDR":
                header = body
            elif kind == b"IDAT":
                rows += body
            at += 12 + size
        return ("png", data[:8], header, zlib.decompress(rows))
    if name.endswith(".pdf"):
        streams = []
        for found in _STREAM.finditer(data):
            meta, body = found.group(1), found.group(2)
            if b"/FlateDecode" in meta:
                body = zlib.decompress(body)
            streams.append((re.sub(rb"/Length \d+", b"", meta), body))
        outside = _STREAM.sub(b"<stream>", data).split(b"xref", 1)[0]
        return ("pdf", outside, streams)
    return data


MANIFEST = "manifest.json"


def manifest(files: dict[str, bytes]) -> bytes:
    """The SHA-256 of every file the script wrote, by path."""
    sums = {path: hashlib.sha256(data).hexdigest() for path, data in sorted(files.items())}
    return (json.dumps(sums, indent=2) + "\n").encode()


def picture(path: str) -> bool:
    """A drawn file (Mac-built), as against plain JSON."""
    return path.startswith("documents/")


def main(argv: list[str], target: Path, files: dict[str, bytes], what: str) -> int:
    """Write the set under ``target``, or with ``--check`` compare the
    committed one with a rebuild, picture by picture (Mac only)."""
    if "--check" in argv:
        if sys.platform != "darwin":
            print("the pictures are Mac-built: run --check on a Mac (the tests hold the manifest)")
            return 1
        stale = ["*"]
        if target.is_dir():
            stale = [
                p
                for p, data in files.items()
                if drawn(p, (target / p).read_bytes()) != drawn(p, data)
            ]
        print("\n".join(stale) or f"{what}: the committed set is this script's")
        return 1 if stale else 0
    files = {**files, MANIFEST: manifest(files)}
    for path, data in files.items():
        out = target / path
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
    print(f"wrote {what} to {target.relative_to(ROOT)}")
    return 0
