"""A receipt read the moment it lands (Standalone Tim, card R1).

Hannah snaps a receipt and the page says back "Shoprite, November 12, 4,300
rupees. Right?". Her tap is the yes. What must never happen: a model's
reading becoming a number in the books without her tap, and the receipt
page getting worse for a ministry whose reader is off (every ministry
today). The model is scripted the way the proving ground scripts it: a
``fixture`` tier reading its replies from ``ENGINE_FIXTURE_REPLIES``.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import sqlite3
import tomllib
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from core.agents.ap.extraction import _ATTACHED_ASK, _JSON_SPEC, ExtractionReply, GatewayExtractor
from core.ledger.ledger import DB_FILENAME
from core.llm.adapters.fixture import FixtureAdapter
from core.tools import receipt_read
from core.tools.receipts import Claim, ReceiptDesk, page, take_photo, upload
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_door_receipt_date import _zone
from tests.unit.test_door_receipts import JPEG, box  # noqa: F401  (fixture)
from tests.unit.test_ministry_lines import _door

NOW = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)  # Nov 13 in Kathmandu


def _reads(solo, monkeypatch, *replies: dict, cap: str = "") -> Path:  # noqa: F811
    """Switch the reader on in the ministry's own file (one line in
    [llm.jobs]) and script the model's replies, one a line."""
    toml = Path(solo["root"]) / "living-water" / "tenant.toml"
    text = toml.read_text()
    assert "[llm.jobs]\n" in text
    text = text.replace("[llm.jobs]\n", '[llm.jobs]\nreceipt_read = "fixture"\n', 1)
    if cap:
        text = re.sub(r"(?m)^monthly_usd = \d+$", f"monthly_usd = {cap}", text)
    toml.write_text(text)
    script = Path(solo["root"]).parent / "replies.jsonl"
    script.write_text("".join(json.dumps(r) + "\n" for r in replies))
    monkeypatch.setenv("ENGINE_FIXTURE_REPLIES", str(script))
    return script


def _shoprite(amount: str = "4300.00", currency: str = "NPR", **more) -> dict:
    return {
        "doc_type": "receipt",
        "vendor_name": "Shoprite",
        "amount": amount,
        "invoice_date": "2026-11-12",
        "currency": currency,
        "category": "Meals",
        "confidence": 0.9,
        **more,
    }


def _snap(solo, claim: Claim | None = None, said: str = "here's a receipt", photo=JPEG):  # noqa: F811
    _zone(solo, "Asia/Kathmandu")
    desk = ReceiptDesk()
    token = desk.issue("daniel-reyes", "Daniel Reyes", claim, said)
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
    return desk, body


def _tap(solo, desk, body, pick: str):  # noqa: F811
    headers = {"X-Receipt-Reading": body["ask"]["token"], "Content-Length": "0"}
    raw = f"pick={pick}".encode()
    headers["Content-Length"] = str(len(raw))
    return upload(
        desk,
        headers,
        io.BytesIO(raw).read,
        origin="https://tim.example.org",
        tenant="living-water",
        tenants_root=solo["root"],
        ledger_root=solo["ledger_root"],
    )


def _line(solo) -> dict:  # noqa: F811
    [month] = _door(solo, "daniel-reyes").call("month_report", {"month": "2026-11"})["people"]
    return month["lines"][0]


def _files(solo) -> list[str]:  # noqa: F811
    return sorted({p.name for p in Path(solo["root"]).parent.rglob("*_receipt_*") if p.is_file()})


def _calls(solo) -> list[tuple[str, str]]:  # noqa: F811
    db = Path(solo["ledger_root"]) / DB_FILENAME
    con = sqlite3.connect(db)
    try:
        return con.execute("SELECT job_type, status FROM llm_calls ORDER BY id").fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()


# ---- off: the page is what it always was ----------------------------------------------


