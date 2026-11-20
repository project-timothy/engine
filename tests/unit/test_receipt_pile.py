"""A pile of receipts, the owner's read of 2026-11-19: "it's almost always in
piles, by weeks or a couple of weeks", and Tim never nags for them. One
photo, several, or dozens: Tim's page has a button for each way, and where
the ministry has the reader on (card R1), each photo in a pile is read the
moment it lands, the page lists what it read, and one yes counts them all.
A photo Tim couldn't read waits for her word, as a pile's always has."""

from __future__ import annotations

import pytest

from core.tools import receipt_read
from core.tools import talk_page as tp
from core.tools.receipts import ReceiptDesk, page, take_photo
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_door_receipt_date import _zone
from tests.unit.test_door_receipts import JPEG
from tests.unit.test_receipt_read import NOW, _calls, _files, _reads, _shoprite, _tap
from tests.unit.test_talk_page import _get, _post, talk  # noqa: F401  (fixture)


def _pile(solo, *photos: bytes):  # noqa: F811
    _zone(solo, "Asia/Kathmandu")
    desk = ReceiptDesk()
    token = desk.issue_pile("daniel-reyes", "Daniel Reyes", by="daniel-reyes")
    bodies = []
    for photo in photos:
        status, body = take_photo(
            desk,
            token,
            photo,
            tenant="living-water",
            tenants_root=solo["root"],
            ledger_root=solo["ledger_root"],
            now=NOW,
        )
        assert status == 200, body
        bodies.append(body)
    return desk, bodies


def test_with_the_reader_off_a_pile_is_what_it_always_was(solo):  # noqa: F811
    _, [body] = _pile(solo, JPEG)
    assert body == {"ok": True, "say": "Got it."}
    assert _calls(solo) == [] and [f for f in _files(solo) if "undated" in f]


def test_each_photo_in_a_pile_is_read_the_moment_it_lands(solo, monkeypatch):  # noqa: F811
    _reads(solo, monkeypatch, _shoprite())
    _, [body] = _pile(solo, JPEG)
    assert body["read"] == "Shoprite, November 12, 4,300 rupees"
    assert body["ask"]["token"] and [c["pick"] for c in body["ask"]["choices"]] == ["right", "no"]
    assert _calls(solo) == [("receipt_read", "ok")]


def test_her_one_yes_counts_each_receipt_tim_read(solo, monkeypatch):  # noqa: F811
    _reads(solo, monkeypatch, _shoprite(), _shoprite(amount="1200.00", vendor_name="Bhatbhateni"))
    desk, bodies = _pile(solo, JPEG + b"1", JPEG + b"2")
    assert [b["read"] for b in bodies] == [
        "Shoprite, November 12, 4,300 rupees",
        "Bhatbhateni, November 12, 1,200 rupees",
    ]
    for body in bodies:  # the page posts each one's "right" on her one tap
        status, said, _ = _tap(solo, desk, body, "right")
        assert status == 200 and said["say"] == receipt_read.words()["counted"]


def test_one_she_sets_aside_waits_for_her_word(solo, monkeypatch):  # noqa: F811
    _reads(solo, monkeypatch, _shoprite())
    desk, [body] = _pile(solo, JPEG)
    status, said, _ = _tap(solo, desk, body, "no")
    assert status == 200 and said["say"] == receipt_read.words()["not_counted"]


def test_a_photo_the_reader_cannot_read_waits_as_a_pile_always_has(solo, monkeypatch):  # noqa: F811
    _reads(solo, monkeypatch)  # no reply: the model gave nothing back
    _, [body] = _pile(solo, JPEG)
    assert body == {"ok": True, "say": "Got it."} and "read" not in body
    assert [f for f in _files(solo) if "undated" in f]


# ---- the page --------------------------------------------------------------------------


def test_with_the_reader_on_the_pile_page_carries_its_one_summary():
    on = page("n", many=True, reads=True)
    lines = receipt_read.words()
    assert "reads.push" in on and "summary()" in on
    for key in ("pile_read", "pile_right", "pile_counted", "pile_aside", "pile_unread"):
        assert lines[key] in on
    assert "reads.push" not in page("n", many=True)  # off: the page it always was


def test_the_pile_words_are_plain():
    lines = receipt_read.words()
    keys = [k for k in lines if k.startswith("pile_")]
    assert len(keys) >= 12
    for key in keys:
        assert "—" not in lines[key] and "card" not in lines[key].lower()
        if key.endswith("_one"):
            assert "{n}" not in lines[key]


# ---- Tim's page: one photo, or several at once -----------------------------------------


def test_tims_page_has_a_button_for_one_photo_and_one_for_several(talk):  # noqa: F811
    html = _get(talk)[2]
    w = tp.words()
    assert w["photo"] in html and w["photos"] in html.replace("&#x27;", "'")


def test_several_at_once_opens_the_pile_page(talk):  # noqa: F811
    status, headers, _ = _post(talk, tp.PHOTO_PATH, {"many": "1"})
    assert status == 303
    where, _, token = headers["Location"].partition("#")
    assert where == "/receipt?many" and talk["receipts"].is_pile(token)


@pytest.mark.parametrize("form", [{}, {"many": "0"}])
def test_one_photo_still_opens_the_camera_page(talk, form):  # noqa: F811
    status, headers, _ = _post(talk, tp.PHOTO_PATH, form)
    where, _, token = headers["Location"].partition("#")
    assert status == 303 and where == "/receipt" and not talk["receipts"].is_pile(token)
