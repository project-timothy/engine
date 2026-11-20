"""Tim reads her IRS letter on the interview's letter screen (Standalone Tim,
card R2).

Hannah takes a picture of her IRS letter and the screen says back
"Your letter reads Hope Springs Ministries Inc., Employer Identification
Number 84-1234567. Is that right?". Her tap fills what she would have typed.
What must never happen: something a model read becoming her answer without
her tap, and the screen getting worse for a box whose reader is off (every
box today). The model is scripted the way the proving ground scripts it: a
``fixture`` tier reading its replies from ``ENGINE_FIXTURE_REPLIES``.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import tomllib
from datetime import date
from functools import partial
from pathlib import Path

import pytest

from core.engine.config import load_tenant
from core.ledger.ledger import DB_FILENAME
from core.onboarding import interview as iv
from core.tools import interview_page as ip
from core.tools import letter_read
from core.tools.mcp_http import _read_letter
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_interview_page import ORIGIN, _multipart, _post, _text

PHOTO = b"\xff\xd8\xff\xe0" + bytes(range(256)) * 4
HOPE = {
    "doc_type": "irs_letter",
    "organization": "nonprofit",
    "legal_name": "Hope Springs Ministries Inc.",
    "ein": "84-1234567",
}


def _on(solo, monkeypatch, *replies: dict, cap: str = "") -> None:  # noqa: F811
    """Switch the reader on in the box's own file and script the model."""
    toml = Path(solo["root"]) / "living-water" / "tenant.toml"
    text = toml.read_text()
    text = text.replace("[llm.jobs]\n", '[llm.jobs]\nletter_read = "fixture"\n', 1)
    if cap:
        text = re.sub(r"(?m)^monthly_usd = \d+$", f"monthly_usd = {cap}", text)
    toml.write_text(text)
    script = Path(solo["root"]).parent / "letter-replies.jsonl"
    script.write_text("".join(json.dumps(r) + "\n" for r in replies))
    monkeypatch.setenv("ENGINE_FIXTURE_REPLIES", str(script))


def _reader(solo):  # noqa: F811
    """The box's own reader, as its door builds it."""
    return partial(_read_letter, "living-water", solo["root"], solo["ledger_root"])


def _calls(solo) -> list[tuple[str, str]]:  # noqa: F811
    db = Path(solo["ledger_root"]) / DB_FILENAME
    if not db.is_file():
        return []
    con = sqlite3.connect(db)
    try:
        return con.execute("SELECT job_type, status FROM llm_calls ORDER BY id").fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()


@pytest.fixture
def at_letter(solo):  # noqa: F811
    """A link whose support screen is answered: she's on her letter."""
    book = iv.InterviewBook(Path(solo["ledger_root"]).parent / iv.INTERVIEWS)
    token = book.mint(name="Hannah Cole", email="hannah@example.org", by="Linda Park", now=1_000)
    _post(book, ip.ANSWER_PATH, {"token": token, "screen": "support", "support": "own_nonprofit"})
    return book, token


def _send_photo(book, token, letter, fields=None):
    headers, body = _multipart({"token": token, "screen": "letter", **(fields or {})}, PHOTO)
    return ip.answer(
        book, ip.ANSWER_PATH, headers, body, ORIGIN, now=lambda: 2_000.0, letter=letter
    )


# ---- off: the screen is what it always was ---------------------------------------------


def test_with_the_reader_off_the_letter_screen_is_byte_for_byte_what_it_was(tmp_path):
    """The letter screen's bytes before card R2 (main 6e8e169), token cut out."""
    book = iv.InterviewBook(tmp_path / "i")
    token = book.mint(name="Hannah Cole", email="hannah@example.org", by="Paul Weston", now=1_000)
    pinned = {
        "": "c3eb19bdce81813bf9e0ad0f34a31f10b9b4c79089336858c3ea169c436480a3",
        iv.words()["letter"]["no_name"]: (
            "f55af50ba3c36f36447746f185b806dc58d8f5e88476530a0870344c7eb17fa5"
        ),
    }
    for error, digest in pinned.items():
        html = ip.screen_html(book, token, "letter", error).replace(token, "TOKEN")
        assert hashlib.sha256(html.encode()).hexdigest() == digest


@pytest.mark.parametrize("on", [False, True])
def test_off_or_with_nothing_to_say_a_photo_with_no_name_asks_for_the_name(
    solo,  # noqa: F811
    monkeypatch,
    at_letter,
    on,
):
    """No line in the file: no model is asked. A bare fixture tier: the
    call fails, is recorded, and the screen answers as it always has."""
    if on:
        _on(solo, monkeypatch)  # nothing scripted
    book, token = at_letter
    status, _, data = _send_photo(book, token, _reader(solo))
    assert status == 200 and iv.words()["letter"]["no_name"] in _text(data)
    assert book.letter(token) is None and "legal_name" not in book.answers(token)
    assert _calls(solo) == ([("letter_read", "transport_failed")] if on else [])