def test_with_the_reader_off_the_page_is_byte_for_byte_what_it_was():
    """These are the receipt page's bytes before card R1 (main 53ea79d); the
    catch-up page's since sends each file's name (t-account-from-name) and
    says to switch the iPhone picker to List view (t-picker-list-view)."""
    pinned = {
        (): "e32281b44b7be92350ae5238f6de5fed422eeaf4632d710cce3caa751f8b80dc",
        (("many", True),): "a3f0bf2bf68c88348dcaa777abafd7f3a1d9586c82c9883326aee7db5786ec8c",
        (
            ("ministry", "Grace"),
        ): "887f87d625141b2376fe298102db33d76c484645d20a73274b08b00d0d4e278f",
        (
            ("home", "k" * 32),
            ("ministry", "Grace"),
        ): "d9f1cffcf005553c1d90585be82fa0f9c81d7f54e094f23cab8d9a2505850a6c",
    }
    for args, digest in pinned.items():
        html = page("n", **dict(args))
        assert hashlib.sha256(html.encode()).hexdigest() == digest
        assert html == page("n", **dict(args), reads=False)
    assert "id=ask" in page("n", reads=True)
    # A pile is read too, its photos listed for one yes (the owner's read of
    # 2026-11-19, tests/unit/test_receipt_pile.py).
    assert "summary()" in page("n", many=True, reads=True)


@pytest.mark.parametrize("claim", [None, Claim(Decimal("9"), "USD", "$")])
def test_with_no_line_in_the_file_no_model_is_asked_and_nothing_changes(solo, claim):  # noqa: F811
    _desk, body = _snap(solo, claim)
    want = "Got it. How much was it?" if claim is None else "Got it: $9.00"
    assert body["say"].startswith(want) and "ask" not in body
    assert _calls(solo) == []


@pytest.mark.parametrize("claim", [None, Claim(Decimal("9"), "USD", "$")])
def test_a_reader_with_nothing_to_say_leaves_the_page_as_it_was(solo, monkeypatch, claim):  # noqa: F811
    """A bare fixture tier answers nothing: the call fails, is recorded, and
    the page answers the way it did before the reader existed."""
    _reads(solo, monkeypatch)  # no replies scripted
    _desk, body = _snap(solo, claim)
    want = "Got it. How much was it?" if claim is None else "Got it: $9.00"
    assert body["say"].startswith(want) and "ask" not in body
    assert _files(solo) == [f"2026-11-13_receipt_{'unread' if claim is None else '$9.00'}.jpg"]
    assert _calls(solo) == [("receipt_read", "transport_failed")]


def test_the_month_cap_stops_the_call_before_it_is_made(solo, monkeypatch):  # noqa: F811
    _reads(solo, monkeypatch, _shoprite(), cap="0")
    _desk, body = _snap(solo)
    assert body["say"].startswith("Got it. How much was it?") and "ask" not in body
    assert _calls(solo) == [("receipt_read", "budget_refused")]


@pytest.mark.parametrize(
    "reply",
    [
        _shoprite(currency=None),  # no currency: never a guessed one
        _shoprite(currency="XYZ"),
        _shoprite(amount="0.00"),
        _shoprite(amount=None),
        _shoprite(doc_type="reference"),  # a photo of a machine
    ],
)
def test_a_reading_code_cannot_trust_is_never_shown(solo, monkeypatch, reply):  # noqa: F811
    _reads(solo, monkeypatch, reply)
    _desk, body = _snap(solo)
    assert body["say"].startswith("Got it. How much was it?") and "ask" not in body


# ---- on: read back, and her tap --------------------------------------------------------


@pytest.mark.security("SEC-12")
def test_a_photo_is_read_back_and_counts_only_on_her_tap(solo, monkeypatch):  # noqa: F811
    _reads(solo, monkeypatch, _shoprite())
    desk, body = _snap(solo)
    assert body["say"] == "Got it. It reads Shoprite, November 12, 4,300 rupees. Right?"
    assert [c["label"] for c in body["ask"]["choices"]] == ["Yes, that's right", "No"]
    assert _calls(solo) == [("receipt_read", "ok")]
    line = _line(solo)  # filed on the day it shows, and not counted
    assert line["date"] == "2026-11-12" and line["read"] == "NPR 4,300.00 at Shoprite"
    assert line["said"] is None and line["waiting_on"] == "their word on the amount"
    status, out, _ = _tap(solo, desk, body, "right")
    assert status == 200 and out["say"] == "Thanks. It counts in this month's receipts."
    line = _line(solo)
    assert line["said"] == "NPR 4,300.00" and line["read"] is None


