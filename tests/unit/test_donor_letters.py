"""Donors and their letters (board chunk c32, t-donor-letters; the owner,
2026-11-12: every donor gets the letter and year-end statement the IRS
expects, without anyone writing them by hand).

A gift is a deposit on the ministry's own account that came in as its
income, with the donor named: said when the line is explained
(``gift_from``), or read from a kind the ministry already named for a
donor ("Gifts from Ruth Okafor"), so books kept before this chunk still
make their letters. Each gift of $250 or more gets its own written
acknowledgment; each donor gets one year-end giving statement. A grant from
a donor-advised fund gets a thank-you only: the fund's sponsor gave the
donor their receipt. The letter's facts that aren't in the books (the
ministry's EIN and address, who signs) are said once and kept."""

from __future__ import annotations

import io
from pathlib import Path

from core.agents.expenses import donors
from core.agents.expenses import statements as st
from tests.unit.test_bank_balance import _keep_ministry
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_ministry_lines import _door

HOLLIS = "ONLINE GIFT HOLLIS FAMILY"
MOBILE = "MOBILE DEPOSIT"
GRANT = "NATIONAL CHRISTIAN FDN GRANT"
LATER = "ONLINE GIFT HOLLIS FAM"
GUEST_HOUSE = "KATHMANDU GUEST HOUSE"
MINISTRY = "Living Water Mozambique, Inc."

GIFTS_CSV = f"""Living Water Ministry Checking ...4417
Statement period,08/01/2026 - 08/31/2026

Date,Description,Debit,Credit,Balance
08/03/2026,{HOLLIS},,250.00,"5,250.00"
08/04/2026,{MOBILE},,249.99,"5,499.99"
08/10/2026,{GRANT},,"1,000.00","6,499.99"
08/12/2026,{LATER},,100.00,"6,599.99"
08/20/2026,{GUEST_HOUSE},"1,204.50",,"5,395.49"
,Ending balance,,,"5,395.49"
"""

HEAD = donors.Letterhead(
    ministry=MINISTRY,
    ein="12-3456789",
    address="100 Main Street, Springfield, Illinois 62701",
    signer="Linda Park",
    title="Treasurer",
)


def _row(day: str, dollars: str, **said) -> dict:
    return {"date": day, "dollars": dollars, "role": st.CAME_IN, "category": None, **said}


def _text(blocks: list[tuple]) -> str:
    out = []
    for b in blocks:
        if b[0] == "table":
            out += [f"{label} {dollars}" for label, dollars, _total in b[2]]
        elif b[0] != "gap":
            out.append(b[1])
    return "\n".join(out)


# ---- who gave ---------------------------------------------------------------------------


def test_the_donor_is_said_or_read_from_a_kind_named_for_them():
    assert donors.donor_of(_row("2026-01-06", "500.00", gift_from="Hollis Family")) == (
        "Hollis Family"
    )
    assert donors.donor_of(_row("2026-01-06", "500.00", category="Gifts from Ruth Okafor")) == (
        "Ruth Okafor"
    )
    assert donors.donor_of(_row("2026-01-06", "500.00", category="Gift from Ann Lee")) == "Ann Lee"
    assert donors.donor_of(_row("2026-01-06", "500.00", category="Gifts")) == ""
    # Their own money in is never a gift to the ministry (c31).
    assert (
        donors.gifts_of([{**_row("2026-01-06", "500.00", gift_from="X"), "role": st.THEIRS_IN}])
        == []
    )


def test_gifts_group_by_donor_whatever_the_case_of_their_name():
    rows = [
        _row("2026-08-03", "250.00", gift_from="Hollis Family"),
        _row("2026-08-12", "100.00", gift_from="hollis family"),
        _row("2026-08-10", "1000.00", gift_from="A Giving Fund", fund_grant=True),
    ]
    people, funds = donors.by_donor(donors.gifts_of(rows))
    [hollis] = people
    assert hollis.name == "Hollis Family" and hollis.total == 35000
    assert [g.day for g in hollis.acknowledged] == ["2026-08-03"]  # $250 or more, each gift
    [grant] = funds
    assert grant.donor == "A Giving Fund" and grant.cents == 100000


