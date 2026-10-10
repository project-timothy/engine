"""Root pytest configuration: the whole suite runs under the strict run-key
audit (issue #153). Any job that reads a tenant-config value, a ``--param``,
or the vendor registry that its run key does not declare turns into an
error result, so the job's own evals fail with the missing inputs named.
Production runs never set ENGINE_KEY_AUDIT and are not traced.

It also points the seat adapter's scratch root (issue #265) at a temporary
directory, so a suite run leaves nothing in the checkout it ran from.
"""

from __future__ import annotations

import contextlib
import os
import re
import zlib
from collections.abc import Iterator
from pathlib import Path

import pytest

from core.engine.runkey import KEY_AUDIT_ENV
from core.llm.adapters.claude_sdk_complete import SCRATCH_ROOT_ENV

# Since the tenant cut (2026-09-22) the owner's shell exports
# ENGINE_TENANTS_ROOT for interactive runs and the launchd wrappers export it
# for the scheduled sessions, pointing at the private tenant clone, which
# holds no demo tenant. The suite tests the checkout it runs in: its own
# tenants/ (demo, the templates), whatever the host says. The names come off
# HERE, at import, because test modules resolve the root at import time
# (tests/unit/test_config.py), before any fixture runs.
_TENANT_ROOT_NAMES = ("ENGINE_TENANTS_ROOT", "AUDITOR_TENANTS_DIR")
_PREVIOUS_TENANT_ROOTS = {name: os.environ.pop(name, None) for name in _TENANT_ROOT_NAMES}


@pytest.fixture(autouse=True, scope="session")
def _the_suite_tests_its_own_tenants():
    """Put the host's tenant-root names back when the session ends."""
    try:
        yield
    finally:
        for name, value in _PREVIOUS_TENANT_ROOTS.items():
            if value is not None:
                os.environ[name] = value


@pytest.fixture(autouse=True, scope="session")
def _strict_run_key_audit():
    previous = os.environ.get(KEY_AUDIT_ENV)
    os.environ[KEY_AUDIT_ENV] = "strict"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(KEY_AUDIT_ENV, None)
        else:
            os.environ[KEY_AUDIT_ENV] = previous


@pytest.fixture(autouse=True, scope="session")
def _scratch_root_off_the_checkout(tmp_path_factory):
    """The SDK adapter makes a directory per call for the session to write in,
    and its default root is beside the run (``.llm-scratch``). A test must not
    put that in the working tree, so the whole suite gets a temporary one."""
    previous = os.environ.get(SCRATCH_ROOT_ENV)
    os.environ[SCRATCH_ROOT_ENV] = str(tmp_path_factory.mktemp("llm-scratch"))
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(SCRATCH_ROOT_ENV, None)
        else:
            os.environ[SCRATCH_ROOT_ENV] = previous


PAGE_BREAK = "\f"
"""Where ``minimal_pdf`` starts a new page. A form feed is the character a
printer breaks a page on, ``pypdf`` round-trips it through nothing, and it
keeps a multi-page case readable as ONE string in a JSON case file
(``core/llm/eval_sets/scan_group``, row 7.13b)."""


@contextlib.contextmanager
def unreadable(folder: Path, mode: int = 0o311) -> Iterator[Path]:
    """Make ``folder`` unlistable for the block, then restore it.

    The 2026-09-13 regressions simulate macOS's silent per-program readdir
    denial with ``chmod``. Root (or any process with CAP_DAC_OVERRIDE, as in
    the product image) reads through it, so the helper first proves the
    denial holds and skips with the reason when it does not (public issue
    #9): the suite is green whichever user runs it, and a run that cannot
    simulate the denial says so instead of passing or failing falsely.
    """
    folder.chmod(mode)
    try:
        try:
            os.listdir(folder)
        except PermissionError:
            pass
        else:
            pytest.skip("this user ignores file permissions (root), so chmod cannot deny a read")
        yield folder
    finally:
        folder.chmod(0o755)