def test_her_no_sets_nothing_and_tim_asks_how_much(solo, monkeypatch):  # noqa: F811
    _reads(solo, monkeypatch, _shoprite())
    desk, body = _snap(solo)
    status, out, _ = _tap(solo, desk, body, "no")
    assert status == 200 and "How much was it?" in out["say"]
    assert _line(solo)["said"] is None


def test_a_question_is_answered_once(solo, monkeypatch):  # noqa: F811
    _reads(solo, monkeypatch, _shoprite())
    desk, body = _snap(solo)
    assert _tap(solo, desk, body, "right")[0] == 200
    status, out, _ = _tap(solo, desk, body, "right")
    assert status == 404 and "already closed" in out["say"]
    assert _tap(solo, desk, {"ask": {"token": "made-up"}}, "right")[0] == 404


def test_a_tap_from_another_site_is_refused(solo, monkeypatch):  # noqa: F811
    _reads(solo, monkeypatch, _shoprite())
    desk, body = _snap(solo)
    headers = {
        "X-Receipt-Reading": body["ask"]["token"],
        "Content-Length": "10",
        "Origin": "https://elsewhere.example",
    }
    status, _out, _ = upload(
        desk,
        headers,
        io.BytesIO(b"pick=right").read,
        origin="https://tim.example.org",
        tenant="living-water",
        tenants_root=solo["root"],
        ledger_root=solo["ledger_root"],
    )
    assert status == 403 and _line(solo)["said"] is None


@pytest.mark.security("SEC-12")
def test_what_she_said_and_what_it_prints_apart_asks_which_and_keeps_neither(solo, monkeypatch):  # noqa: F811
    """Persona beat S09: a receipt's printed amount was never checked
    against the amount said."""
    _reads(solo, monkeypatch, _shoprite(amount="12.40", currency="USD"))
    desk, body = _snap(solo, Claim(Decimal("9"), "USD", "$"), "here's a $9 receipt")
    assert body["say"] == "The receipt shows $12.40, and you said $9. Which is right?"
    assert [c["label"] for c in body["ask"]["choices"]] == ["$12.40", "$9"]
    assert body["ask"]["even"] is True  # neither answer is the easy one
    assert _files(solo) == ["2026-11-13_receipt_unread.jpg"]  # her $9 isn't kept either
    assert _line(solo)["dollars"] is None
    _tap(solo, desk, body, "said")
    assert _line(solo)["dollars"] == "9.00"


def test_she_can_pick_what_the_receipt_shows(solo, monkeypatch):  # noqa: F811
    _reads(solo, monkeypatch, _shoprite(amount="4500.00"))
    claim = Claim(Decimal("4300"), "", "rupees")
    desk, body = _snap(solo, claim, "4,300 rupees at Shoprite")
    assert (
        body["say"] == "The receipt shows 4,500 rupees, and you said 4,300 rupees. Which is right?"
    )
    _tap(solo, desk, body, "shown")
    assert _line(solo)["said"] == "NPR 4,500.00"


def test_the_same_amount_is_simply_in(solo, monkeypatch):  # noqa: F811
    _reads(solo, monkeypatch, _shoprite(amount="9.00", currency="USD"))
    _desk, body = _snap(solo, Claim(Decimal("9"), "USD", "$"), "here's a $9 receipt")
    assert body["say"] == (
        "Got it: Shoprite, November 12, $9, the same as you said. "
        "It's in with this month's receipts."
    )
    assert "ask" not in body and _files(solo) == ["2026-11-13_receipt_$9.00.jpg"]
    assert _line(solo)["date"] == "2026-11-12"


def test_a_heic_photo_is_never_sent_to_the_reader(solo, monkeypatch):  # noqa: F811
    _reads(solo, monkeypatch, _shoprite())
    heic = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 64
    _desk, body = _snap(solo, photo=heic)
    assert "ask" not in body and _calls(solo) == []


# ---- the reader is the expense lane's own --------------------------------------------