def test_the_month_cap_stops_the_call_before_it_is_made(solo, monkeypatch, at_letter):  # noqa: F811
    _on(solo, monkeypatch, HOPE, cap="0")
    book, token = at_letter
    _, _, data = _send_photo(book, token, _reader(solo))
    assert iv.words()["letter"]["no_name"] in _text(data)
    assert _calls(solo) == [("letter_read", "budget_refused")]


def test_a_typed_name_is_taken_as_always_and_no_model_is_asked(solo, monkeypatch, at_letter):  # noqa: F811
    _on(solo, monkeypatch, HOPE)
    book, token = at_letter
    _, _, data = _send_photo(book, token, _reader(solo), {"legal_name": "Hope Springs Inc"})
    assert "Where do you live and work?" in _text(data)
    assert book.answers(token)["legal_name"] == "Hope Springs Inc"
    assert "ein" not in book.answers(token) and _calls(solo) == []


@pytest.mark.parametrize(
    "reply",
    [
        {**HOPE, "organization": "other"},  # an LLC's or a trust's number
        {**HOPE, "doc_type": "other"},
        {**HOPE, "ein": "84-12345"},  # eight digits
        {**HOPE, "ein": "00-1234567"},  # a prefix the IRS never issues
        {**HOPE, "ein": None},
        {**HOPE, "legal_name": "Internal Revenue Service"},  # the sender, not her
        {**HOPE, "legal_name": "1234"},
        {**HOPE, "legal_name": None},
    ],
)
def test_a_reading_code_cannot_trust_is_never_shown(solo, monkeypatch, at_letter, reply):  # noqa: F811
    _on(solo, monkeypatch, reply)
    book, token = at_letter
    _, _, data = _send_photo(book, token, _reader(solo))
    assert iv.words()["letter"]["no_name"] in _text(data)
    assert "legal_name" not in book.answers(token)


# ---- on: read back, and her tap ---------------------------------------------------------


def test_the_letter_is_read_back_and_nothing_is_kept_until_her_tap(solo, monkeypatch, at_letter):  # noqa: F811
    _on(solo, monkeypatch, HOPE)
    book, token = at_letter
    status, headers, data = _send_photo(book, token, _reader(solo))
    text = _text(data)
    assert status == 200 and "<script" not in data.decode()
    assert "script-src 'none'" in headers["Content-Security-Policy"]
    assert (
        "Your letter reads Hope Springs Ministries Inc., Employer Identification Number "
        "84-1234567. Is that right?"
    ) in text
    assert "Yes, that's right" in text and "No, I'll type it" in text
    assert _calls(solo) == [("letter_read", "ok")]
    # the photo is kept as it always is; nothing read is her answer yet
    assert book.letter(token) == PHOTO
    assert book.answers(token) == {"answered": ["support"], "support": ["own_nonprofit"]}
    assert book.next_screen(token, now=2_000.0) == "letter"


def _tap(book, token, html: bytes, right: str):
    """Her tap: the form the page drew, posted as her phone posts it."""
    form = dict(
        re.findall(r'<input type=hidden name=(?:")?(\w+)(?:")? value="([^"]*)"', html.decode())
    )
    form["right"] = right
    import html as h

    form = {k: h.unescape(v) for k, v in form.items()}
    return _post(book, ip.ANSWER_PATH, form)


def test_her_yes_fills_what_she_would_have_typed(solo, monkeypatch, at_letter):  # noqa: F811
    _on(solo, monkeypatch, HOPE)
    book, token = at_letter
    _, _, data = _send_photo(book, token, _reader(solo))
    _, _, data = _tap(book, token, data, "yes")
    assert "Where do you live and work?" in _text(data)
    answers = book.answers(token)
    assert (
        answers["legal_name"] == "Hope Springs Ministries Inc." and answers["ein"] == "84-1234567"
    )
    heard = iv.heard(answers, name="Hannah Cole")
    assert (
        "Your ministry",
        "Hope Springs Ministries Inc., Employer Identification Number 84-1234567",
    ) in heard["mine"]


def test_her_no_leaves_her_the_box_and_keeps_nothing_read(solo, monkeypatch, at_letter):  # noqa: F811
    _on(solo, monkeypatch, HOPE)
    book, token = at_letter
    _, _, data = _send_photo(book, token, _reader(solo))
    _, _, data = _tap(book, token, data, "no")
    text = _text(data)
    assert iv.words()["letter"]["type_it"] in text and 'name="legal_name"' in data.decode()
    assert "legal_name" not in book.answers(token) and book.letter(token) == PHOTO
    # she types it, and the number stays out
    _, _, data = _post(
        book, ip.ANSWER_PATH, {"token": token, "screen": "letter", "legal_name": "Hope Springs"}
    )
    assert "Where do you live and work?" in _text(data)
    assert book.answers(token)["legal_name"] == "Hope Springs" and "ein" not in book.answers(token)