def test_a_years_statement_regenerates_six_monthly_gifts_from_a_kind_named_for_the_donor():
    """The shape of the first real letter, built by hand 11/12: six $500
    gifts booked under a kind named for the donor."""
    days = ["2026-01-06", "2026-02-06", "2026-03-06", "2026-04-06", "2026-05-06", "2026-06-08"]
    rows = [_row(d, "500.00", category="Gifts from Ruth Okafor") for d in days]
    [ruth], _funds = donors.by_donor(donors.gifts_of(rows))
    text = _text(
        donors.statement(ruth, HEAD, "2026", "2026-11-12", "14 Elm Street, Orlando, Florida 32828")
    )
    for line in (
        "November 12, 2026",
        "Ruth Okafor",
        "14 Elm Street",
        "Orlando, Florida 32828",
        "Dear Ruth,",
        "January 6, 2026 $500.00",
        "June 8, 2026 $500.00",
        "Total $3,000.00",
        "No goods or services were provided in exchange for these contributions.",
        f"{MINISTRY} has exclusive legal control over the use of these contributions.",
        "section 501(c)(3) of the Internal Revenue Code, EIN 12-3456789.",
        "Linda Park",
        f"Treasurer, {MINISTRY}",
    ):
        assert line in text, line
    assert text.count("$500.00") == 6


def test_one_gift_is_acknowledged_in_the_singular():
    gift = donors.Gift("2026-08-03", 25000, "Hollis Family", False)
    text = _text(donors.acknowledgment(gift, HEAD, "2026-08-04", ""))
    assert "No goods or services were provided in exchange for this contribution." in text
    assert "August 3, 2026 $250.00" in text and "Dear Hollis Family," in text


def test_a_fund_grant_gets_a_thank_you_and_never_a_tax_receipt():
    gift = donors.Gift("2026-08-10", 100000, "A Giving Fund", True)
    text = _text(donors.thank_you(gift, HEAD, "2026-08-11", ""))
    assert "$1,000.00" in text and "August 10, 2026" in text
    assert "goods or services" not in text and "501(c)(3)" not in text


def test_the_letterhead_says_what_it_still_needs():
    bare = donors.Letterhead(ministry=MINISTRY)
    assert bare.missing() == [
        "the ministry's EIN",
        "the ministry's mailing address",
        "who signs the letters, and their title",
    ]
    assert HEAD.missing() == []


def test_the_newest_word_on_each_detail_stands():
    events = [
        {"ein": "11-1111111", "signer": "Linda Park", "title": "Treasurer"},
        {"ein": "12-3456789"},
        {"donor": "Hollis Family", "address": "1 Oak Lane, Peoria, Illinois 61602"},
    ]
    head = donors.letterhead(events, MINISTRY)
    assert head.ein == "12-3456789" and head.signer == "Linda Park"
    assert donors.address_of(events, "hollis family") == "1 Oak Lane, Peoria, Illinois 61602"


# ---- through the door ------------------------------------------------------------------


def _bank(door) -> list[dict]:
    [me] = door.call("month_report", {"month": "2026-08"})["people"]
    return me["bank"]


def _line(door, words: str) -> dict:
    return next(b for b in _bank(door) if b["words"] == words)


def _given(solo):  # noqa: F811
    door, *_rest = _keep_ministry(solo, GIFTS_CSV)
    for words, said, args in (
        (HOLLIS, "the Hollis family's gift", {"gift_from": "Hollis Family"}),
        (MOBILE, "a check from Grace Okoro", {"gift_from": "Grace Okoro"}),
        (GRANT, "a grant", {"gift_from": "National Christian Foundation", "fund_grant": True}),
        (LATER, "the Hollises again", {"gift_from": "hollis family"}),
    ):
        put = door.call(
            "explain_bank_line", {"line": _line(door, words)["source"], "said": said, **args}
        )
        assert put["status"] == "set", put
    return door


def test_a_deposit_explained_as_a_gift_names_its_donor(solo):  # noqa: F811
    door = _given(solo)
    row = _line(door, HOLLIS)
    assert row["role"] == st.CAME_IN and row["gift_from"] == "Hollis Family"
    assert _line(door, GRANT)["fund_grant"] is True
    assert "fund_grant" not in row


def test_only_money_in_can_be_a_gift(solo):  # noqa: F811
    door = _given(solo)
    put = door.call(
        "explain_bank_line",
        {"line": _line(door, GUEST_HOUSE)["source"], "said": "rooms", "gift_from": "Hollis Family"},
    )
    assert put["status"] == "asks"