def test_the_page_asks_with_the_extractors_own_brief_and_one_line_more(tmp_path):
    seen = FixtureAdapter({"*": json.dumps(_shoprite())})
    photo = tmp_path / "r.png"
    photo.write_bytes(b"\x89PNG\r\n\x1a\n")
    sent = []

    def completer(_ctx, job_type, messages, output_model, **more):
        sent.append((job_type, messages, output_model))
        from core.llm.gateway import complete

        return complete(job_type, messages, output_model, adapter=seen, model="m")

    reply = GatewayExtractor(
        None,
        job_type=receipt_read.JOB,
        completer=completer,
        output_model=receipt_read.PageReading,
        also=receipt_read.CURRENCY,
    ).read(photo)
    assert reply.currency == "NPR"
    job_type, messages, model = sent[0]
    assert job_type == "receipt_read" and model is receipt_read.PageReading
    assert [m.content for m in messages] == [_JSON_SPEC + receipt_read.CURRENCY, _ATTACHED_ASK]


def test_the_nightly_jobs_ask_exactly_what_they_asked(tmp_path):
    """The receipt_extract and invoice_extract jobs keep their own words."""
    photo = tmp_path / "r.png"
    photo.write_bytes(b"\x89PNG\r\n\x1a\n")
    sent = []

    def completer(_ctx, job_type, messages, output_model, **more):
        sent.append((messages, output_model))
        from core.llm.gateway import complete

        return complete(
            job_type, messages, output_model, adapter=FixtureAdapter({"*": "{}"}), model="m"
        )

    doc = GatewayExtractor(None, completer=completer).extract(photo)
    assert doc.doc_type == "unknown"
    messages, model = sent[0]
    assert model is ExtractionReply
    assert [m.content for m in messages] == [_JSON_SPEC, _ATTACHED_ASK]


def test_code_checks_every_field_of_a_reading():
    today = date(2026, 11, 13)
    got = receipt_read.checked(
        receipt_read.PageReading(**_shoprite(vendor_name="  Shop  rite ")), today
    )
    assert got == receipt_read.Reading(
        Decimal("4300.00"), "NPR", date(2026, 11, 12), "Shop rite", "Meals"
    )
    future = receipt_read.PageReading(**_shoprite(invoice_date="2026-12-25"))
    assert receipt_read.checked(future, today).day is None
    assert (
        receipt_read.checked(
            receipt_read.PageReading(**_shoprite(vendor_name="12345")), today
        ).vendor
        == ""
    )
    assert receipt_read.money("USD", Decimal("12.40")) == "$12.40"
    assert receipt_read.money("MZN", Decimal("1200.00")) == "1,200 meticais"
    assert receipt_read.money("CHF", Decimal("40")) == "CHF 40"
    last_year = receipt_read.Reading(Decimal("9"), "USD", date(2025, 12, 30))
    assert last_year.words(today) == "December 30, 2025, $9"


def test_the_page_words_are_plain():
    words = tomllib.loads(receipt_read.WORDS_FILE.read_text(encoding="utf-8"))["words"]
    for key, line in words.items():
        assert "—" not in line and "delve" not in line.lower(), key
        for word in ("model", "tier", "ledger", "token", "reading", "extract", "toml"):
            assert not re.search(rf"\b{word}\b", line, re.IGNORECASE), (key, word)


# ---- over the box's own door ------------------------------------------------------------


def test_the_box_serves_the_question_and_takes_her_tap(box, monkeypatch):  # noqa: F811
    """The page carries the question only once the ministry's file switches
    the reader on, read at each page; her tap is a post to the same page."""
    import urllib.request

    from tests.unit.test_door_receipts import _post

    with urllib.request.urlopen(box["base"] + "/receipt") as resp:
        assert "id=ask" not in resp.read().decode()
    _reads(box, monkeypatch, _shoprite())
    with urllib.request.urlopen(box["base"] + "/receipt") as resp:
        assert "id=ask" in resp.read().decode()
    _zone(box, "Asia/Kathmandu")
    token = box["desk"].issue("daniel-reyes", "Daniel Reyes", None, "here's a receipt")
    status, body = _post(box, token, JPEG)
    assert status == 200 and body["say"].endswith("4,300 rupees. Right?")
    req = urllib.request.Request(
        box["base"] + "/receipt",
        data=b"pick=right",
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Receipt-Reading": body["ask"]["token"],
        },
    )
    with urllib.request.urlopen(req) as resp:
        assert json.loads(resp.read())["say"] == "Thanks. It counts in this month's receipts."