def minimal_pdf(text: str) -> bytes:
    """A real, minimal PDF 1.4 whose text layer is ``text``, one page per
    form-feed-separated chunk.

    Hand-built (a Helvetica content stream per page, a correct xref table),
    no dependency. ``pypdf`` parses it and ``extract_text()`` returns the
    chunk line for line. Tests use this in place of the old ``b"%PDF stub"``
    bytes: the W-9 lane reads every PDF's text layer before any model call
    (honesty audit 2026-09-03, 02-F7), so a plain-text file named ``.pdf`` is
    no longer the input the engine sees.

    Text with no form feed produces the same bytes it always has: the object
    layout below is the single-page one generalised, not replaced, and the
    committed eval-set documents pin that byte for byte.
    """

    def _escape(line: str) -> str:
        return line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    def _stream(chunk: str) -> bytes:
        ops = ["BT", "/F1 12 Tf", "14 TL", "72 720 Td"]
        for i, line in enumerate(chunk.split("\n") or [""]):
            if i:
                ops.append("T*")
            ops.append(f"({_escape(line)}) Tj")
        ops.append("ET")
        return "\n".join(ops).encode("latin-1", errors="replace")

    chunks = text.split(PAGE_BREAK)
    # 1 catalog, 2 pages, 3..(2+n) the pages, (3+n) the font, then one content
    # stream each. For n == 1 that is exactly the old numbering (page 3, font
    # 4, contents 5).
    n = len(chunks)
    font_no = 3 + n
    kids = " ".join(f"{3 + i} 0 R" for i in range(n))
    streams = [_stream(chunk) for chunk in chunks]

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode(),
    ]
    objects += [
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        f"/Resources << /Font << /F1 {font_no} 0 R >> >> "
        f"/Contents {font_no + 1 + i} 0 R >>".encode()
        for i in range(n)
    ]
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    objects += [
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
        for stream in streams
    ]
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for n, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n"
    ).encode()
    return bytes(out)


def image_only_pdf(pages: int = 1, *, size: int = 8) -> bytes:
    """A real, minimal PDF whose every page is ONE image and no text at all:
    the shape a flatbed scanner and a fax machine produce, and the shape
    ``pypdf`` returns ``""`` for.

    ``minimal_pdf`` above is its opposite (a Helvetica content stream), and the
    pair is what the rasterizer's cases are built from: a document with a text
    layer must never be rendered, and one without it must be. The image is a
    ``size`` by ``size`` deflated RGB checkerboard scaled over the whole page,
    so a rendered page has both black and white pixels in it and a test can
    prove something was actually drawn rather than a blank sheet written.
    """
    rows = bytearray()
    for y in range(size):
        for x in range(size):
            value = 0 if (x + y) % 2 else 255
            rows += bytes((value, value, value))
    image = zlib.compress(bytes(rows), 9)

    image_no = 3 + pages
    kids = " ".join(f"{3 + i} 0 R" for i in range(pages))
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode(),
    ]
    objects += [
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        f"/Resources << /XObject << /Im0 {image_no} 0 R >> >> "
        f"/Contents {image_no + 1 + i} 0 R >>".encode()
        for i in range(pages)
    ]
    objects.append(
        f"<< /Type /XObject /Subtype /Image /Width {size} /Height {size} "
        f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode "
        f"/Length {len(image)} >>\nstream\n".encode()
        + image
        + b"\nendstream"
    )
    draw = b"q 612 0 0 792 0 0 cm /Im0 Do Q"
    objects += [
        b"<< /Length " + str(len(draw)).encode() + b" >>\nstream\n" + draw + b"\nendstream"
    ] * pages

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n"
    ).encode()
    return bytes(out)


@pytest.fixture
def demo_without_authority(tmp_path_factory, monkeypatch) -> Path:
    """The demo tenant as the first tenant is today: no authority.toml, so
    the queue and the lanes take the path they took before #435. For the
    tests of that path's own mechanics (cards, overrides, the run lock,
    unattended lists); tests/unit/test_authority_queue.py covers a tenant
    with the file, and test_authority_equivalence.py holds the path itself
    byte-identical."""
    import shutil

    root = tmp_path_factory.mktemp("tenants")
    shutil.copytree(Path(__file__).resolve().parent / "tenants" / "demo", root / "demo")
    (root / "demo" / "authority.toml").unlink()
    monkeypatch.setenv("ENGINE_TENANTS_ROOT", str(root))
    return root


# The first tenant's project-code scheme, the one these tests speak (#340).
FIRST_TENANT_CODES = {
    "pattern": r"(?i)p\s?n?\s?(\d{2})\s?_?\s?(\d{4})",
    "canonical": "P{0}_{1}",
    "tag_pattern": r"\bPN?(?:(\d{2})[_-])?(\d{4})\b",
}


@pytest.fixture
def demo_with_project_codes(tmp_path_factory, monkeypatch) -> Path:
    """The demo tenant with a project-code scheme in [books.cost_object].
    The engine ships none (#340), so a test of tag or hint parsing names the
    scheme it relies on here."""
    import json
    import shutil

    root = tmp_path_factory.mktemp("tenants")
    shutil.copytree(Path(__file__).resolve().parent / "tenants" / "demo", root / "demo")
    toml = root / "demo" / "tenant.toml"
    text = toml.read_text()
    for name, value in FIRST_TENANT_CODES.items():
        line = f"{name} = {json.dumps(value)}"
        text, n = re.subn(rf"^{name} = .*$", lambda _m, line=line: line, text, count=1, flags=re.M)
        if not n:
            text = text.replace("[books.cost_object]\n", f"[books.cost_object]\n{line}\n", 1)
    toml.write_text(text)
    monkeypatch.setenv("ENGINE_TENANTS_ROOT", str(root))
    return root