def test_donor_letters_list_each_donor_and_what_they_are_owed(solo):  # noqa: F811
    _given(solo)
    linda = _door(solo, "linda-park")
    got = linda.call("donor_letters", {"year": "2026"})
    assert got["needs"] == [
        "the ministry's EIN",
        "the ministry's mailing address",
        "who signs the letters, and their title",
    ]
    grace, hollis = got["donors"]
    assert grace["name"] == "Grace Okoro" and grace["total"] == "249.99"
    assert grace["letters"] == ["a year-end giving statement"]
    assert hollis["total"] == "350.00" and len(hollis["gifts"]) == 2
    assert hollis["letters"] == [
        "an acknowledgment of the Aug 3, 2026 gift of $250.00",
        "a year-end giving statement",
    ]
    [grant] = got["fund_grants"]
    assert grant["from"] == "National Christian Foundation" and grant["dollars"] == "1000.00"
    assert got["unnamed"] == 0


def test_the_letters_details_are_said_once_and_kept(solo):  # noqa: F811
    _given(solo)
    linda = _door(solo, "linda-park")
    put = linda.call(
        "letter_details",
        {
            "ein": "12-3456789",
            "address": "100 Main Street, Springfield, Illinois 62701",
            "signer": "Linda Park",
            "title": "Treasurer",
        },
    )
    assert put["status"] == "set", put
    linda.call("letter_details", {"donor": "Hollis Family", "donor_address": "1 Oak Lane"})
    got = linda.call("donor_letters", {"year": "2026"})
    assert got["needs"] == []
    assert got["donors"][1]["address"] == "1 Oak Lane"


def test_the_ein_is_asked_again_when_it_isnt_one(solo):  # noqa: F811
    linda = _door(solo, "linda-park")
    assert linda.call("letter_details", {"ein": "123"})["status"] == "needs"


def test_a_solo_missionary_looks_over_every_month_so_makes_their_own_letters(solo):  # noqa: F811
    door = _given(solo)
    assert {"donor_letters", "letter_details"} <= {s.name for s in door.specs()}


def test_the_command_writes_every_letter_as_a_page_and_a_pdf(
    solo,  # noqa: F811
    tmp_path,
    monkeypatch,
    capsys,
):
    from pypdf import PdfReader

    from core.engine.cli import main

    _given(solo)
    linda = _door(solo, "linda-park")
    linda.call(
        "letter_details",
        {
            "ein": "12-3456789",
            "address": "100 Main Street",
            "signer": "Linda Park",
            "title": "Treasurer",
        },
    )
    monkeypatch.setenv("ENGINE_TENANTS_ROOT", str(solo["root"]))
    out = tmp_path / "letters"
    args = ["donor-letters", "living-water", "--as", "linda-park", "--year", "2026"]
    args += ["--out", str(out), "--ledger-dir", str(Path(solo["ledger_root"]).parent)]
    assert main(args) == 0, capsys.readouterr()
    names = sorted(p.name for p in out.iterdir())
    assert names == sorted(
        f"living-water-2026-{stem}.{ext}"
        for stem in (
            "statement-grace-okoro",
            "statement-hollis-family",
            "acknowledgment-2026-08-03-hollis-family",
            "thank-you-2026-08-10-national-christian-foundation",
        )
        for ext in ("html", "pdf")
    )
    pdf = (out / "living-water-2026-statement-hollis-family.pdf").read_bytes()
    words = " ".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)
    assert "$350.00" in words and "No goods or services were provided" in words
    page = (out / "living-water-2026-statement-hollis-family.html").read_text()
    assert "<script" not in page and "Living Water Mozambique, Inc." in page


def test_the_command_refuses_until_the_letters_details_are_said(
    solo,  # noqa: F811
    tmp_path,
    monkeypatch,
    capsys,
):
    from core.engine.cli import main

    _given(solo)
    monkeypatch.setenv("ENGINE_TENANTS_ROOT", str(solo["root"]))
    args = ["donor-letters", "living-water", "--as", "linda-park", "--year", "2026"]
    args += ["--out", str(tmp_path / "x"), "--ledger-dir", str(Path(solo["ledger_root"]).parent)]
    assert main(args) == 2
    assert "EIN" in capsys.readouterr().err
