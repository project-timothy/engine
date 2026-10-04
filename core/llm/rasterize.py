"""Page images for a document no model can read as a file (issue #296).

A scanned PDF has no text layer: it is a picture of a page wrapped in PDF
structure. Every adapter hits the same wall on one, from a different side. The
Claude Agent SDK seat can only Read the file, and reading a PDF whose bytes are
a JPEG tells the model nothing, which is why the live sessions of 2026-09-16
shelled out and rendered their own pages (issue #265, and the reason that
session kept Bash). The two API adapters put the file on the wire and hope the
provider rasterizes server-side: Anthropic does, a small local model behind an
OpenAI-compatible endpoint does not, so a scanned invoice was unextractable on
the local tier altogether.

Doing it here settles both. The ENGINE renders the pages, in Python, with no
system package to install (:mod:`pypdfium2` is a wheel with the PDFium renderer
inside it; see ``docs/decisions/2026-09-17-rasterize-image-only-pdfs-in-python.md``).
Adapters then carry pictures, which every provider understands, and the seat
session shrinks to ``Read`` with nothing left to shell out for.

The rules this module keeps:

- **A document with a text layer is never rendered.** The extractor already
  inlines that text (row 7.10), a text PDF reads perfectly as a file, and
  rendering one would cost time and tokens for nothing. :func:`has_text_layer`
  asks ``pypdf``, the same reader ``core.agents.ap.extraction`` uses, so the
  two can never disagree about a file.
- **A render that cannot happen is not a failure.** Encrypted, malformed,
  truncated, missing, a page PDFium refuses: :func:`render_attachment` returns
  ``None`` and the caller attaches the file exactly as it did before. A
  document still reaches the model and comes back ``needs_ocr`` if nobody can
  read it, which is the review path that already exists.
- **Bounded.** :data:`MAX_RENDER_PAGES` pages and :data:`MAX_RENDER_BYTES` of
  PNG, whichever comes first, so a 300-page scan cannot fill a disk or a
  transport buffer. The first page always renders; what is cut is said out
  loud in :meth:`RenderedPages.note` so the model reports it instead of
  guessing what it could not see.
- **Nothing is written unless a caller asks.** :func:`render_pdf` returns
  bytes. :func:`write_pages` is the only thing that touches a disk, it writes
  only into the directory it is handed, and the names are the engine's
  (``page-1.png``), never anything read out of the document.

The PNG is encoded here with ``zlib`` and ``struct`` (8-bit RGB, filter 0, one
IDAT). PDFium hands back a raw bitmap and the usual way to save it is Pillow,
which would be a second dependency for forty lines of the format's simplest
case.
"""

from __future__ import annotations

import struct
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from core.llm.gateway import Attachment

PDF_MIME = "application/pdf"
PAGE_MIME = "image/png"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

RENDER_DPI = 150
"""What a scan is rendered at. 150 DPI puts a US Letter page at 1275 by 1650,
which is around the long edge Claude's vision stack works at and comfortably
past what an OCR-grade read of 8 point type needs; 72 (the PDF's own unit) is
unreadable and 300 quadruples the bytes for detail no invoice carries."""

MAX_RENDER_PAGES = 6
"""How many pages of one document are rendered. An AP document is an invoice,
a receipt or a statement page: the run of live 2026 documents is one to three
pages, and six covers the long ones without letting a 300-page scan through.
Anything past it is named in the note rather than silently dropped."""

MAX_RENDER_BYTES = 12 * 1024 * 1024
"""The ceiling on the pages of one document, together. The seat reads them
back through the CLI transport, which base64-inflates by ~4/3, so 12 MB
becomes ~16 MB against the adapter's 32 MB buffer and leaves the rest of the
transcript room (docs/lessons.md, "Oversize inputs fail fast"). The API adapters inline the
same bytes in a request body. A colour photo scan can run several MB a page,
which is the case this bounds."""

PAGES_DIRNAME = "pages"
"""Where a caller with a scratch directory puts them (the seat adapter's
per-call directory, issue #265). The engine's name, not the document's."""

PAGE_NAME = "page-{number}.png"

_PNG_COMPRESSION = 6
"""zlib's default. Level 9 costs several times the CPU for a few percent on a
page image, and these files live for one call."""


def pdf_text(path: Path) -> str:
    """A PDF's text layer, or ``""`` when it has none and when anything at all
    goes wrong reading it.

    The one PDF text reader in the engine:
    ``core.agents.ap.extraction.read_document_text`` calls this for its PDF
    branch, so "does this document have text" has exactly one answer.
    """
    try:
        from pypdf import PdfReader
    except ModuleNotFoundError:  # pragma: no cover - dependency-gated
        return ""
    from core.engine.timebox import pdf_deadline

    try:
        with pdf_deadline(Path(path).name):
            reader = PdfReader(str(path))
            return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception:
        # A file pypdf cannot parse has no text layer as far as any caller is
        # concerned; the rasterizer gets its turn and the model gets the file.
        return ""


def has_text_layer(path: Path) -> bool:
    """Whether a model reading this file would find words in it."""
    return bool(pdf_text(path).strip())


@dataclass(frozen=True)
class RenderedPages:
    """The page images of one document, in order, in memory.

    ``page_count`` is what the document holds; ``pages`` is what was rendered,
    which is fewer when a cap cut it. The bytes are PNG, so an adapter either
    writes them out (the seat) or base64-inlines them (the API adapters).
    """

    source: Path
    pages: tuple[bytes, ...]
    page_count: int
    dpi: int = RENDER_DPI
    mime: str = PAGE_MIME

    @property
    def truncated(self) -> bool:
        return len(self.pages) < self.page_count

    def note(self) -> str:
        """One line of prose for the model: what these pictures are, where
        they came from, and what it is NOT looking at."""
        shown = len(self.pages)
        which = "page 1" if shown == 1 else f"pages 1 to {shown}"
        text = (
            f"The attached page image{'' if shown == 1 else 's'} are {which} of "
            f"{self.page_count} of the document {self.source.name}, rendered by the "
            f"engine at {self.dpi} DPI because the file has no text layer to read."
        )
        if self.truncated:
            text += (
                f" Pages {shown + 1} to {self.page_count} were not rendered (the engine's "
                "render cap): say so in warnings rather than guessing what they hold."
            )
        return text