def test_a_number_that_cannot_be_one_is_never_kept(at_letter):
    book, token = at_letter
    form = {
        "token": token,
        "screen": "letter",
        "legal_name": "Hope",
        "ein": "12-34",
        "right": "yes",
    }
    _post(book, ip.ANSWER_PATH, form)
    assert book.answers(token)["legal_name"] == "Hope" and "ein" not in book.answers(token)


def test_the_file_the_maintainer_gets_is_the_same_whichever_way_the_name_came(solo):  # noqa: F811
    def walk(letter: dict) -> str:
        book = iv.InterviewBook(Path(solo["ledger_root"]).parent / f"w{len(letter)}")
        token = book.mint(name="Hannah Cole", email="hannah@example.org", by="Linda", now=1_000)
        for screen, form in (
            ("support", {"support": ["own_nonprofit"]}),
            ("letter", letter),
            ("where", {"town": "Nakuru, Kenya"}),
            ("reviewer", {"nobody": "yes"}),
            ("driving", {"drives": "no"}),
            ("heard", {"right": "yes"}),
        ):
            assert book.take(token, screen, form, now=2_000) == ""
        return book.ministry_file(token)

    typed = walk({"legal_name": "Hope Springs Ministries Inc."})
    tapped = walk(
        {"legal_name": "Hope Springs Ministries Inc.", "ein": "84-1234567", "right": "yes"}
    )
    assert typed == tapped and tomllib.loads(typed)["ministry"] == "Hope Springs Ministries"


def test_the_box_reads_the_letter_on_its_door(solo, monkeypatch, tmp_path):  # noqa: F811
    """Over HTTP: the door hands the box's own reader to the screen."""
    import threading
    import time
    import urllib.request

    from core.tools.mcp_http import TokenFile, make_server

    _on(solo, monkeypatch, HOPE)
    server = make_server(
        "living-water",
        tenants_root=solo["root"],
        ledger_root=solo["ledger_root"],
        obligations_file=None,
        verifier=TokenFile(tmp_path / "tokens.json"),
        resource=ORIGIN + "/mcp",
        authorization_servers=[ORIGIN],
        port=0,
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        book = iv.InterviewBook(solo["ledger_root"].parent / iv.INTERVIEWS)
        token = book.mint(name="Hannah Cole", email="h@example.org", by="Linda", now=time.time())
        assert book.take(token, "support", {"support": ["own_nonprofit"]}, now=time.time()) == ""
        headers, body = _multipart({"token": token, "screen": "letter"}, PHOTO)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        req = urllib.request.Request(base + ip.ANSWER_PATH, data=body, headers=headers)
        with urllib.request.urlopen(req) as resp:
            html = resp.read().decode()
        assert "Employer Identification Number 84-1234567. Is that right?" in html
    finally:
        server.shutdown()


# ---- the reader itself -----------------------------------------------------------------


def test_code_checks_every_field_of_a_reading():
    reply = letter_read.LetterReply(**{**HOPE, "legal_name": "  Hope\tSprings  Ministries Inc. "})
    assert letter_read.checked(reply) == letter_read.LetterReading(
        "Hope Springs Ministries Inc.", "84-1234567"
    )
    assert letter_read.checked(letter_read.LetterReply(**{**HOPE, "ein": "841234567"})).ein == (
        "84-1234567"
    )
    assert iv.ein_of("84 1234567") == "84-1234567"
    for bad in ("", "84-123456", "841234567x", "07-1234567", "96-1234567"):
        assert iv.ein_of(bad) == ""


def test_the_reader_is_named_only_and_a_document_job():
    from core.llm.policy import DOCUMENT_JOB_TYPES, NAMED_ONLY

    assert letter_read.JOB in NAMED_ONLY and letter_read.JOB in DOCUMENT_JOB_TYPES


def test_a_document_tier_the_box_refuses_never_sees_the_letter(solo, monkeypatch, at_letter):  # noqa: F811
    _on(solo, monkeypatch, HOPE)
    toml = Path(solo["root"]) / "living-water" / "tenant.toml"
    text = toml.read_text()
    assert "[llm.tiers]\n" in text
    toml.write_text(text.replace("[llm.tiers]\n", "[llm]\ndocument_tiers = []\n\n[llm.tiers]\n", 1))
    assert load_tenant("living-water", tenants_root=solo["root"]).llm.document_tiers == []
    book, token = at_letter
    _, _, data = _send_photo(book, token, _reader(solo))
    assert iv.words()["letter"]["no_name"] in _text(data)
    assert _calls(solo) == [("letter_read", "documents_refused")]


def test_the_words_are_plain():
    for line in iv.all_words():
        assert "—" not in line
    assert date.today()  # the reader reads at the box's own day
