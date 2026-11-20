"""The pages CI photographs at a phone's width (tests/browser/pages.py,
structure review proposal 4) all render, every one Tim's own, so the photo
job never breaks for a reason a unit test could have caught."""

from __future__ import annotations

import importlib.util
from pathlib import Path

PAGES = Path(__file__).resolve().parents[1] / "browser" / "pages.py"


def _pages() -> dict[str, str]:
    spec = importlib.util.spec_from_file_location("page_shots", PAGES)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.pages()


def test_every_page_a_person_sees_renders_for_the_photos():
    pages = _pages()
    start = {
        f"start-{s}"
        for s in ("support", "letter", "where", "reviewer", "driving", "heard", "letter-read")
    }
    talk = {"talk", "talk-first", "talk-sign-in"}  # Tim's own page (card T1)
    read = {"receipt-read", "receipt-read-which"}  # a photo read as it lands (card R1)
    read |= {"catch-up-statement"}  # a statement read off a picture (card R2)
    read |= {"catch-up-read"}  # a pile read, listed for one yes (2026-11-19)
    assert (
        set(pages)
        == {"receipt", "catch-up", "month", "month-empty", "month-timed-out"} | start | talk | read
    )
    for name, html in pages.items():
        assert '<svg class="mark"' in html, name
        # A missionary's own setup questions come before her ministry has a box.
        assert ("for Grace Fellowship" in html) == (name not in start), name


def test_the_setup_question_photos_show_each_screen():
    pages = _pages()
    assert "How does support reach you?" in pages["start-support"]
    assert 'type=file name=photo accept="image/*" capture=environment' in pages["start-letter"]
    assert "Who looks over your spending?" in pages["start-reviewer"]
    assert "Hannah Cole, in Nakuru, Kenya" in pages["start-heard"]
    read = pages["start-letter-read"]  # card R2: what her letter reads, for her tap
    assert "Employer Identification Number 84-1234567. Is that right?" in read
    assert 'name="right" value="yes"' in read and "<script" not in read


def test_the_statement_photo_shows_its_question_on_the_catch_up_page():
    """Card R2: a statement that adds up, its Keep them drawn by the page."""
    page = _pages()["catch-up-statement"]
    assert "The lines add up to both. Keep them?" in page and "Keep them" in page
    assert "asks.push" in page and "id=ask" in page


def test_the_talk_photos_show_her_next_thing_the_conversation_and_the_sign_in():
    pages = _pages()
    talk = pages["talk"]
    assert "Next for you" in talk and "4,300 rupees" in talk and "Add a receipt photo" in talk
    assert "Say hi, or ask Tim" in pages["talk-first"]
    assert "Continue with Face ID or fingerprint" in pages["talk-sign-in"]


def test_the_month_photo_shows_a_full_month_and_its_button():
    month = _pages()["month"]
    assert "NPR 4,300.00" in month and "<button>Approve October</button>" in month
    assert "What I checked" in month and "1 bank line still waits on Ruth" in month
    assert "Waiting on what it was for" in month


def test_the_read_back_photos_show_his_line_and_her_answers():
    """The receipt page once Tim read the photo (card R1): the question is
    drawn by the page's own script, from the answer the box sends."""
    pages = _pages()
    assert (
        "asked({token:'t'" in pages["receipt-read"] and "Yes, that's right" in pages["receipt-read"]
    )
    assert (
        "Which is right?" in pages["receipt-read-which"]
        and "even:true" in pages["receipt-read-which"]
    )