def render_attachment(
    attachment: Attachment,
    *,
    dpi: int = RENDER_DPI,
    max_pages: int = MAX_RENDER_PAGES,
    max_bytes: int = MAX_RENDER_BYTES,
) -> RenderedPages | None:
    """The one call an adapter makes: page images for this attachment, or
    ``None`` when it should travel exactly as it always has.

    ``None`` means every one of: it is not a PDF, it has a text layer, the
    renderer is not installed, or the render failed. The caller does not need
    to tell those apart, because the answer is the same in all four.
    """
    if attachment.mime != PDF_MIME:
        return None
    if has_text_layer(attachment.path):
        return None
    return render_pdf(attachment.path, dpi=dpi, max_pages=max_pages, max_bytes=max_bytes)


def render_pdf(
    path: Path,
    *,
    dpi: int = RENDER_DPI,
    max_pages: int = MAX_RENDER_PAGES,
    max_bytes: int = MAX_RENDER_BYTES,
) -> RenderedPages | None:
    """Render up to the caps and return the PNG bytes, or ``None`` if this
    file cannot be rendered at all. Never raises."""
    try:
        import pypdfium2 as pdfium
    except ModuleNotFoundError:  # pragma: no cover - dependency-gated
        return None
    document = None
    try:
        document = pdfium.PdfDocument(str(path))
        page_count = len(document)
        if page_count < 1:
            return None
        pages: list[bytes] = []
        total = 0
        for index in range(min(page_count, max_pages)):
            page = document[index]
            bitmap = page.render(scale=dpi / 72)
            data = _png(bitmap)
            if pages and total + len(data) > max_bytes:
                break  # the first page always rides; the rest are budgeted
            pages.append(data)
            total += len(data)
        if not pages:
            return None
        return RenderedPages(source=Path(path), pages=tuple(pages), page_count=page_count, dpi=dpi)
    except Exception:
        # Encrypted, malformed, missing, a PDFium refusal, an unknown bitmap
        # mode: the document goes to the model the way it always did.
        return None
    finally:
        closer = getattr(document, "close", None)
        if closer is not None:
            try:
                closer()
            except Exception:  # pragma: no cover - a close that fails changes nothing
                pass


def write_pages(rendered: RenderedPages, out_dir: Path) -> tuple[Path, ...]:
    """Write the pages into ``out_dir`` (made if missing) as ``page-1.png``
    upward, and return the paths in order.

    The only disk write in this module, and it happens nowhere but inside the
    directory the caller named: the file names are built from a counter, so no
    part of the document's own name or content can steer a write.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for number, data in enumerate(rendered.pages, start=1):
        target = out_dir / PAGE_NAME.format(number=number)
        target.write_bytes(data)
        written.append(target)
    return tuple(written)


# ---- the PNG, with the standard library ---------------------------------------


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _rows(buffer: bytes, *, width: int, height: int, stride: int, mode: str) -> Iterator[bytes]:
    """One row of pixel bytes per page row, converted to what the PNG header
    declares. PDFium hands back its native byte order (BGR on this build) and
    may pad each row past the pixels, which ``stride`` measures."""
    channels = 1 if mode == "L" else len(mode)
    if channels not in (1, 3, 4):
        raise ValueError(f"unhandled bitmap mode {mode!r}")
    for y in range(height):
        start = y * stride
        row = buffer[start : start + width * channels]
        if len(row) < width * channels:
            raise ValueError("the bitmap is shorter than its own geometry")
        yield row if mode in ("L", "RGB") else _to_rgb(row, mode, width)


def _to_rgb(row: bytes, mode: str, width: int) -> bytes:
    """Channel order, in slice assignments rather than a Python loop over two
    million pixels."""
    source = bytearray(row)
    out = bytearray(width * 3)
    if mode == "BGR":
        out[0::3], out[1::3], out[2::3] = source[2::3], source[1::3], source[0::3]
    elif mode in ("RGBA", "RGBX"):
        out[0::3], out[1::3], out[2::3] = source[0::4], source[1::4], source[2::4]
    elif mode in ("BGRA", "BGRX"):
        out[0::3], out[1::3], out[2::3] = source[2::4], source[1::4], source[0::4]
    else:
        raise ValueError(f"unhandled bitmap mode {mode!r}")
    return bytes(out)


def _png(bitmap) -> bytes:
    """A PDFium bitmap as a PNG: 8-bit greyscale or RGB, no interlacing, every
    row written with filter type 0."""
    width, height, mode, stride = bitmap.width, bitmap.height, bitmap.mode, bitmap.stride
    greyscale = mode == "L"
    raw = bytearray()
    for row in _rows(bytes(bitmap.buffer), width=width, height=height, stride=stride, mode=mode):
        raw += b"\x00"
        raw += row
    header = struct.pack(">IIBBBBB", width, height, 8, 0 if greyscale else 2, 0, 0, 0)
    return b"".join(
        (
            PNG_MAGIC,
            _chunk(b"IHDR", header),
            _chunk(b"IDAT", zlib.compress(bytes(raw), _PNG_COMPRESSION)),
            _chunk(b"IEND", b""),
        )
    )
