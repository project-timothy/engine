"""The engine rasterizes an image-only PDF itself (issue #296).

Issue #265 settled the honest half: the seat session ran Bash, because a
scanned PDF with no text layer is unreadable to the model as a file and the
live sessions rendered it to page images themselves. The tool was the only
thing standing between the adapter and a Read-only session, so this row moves
the rendering into the engine, where it serves EVERY adapter: the seat reads
the pages off disk, and the two API adapters inline them instead of a PDF
their provider may not be able to look at.

Nothing here calls a model. The rendering is real (``pypdfium2`` renders the
fixture PDFs this suite builds), so a page that comes back is proof the
picture was drawn, not a blank sheet with the right dimensions.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

from conftest import image_only_pdf, minimal_pdf
from core.llm import Attachment
from core.llm.rasterize import (
    MAX_RENDER_PAGES,
    PAGE_MIME,
    PNG_MAGIC,
    RENDER_DPI,
    has_text_layer,
    pdf_text,
    render_attachment,
    write_pages,
)

# ---- reading a PNG back, with nothing but the standard library ---------------


def _chunks(data: bytes):
    assert data[:8] == PNG_MAGIC, "not a PNG"
    at = 8
    while at < len(data):
        (length,) = struct.unpack(">I", data[at : at + 4])
        kind = data[at + 4 : at + 8]
        payload = data[at + 8 : at + 8 + length]
        (crc,) = struct.unpack(">I", data[at + 8 + length : at + 12 + length])
        assert crc == zlib.crc32(kind + payload) & 0xFFFFFFFF, f"{kind!r} checksum"
        yield kind, payload
        at += 12 + length


def _header(data: bytes) -> tuple[int, int, int, int]:
    for kind, payload in _chunks(data):
        if kind == b"IHDR":
            width, height, depth, colour = struct.unpack(">IIBB", payload[:10])
            return width, height, depth, colour
    raise AssertionError("no IHDR")


def _pixels(data: bytes) -> bytes:
    """Every row's bytes, the per-row filter byte dropped (the encoder writes
    filter 0, so the rows need no unfiltering)."""
    width, height, _depth, colour = _header(data)
    stride = width * (3 if colour == 2 else 1)
    raw = zlib.decompress(b"".join(payload for kind, payload in _chunks(data) if kind == b"IDAT"))
    assert len(raw) == height * (stride + 1)
    out = bytearray()
    for row in range(height):
        start = row * (stride + 1)
        assert raw[start] == 0, "the encoder writes filter type 0 on every row"
        out += raw[start + 1 : start + 1 + stride]
    return bytes(out)


def _scan(tmp_path: Path, pages: int = 1, name: str = "scan.pdf") -> Attachment:
    path = tmp_path / name
    path.write_bytes(image_only_pdf(pages))
    return Attachment(path, "application/pdf")


def _typed(tmp_path: Path, text: str = "ACME LLC\nINVOICE 4471\nTOTAL 1875.00") -> Attachment:
    path = tmp_path / "invoice.pdf"
    path.write_bytes(minimal_pdf(text))
    return Attachment(path, "application/pdf")


# ---- the text layer is the whole decision ------------------------------------


def test_the_text_layer_is_what_decides_whether_a_pdf_is_rendered(tmp_path):
    assert has_text_layer(_typed(tmp_path).path) is True
    assert has_text_layer(_scan(tmp_path).path) is False


def test_the_text_reader_is_the_one_the_ap_extractor_already_uses(tmp_path):
    """Row 7.10 inlines a document's text layer in the ask. The rasterizer asks
    the same question of the same reader rather than a second opinion of its
    own, so a file can never be both "has text" to the extractor and "no text"
    to the renderer."""
    from core.agents.ap.extraction import read_document_text

    for path in (_typed(tmp_path).path, _scan(tmp_path).path):
        assert bool(read_document_text(path).strip()) is has_text_layer(path)
        assert read_document_text(path) == pdf_text(path)


def test_a_pdf_with_a_text_layer_is_never_rendered(tmp_path):
    assert render_attachment(_typed(tmp_path)) is None


def test_an_image_attachment_is_never_rendered(tmp_path):
    """A photo of a receipt is already a picture: every adapter can send it as
    it is, and rendering is for PDFs alone."""
    photo = tmp_path / "receipt.png"
    photo.write_bytes(PNG_MAGIC + b"not really an image")
    assert render_attachment(Attachment(photo, "image/png")) is None


# ---- the render --------------------------------------------------------------


def test_an_image_only_pdf_becomes_one_png_per_page(tmp_path):
    rendered = render_attachment(_scan(tmp_path, pages=3))

    assert rendered is not None
    assert rendered.page_count == 3
    assert len(rendered.pages) == 3
    assert rendered.truncated is False
    assert rendered.mime == PAGE_MIME == "image/png"
    for page in rendered.pages:
        width, height, depth, colour = _header(page)
        # US Letter at the module's DPI, within pdfium's rounding.
        assert abs(width - round(612 * RENDER_DPI / 72)) <= 2
        assert abs(height - round(792 * RENDER_DPI / 72)) <= 2
        assert (depth, colour) == (8, 2), "8-bit RGB"


def test_the_rendered_page_carries_the_picture_and_not_a_blank_sheet(tmp_path):
    """The fixture draws a black and white checkerboard over the whole page, so
    a render that produced a white sheet (a page that never drew, or a bitmap
    read at the wrong stride) fails here rather than looking plausible."""
    rendered = render_attachment(_scan(tmp_path))
    assert rendered is not None
    pixels = _pixels(rendered.pages[0])

    assert min(pixels) < 32, "no dark pixel: nothing was drawn"
    assert max(pixels) > 224, "no light pixel: the page is not a scan"


def test_the_page_cap_stops_a_long_scan_and_says_so(tmp_path):
    """A 300-page scan must not fill a disk or a transport buffer."""
    rendered = render_attachment(_scan(tmp_path, pages=MAX_RENDER_PAGES + 3))

    assert rendered is not None
    assert len(rendered.pages) == MAX_RENDER_PAGES
    assert rendered.page_count == MAX_RENDER_PAGES + 3
    assert rendered.truncated is True
    note = rendered.note()
    assert str(MAX_RENDER_PAGES + 3) in note
    assert "not rendered" in note


def test_the_byte_budget_stops_a_render_the_transport_could_not_carry(tmp_path):
    """The pages ride the CLI transport base64-inflated by ~4/3 and the API
    adapters inline them in a request body; the budget is what keeps a colour
    scan from filling either."""
    rendered = render_attachment(_scan(tmp_path, pages=4), max_bytes=1)

    assert rendered is not None
    assert len(rendered.pages) == 1, "one page always renders, budget or no budget"
    assert rendered.truncated is True


def test_the_note_tells_the_model_what_it_is_looking_at(tmp_path):
    rendered = render_attachment(_scan(tmp_path, pages=2, name="fax-from-a-vendor.pdf"))
    assert rendered is not None
    note = rendered.note()

    assert "fax-from-a-vendor.pdf" in note
    assert str(RENDER_DPI) in note
    assert "text layer" in note


# ---- a render that cannot happen falls back, never fails ---------------------


def test_a_malformed_pdf_falls_back_to_the_file_itself(tmp_path):
    """The eval the issue asks for by name: a broken file still reaches the
    model as it is, rather than turning a document into an extraction failure
    the renderer invented."""
    broken = tmp_path / "half-a-scan.pdf"
    broken.write_bytes(b"%PDF-1.4 truncated before anything useful")

    assert render_attachment(Attachment(broken, "application/pdf")) is None


def test_an_encrypted_pdf_falls_back_to_the_file_itself(tmp_path):
    from pypdf import PdfWriter

    writer = PdfWriter()
    source = tmp_path / "plain.pdf"
    source.write_bytes(image_only_pdf(1))
    writer.append(str(source))
    writer.encrypt("a-password-the-engine-does-not-have")
    locked = tmp_path / "locked.pdf"
    with locked.open("wb") as handle:
        writer.write(handle)

    assert render_attachment(Attachment(locked, "application/pdf")) is None


def test_a_missing_file_falls_back_rather_than_raising(tmp_path):
    assert render_attachment(Attachment(tmp_path / "gone.pdf", "application/pdf")) is None


# ---- writing the pages out ---------------------------------------------------


def test_the_pages_are_written_with_deterministic_names_inside_the_given_directory(tmp_path):
    rendered = render_attachment(_scan(tmp_path, pages=2, name="a vendor's own $can.pdf"))
    assert rendered is not None
    out = tmp_path / "scratch" / "pages"

    paths = write_pages(rendered, out)

    assert [p.name for p in paths] == ["page-1.png", "page-2.png"]
    for path in paths:
        assert path.parent == out, "the module writes nowhere but the directory it was given"
        assert "$can" not in str(path), "the names are the engine's, never the document's"
        assert path.read_bytes()[:8] == PNG_MAGIC
    assert sorted(p.name for p in out.iterdir()) == ["page-1.png", "page-2.png"]


def test_writing_the_same_render_twice_produces_the_same_files(tmp_path):
    """Deterministic names and deterministic bytes: a retry writes the same
    pages over the same names instead of growing the directory."""
    rendered = render_attachment(_scan(tmp_path, pages=2))
    assert rendered is not None
    out = tmp_path / "pages"

    first = write_pages(rendered, out)
    contents = [p.read_bytes() for p in first]
    second = write_pages(rendered, out)

    assert first == second
    assert [p.read_bytes() for p in second] == contents
    assert len(list(out.iterdir())) == 2
