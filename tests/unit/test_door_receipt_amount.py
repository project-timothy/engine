"""A receipt whose amount comes after the photo (board item t-receipt-photo,
2026-10-13): "here's a receipt" with no amount still gets the link. When
the ministry has a photo reader, the photo's amount is a proposal and
nothing more; the person's own words decide it, either by saying the amount
("it was 4,300 rupees") or by a yes to what the photo reads. With no
reader, Tim asks how much. Until then the receipt waits, unpriced, and is
never counted at a guessed number."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from core.adapters.pretend_reader import MARK, PretendReader
from core.contracts.reader import Reading, ReceiptReader
from core.tools.receipts import Claim, ReceiptDesk, Receipts, take_photo
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_door_receipt_date import _zone
from tests.unit.test_door_receipts import JPEG
from tests.unit.test_ministry_lines import _door, _month
from tests.unit.test_missionary_month import NPR_OCT_12, feed  # noqa: F401  (fixture)


def _reader(solo) -> None:  # noqa: F811
    toml = solo["root"] / "living-water" / "tenant.toml"
    text = toml.read_text()
    assert "\n[expenses]\n" in text
    toml.write_text(text.replace("\n[expenses]\n", '\n[expenses]\nreader = "pretend"\n', 1))


def _unpriced_snap(solo, said: str = "here's a receipt", photo: bytes = JPEG) -> str:  # noqa: F811
    _zone(solo, "Asia/Kathmandu")
    desk = ReceiptDesk()
    token = desk.issue("daniel-reyes", "Daniel Reyes", None, said)
    status, body = take_photo(
        desk,
        token,
        photo,
        tenant="living-water",
        tenants_root=solo["root"],
        ledger_root=solo["ledger_root"],
        now=datetime(2026, 10, 12, 9, 0, tzinfo=UTC),
    )
    assert status == 200, body
    return body["say"]


def _line(solo) -> dict:  # noqa: F811
    [month] = _door(solo, "daniel-reyes").call("month_report", {"month": "2026-10"})["people"]
    return month["lines"][0]


def _set(solo, said: str) -> dict:  # noqa: F811
    daniel = _door(solo, "daniel-reyes")
    return daniel.call("set_amount", {"line": _line(solo)["source"], "said": said})


def test_no_amount_in_the_words_still_gets_the_link(solo):  # noqa: F811
    receipts = Receipts(
        "living-water",
        tenants_root=solo["root"],
        ledger_root=solo["ledger_root"],
        person="daniel-reyes",
        desk=ReceiptDesk(),
        base_url="https://tim.example.org/receipt",
    )
    out = receipts.add("here's a receipt from the clinic")
    assert out["status"] == "tap_to_add" and "](https://tim.example.org/receipt#" in out["show"]
    assert "how much" in out["say"]


@pytest.mark.security("SEC-12")
def test_with_no_reader_tim_asks_how_much_and_nothing_is_counted(solo):  # noqa: F811
    said = _unpriced_snap(solo)
    assert said.startswith("Got it.") and "How much was it?" in said
    line = _line(solo)
    assert line["dollars"] is None and line["waiting_on"] == "how much it was"


def test_their_words_set_the_amount_afterward(solo, feed):  # noqa: F811
    feed("2026-10-12", npr=NPR_OCT_12)
    _unpriced_snap(solo)
    out = _set(solo, "it was 4,300 rupees")
    assert out["status"] == "set" and "NPR 4,300.00" in out["say"]
    _month(solo)  # the month records the day's rate
    line = _line(solo)
    assert line["said"] == "NPR 4,300.00" and line["dollars"] == "28.06"
    assert line["waiting_on"] is None


@pytest.mark.security("SEC-12")
def test_the_photo_reading_is_only_a_proposal_until_their_yes(solo):  # noqa: F811
    _reader(solo)
    said = _unpriced_snap(solo, photo=JPEG + MARK + b"43.00 USD")
    assert "The photo reads $43.00" in said and "tell Tim" in said
    line = _line(solo)
    assert line["dollars"] is None and line["read"] == "$43.00"
    assert line["waiting_on"] == "their word on the amount"
    out = _set(solo, "yes, that's right")
    assert out["status"] == "set"
    line = _line(solo)
    assert line["dollars"] == "43.00" and line["read"] is None


@pytest.mark.security("SEC-12")
def test_their_own_amount_wins_over_the_reading(solo):  # noqa: F811
    _reader(solo)
    _unpriced_snap(solo, photo=JPEG + MARK + b"43.00 USD")
    _set(solo, "no, it was $34")
    assert _line(solo)["dollars"] == "34.00"


def test_a_yes_with_nothing_read_asks_how_much(solo):  # noqa: F811
    _unpriced_snap(solo)
    out = _set(solo, "yes")
    assert out["status"] == "needs_amount" and "how much" in out["say"]
    assert _line(solo)["dollars"] is None


def test_the_month_counts_it_once_their_word_is_in(solo):  # noqa: F811
    _unpriced_snap(solo)
    _set(solo, "$43")
    done = _month(solo)
    assert done.status in ("ok", "needs_approval")
    [month] = _door(solo, "daniel-reyes").call("month_report", {"month": "2026-10"})["people"]
    assert month["total"] == "43.00"


def test_a_photo_the_reader_cannot_read_is_asked_about(solo):  # noqa: F811
    _reader(solo)
    said = _unpriced_snap(solo)
    assert "How much was it?" in said


def test_the_pretend_reader_fits_the_seam():
    reader = PretendReader()
    assert isinstance(reader, ReceiptReader)
    assert reader.read(JPEG + MARK + b"4300.00 NPR") == Reading("4300.00", "NPR")
    assert reader.read(JPEG) is None


def test_a_receipt_said_with_its_amount_is_unchanged(solo):  # noqa: F811
    from tests.unit.test_ministry_lines import _snap

    said = _snap(solo, "$9 for lunch", Claim(Decimal("9"), "USD", "$"))
    assert said.startswith("Got it: $9.00")
    assert _line(solo)["dollars"] == "9.00"


def test_a_currency_their_zone_cannot_settle_is_asked_before_the_link(solo):  # noqa: F811
    """The second walk (2026-10-13): "rupees" said from Eastern time names no
    one currency, and that receipt held back the whole month. Tim asks which
    one before the photo, never after."""
    _zone(solo, "America/New_York")
    receipts = Receipts(
        "living-water",
        tenants_root=solo["root"],
        ledger_root=solo["ledger_root"],
        person="daniel-reyes",
        desk=ReceiptDesk(),
        base_url="https://tim.example.org/receipt",
    )
    out = receipts.add("4,300 rupees for the bus")
    assert out["status"] == "needs_currency" and "show" not in out
    assert "which" in out["say"] and "rupees" in out["say"]
    named = receipts.add("4,300 Nepali rupees for the bus")
    assert named["status"] == "tap_to_add" and "NPR 4,300.00" in named["say"]
    _zone(solo, "Asia/Kathmandu")
    assert receipts.add("4,300 rupees for the bus")["status"] == "tap_to_add"


def test_the_country_names_the_currency():
    from core.tools.receipts import claim_in

    assert claim_in("4,300 Nepali rupees") == Claim(Decimal("4300.00"), "NPR", "Nepali rupees")
    assert claim_in("1,200 Kenyan shillings for airtime").currency == "KES"
    assert claim_in("500 Philippine pesos").currency == "PHP"
    assert claim_in("4,300 rupees").currency == ""  # still settled by where they are


# ---- a number with no money named (persona finding S13, 2026-11-15) -----------------------
# "12.49 total" for an online-store receipt filed the photo with no amount, and his AI
# asked "how much?"; he said "i already told u." Tim never guesses which money,
# so the question is only which money, keeping the number he said.


@pytest.mark.parametrize(
    ("said", "bare"),
    [
        ("12.49 total for two Kindle books", "12.49"),
        ("it was 4,300", "4,300"),
        ("950 for the taxi", "950"),
        ("receipt from Oct 3", ""),  # a day isn't an amount
        ("the 12/14 bus ticket", ""),
        ("$12.49 for the books", ""),  # money named: no question
        ("12.49 and 3.50", ""),  # two numbers: ask how much
        ("here's a receipt", ""),
    ],
)
def test_a_number_with_no_money_named(said, bare):
    from core.tools.receipts import _bare_number

    assert _bare_number(said) == bare


def test_a_bare_number_is_asked_which_money_never_how_much(solo):  # noqa: F811
    receipts = Receipts(
        "living-water",
        tenants_root=solo["root"],
        ledger_root=solo["ledger_root"],
        person="daniel-reyes",
        desk=ReceiptDesk(),
        base_url="https://tim.example.org/receipt",
    )
    out = receipts.add("12.49 total for two Kindle books")
    assert out["status"] == "tap_to_add"
    assert "whether 12.49 was dollars or rupees" in out["say"]
    assert "never how much again" in out["say"]


def test_set_amount_with_a_bare_number_asks_which_money(solo):  # noqa: F811
    _unpriced_snap(solo)
    out = _set(solo, "12.49 total")
    assert out["status"] == "needs_currency" and "dollars or rupees" in out["say"]
    assert "12.49" in out["say"] and _line(solo)["dollars"] is None


def test_a_receipt_tim_already_keeps_is_named_never_asked_for_again(solo):  # noqa: F811
    """Persona night S08 (2026-11-16): "march 7 bhat-bhateni receipt 4380
    rupees, 430 of that was cooking oil for my kitchen" was a receipt Tim
    already kept, yet Tim sent a camera link for it, the person went to find
    the paper, and the 430 rupees never came out. The same amount in the same
    money on a day their words name is that receipt: Tim names it, and its
    source to split. With no day named it is a new one (S01: taxi fares recur)."""
    from tests.unit.test_ministry_lines import _snap

    _snap(solo, "4,380 rupees for groceries", Claim(Decimal("4380"), "", "rupees"))
    source = _line(solo)["source"]
    daniel = _door(solo, "daniel-reyes")
    said = "oct 12 bhat-bhateni receipt 4380 rupees 430 of that was cooking oil for my kitchen"
    out = daniel.call("add_receipt", {"said": said})
    assert out["status"] == "already_kept" and "show" not in out
    assert "NPR 4,380.00" in out["say"] and "2026-10-12" in out["say"]
    assert source in out["say"] and "split_line" in out["say"]
    again = daniel.call("add_receipt", {"said": said, "another": True})
    assert again["status"] == "tap_to_add"
    assert daniel.call("add_receipt", {"said": "500 rupees for tea"})["status"] == "tap_to_add"
    for fresh in ("the bhat-bhateni receipt from yesterday, 4380 rupees", "oct 13, 4380 rupees"):
        assert daniel.call("add_receipt", {"said": fresh})["status"] == "tap_to_add", fresh


@pytest.mark.parametrize(
    ("said", "days"),
    [
        ("march 7 bhat-bhateni receipt 4380 rupees", {"03-07"}),
        ("the 7th of March, 4,380 rupees", {"03-07"}),
        ("receipt dated 2026-03-07", {"03-07"}),
        ("taxi receipt from yesterday, 950 rupees", set()),
    ],
)
def test_the_days_their_words_name(said, days):
    from core.tools.receipts import days_named

    assert days_named(said) == days
