"""Payers and the missionary's tax packet (board chunk c33, t-payer-packet;
the owner, 2026-11-12: any missionary, paid by any mix of payers, gets one
plain tax packet in January).

A missionary keeps a list of who pays them, each a kind: a support platform
(everything on a 1099), a sending church (pay on a 1099, costs reimbursed),
an agency (a 1099 or a W-2), or a personal gift (recorded, kept off the
packet). Each deposit that was their own money (c31) matches to its payer by
the words the bank prints on it. In January the person says what each
payer's tax form reports, and the packet checks it against what Tim saw
arrive: the difference went to an account Tim doesn't see."""

from __future__ import annotations

import pytest

from core.agents.expenses import payers
from core.agents.expenses import statements as st
from tests.unit.test_bank_balance import _keep_ministry
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_ministry_lines import _door

PAY = "EQUIPNET PPD ID: 1234 Descr:Payroll Sec:PPD Ind Name:Daniel Reyes"
PAY_LATER = "EQUIPNET PPD ID: 1234 Descr:Payroll Sec:PPD Ind Name:Daniel Reyes TRN 2"
GIFT = "ONLINE GIFT HOLLIS FAMILY"
AUNT = "ZELLE FROM AUNT MARIA REYES"
MOBILE = "MOBILE DEPOSIT"
GUEST_HOUSE = "KATHMANDU GUEST HOUSE"

PAY_CSV = f"""Living Water Ministry Checking ...4417
Statement period,08/01/2026 - 08/31/2026

Date,Description,Debit,Credit
08/03/2026,{PAY},,400.00
08/05/2026,{GIFT},,250.00
08/09/2026,{AUNT},,100.00
08/12/2026,{MOBILE},,300.00
08/17/2026,{PAY_LATER},,400.00
08/20/2026,{GUEST_HOUSE},"1,204.50",
"""


def _payer(name: str, kind: str, words: str = "", form: str = "") -> payers.Payer:
    return payers.Payer.of(
        {"person": "daniel-reyes", "name": name, "kind": kind, "words": words, "form": form}
    )


# ---- the kinds and their forms ---------------------------------------------------------


@pytest.mark.parametrize(
    ("said", "kind"),
    [
        ("support platform", payers.PLATFORM),
        ("Platform", payers.PLATFORM),
        ("sending church", payers.CHURCH),
        ("church", payers.CHURCH),
        ("agency", payers.AGENCY),
        ("personal gift", payers.PERSONAL),
        ("gift", payers.PERSONAL),
        ("a bank", ""),
    ],
)
def test_a_payers_kind_is_read_from_plain_words(said, kind):
    assert payers.kind_of(said) == kind


def test_each_kind_sends_its_own_form():
    assert _payer("EquipNet", payers.PLATFORM).form == "1099-NEC"
    assert _payer("Faith Assembly", payers.CHURCH).form == "1099-NEC"
    assert _payer("Wycliffe", payers.AGENCY).form == "1099-NEC"
    assert _payer("Wycliffe", payers.AGENCY, form="W-2").form == "W-2"
    assert _payer("Aunt Maria", payers.PERSONAL).form == ""
    assert _payer("Aunt Maria", payers.PERSONAL, form="W-2").form == ""  # a gift sends none


def test_the_newest_word_on_a_payer_stands():
    said = [
        {"person": "daniel-reyes", "name": "EquipNet", "kind": payers.PLATFORM},
        {"person": "daniel-reyes", "name": "equipnet", "kind": payers.PLATFORM, "words": "EQPNT"},
        {"person": "ruth-okafor", "name": "Faith Assembly", "kind": payers.CHURCH},
    ]
    kept = payers.kept(said)
    [equipnet] = kept["daniel-reyes"]
    assert equipnet.name == "equipnet" and equipnet.words == ("EQPNT",)
    assert [p.name for p in kept["ruth-okafor"]] == ["Faith Assembly"]


# ---- a deposit to its payer ------------------------------------------------------------


def test_a_deposit_matches_the_payer_whose_words_it_carries():
    equipnet = _payer("EquipNet", payers.PLATFORM)  # no words said: its name is its words
    church = _payer("Faith Assembly", payers.CHURCH, words="FAITH ASSY")
    both = [equipnet, church]
    assert payers.match(PAY, both) == equipnet
    assert payers.match("ACH CREDIT FAITH ASSY ORLANDO", both) == church
    assert payers.match(MOBILE, both) is None


def test_two_payers_a_deposit_could_be_leave_it_for_the_person():
    a = _payer("EquipNet", payers.PLATFORM)
    b = _payer("EquipNet Missions", payers.PLATFORM, words="EQUIPNET")
    assert payers.match(PAY, [a, b]) is None


# ---- through the door ------------------------------------------------------------------


def _line(door, words: str) -> dict:
    [me] = door.call("month_report", {"month": "2026-08"})["people"]
    return next(b for b in me["bank"] if b["words"] == words)


def _paid(solo):  # noqa: F811
    return _paid_desk(solo)[0]


def _paid_desk(solo):  # noqa: F811
    door, desk, token, _said = _keep_ministry(solo, PAY_CSV)
    for name, kind, extra in (
        ("EquipNet", "support platform", {}),
        ("Aunt Maria", "personal gift", {"words": "AUNT MARIA"}),
    ):
        put = door.call("keep_payers", {"payer": name, "kind": kind, **extra})
        assert put["status"] == "set", put
    for words in (PAY, AUNT, MOBILE):
        line = _line(door, words)["source"]
        put = door.call("explain_bank_line", {"line": line, "said": "mine", "whose": "own"})
        assert put["status"] == "set", put
    return door, desk, token


def test_the_packet_counts_each_payers_deposits(solo):  # noqa: F811
    door = _paid(solo)
    # The one question about support Tim can't see is answered (persona night Y07).
    door.call("keep_tax_facts", {"year": "2026", "support_elsewhere": "none"})
    got = door.call("tax_packet", {"year": "2026"})
    assert got["person"] == "Daniel Reyes" and got["year"] == "2026"
    [equipnet] = got["payers"]
    assert equipnet["name"] == "EquipNet" and equipnet["kind"] == "support platform"
    assert equipnet["form"] == "1099-NEC"
    assert equipnet["came_in_seen"] == "400.00"
    assert equipnet["deposits"] == [{"date": "2026-08-03", "dollars": "400.00"}]
    assert "form_says" not in equipnet
    # A personal gift is kept, and stays off the packet.
    assert got["personal_gifts"] == [
        {"from": "Aunt Maria", "date": "2026-08-09", "dollars": "100.00"}
    ]
    # Their own money that names no payer: Tim asks who paid it.
    assert got["unmatched"] == [{"date": "2026-08-12", "dollars": "300.00", "words": MOBILE}]
    # A deposit still counted as the ministry's that carries a payer's words.
    assert got["asks"] == [
        {
            "date": "2026-08-17",
            "dollars": "400.00",
            "payer": "EquipNet",
            "ask": "This deposit carries EquipNet's name but counts as the ministry's "
            "money. Was it their pay?",
        }
    ]
    assert got["says"].startswith("2026: EquipNet paid $400.00 that Tim saw arrive.")


def test_the_form_figure_is_checked_against_what_tim_saw(solo):  # noqa: F811
    door = _paid(solo)
    put = door.call("keep_payers", {"payer": "EquipNet", "year": "2026", "form_says": "1,000.00"})
    assert put["status"] == "set", put
    [equipnet] = door.call("tax_packet", {"year": "2026"})["payers"]
    assert equipnet["form_says"] == "1000.00"
    assert equipnet["not_seen"] == "600.00"
    assert "went to an account Tim doesn't see" in door.call("tax_packet", {"year": "2026"})["says"]


def test_a_form_that_says_zero_is_kept_as_zero_and_every_packet_still_reads(solo):  # noqa: F811
    # The persona night of 2026-11-16 (proving ground finding Y02): told a
    # 2025 form said $0, Tim kept the word with no amount, and every packet
    # after it failed on the missing figure ('cents'), for every year.
    door = _paid(solo)
    put = door.call("keep_payers", {"payer": "EquipNet", "year": "2025", "form_says": "0"})
    assert put["status"] == "set", put
    got = door.call("tax_packet", {"year": "2025"})
    assert "error" not in got, got
    assert door.call("tax_packet", {"year": "2026"})["payers"][0]["came_in_seen"] == "400.00"


def test_an_old_form_word_kept_without_its_amount_reads_as_zero():
    # Ledgers written before the fix hold the $0 word with no cents at all.
    said = [{"person": "dr", "payer": "EquipNet", "year": "2025"}]
    assert payers.figures(said) == {("dr", "equipnet", "2025"): 0}


def test_a_form_figure_needs_a_payer_already_on_the_list(solo):  # noqa: F811
    door = _paid(solo)
    put = door.call("keep_payers", {"payer": "Wycliffe", "year": "2026", "form_says": "50.00"})
    assert put["status"] == "needs" and "Wycliffe" in put["say"]


def test_an_agency_that_sends_a_w2_says_so(solo):  # noqa: F811
    door = _paid(solo)
    door.call("keep_payers", {"payer": "Wycliffe", "kind": "agency", "form": "W-2"})
    names = {p["name"]: p["form"] for p in door.call("tax_packet", {"year": "2026"})["payers"]}
    assert names == {"EquipNet": "1099-NEC", "Wycliffe": "W-2"}


def test_a_kind_tim_doesnt_know_is_asked_again(solo):  # noqa: F811
    door, *_rest = _keep_ministry(solo, PAY_CSV)
    put = door.call("keep_payers", {"payer": "First Bank", "kind": "a bank"})
    assert put["status"] == "needs"
    assert "support platform" in put["say"] and "personal gift" in put["say"]


def test_whoever_looks_over_every_month_reads_a_missionarys_packet(solo):  # noqa: F811
    # The owner, 2026-11-15, after decision 2: the president does the work on
    # the missionary's books, so they read the packet without a share.
    _paid(solo)
    linda = _door(solo, "linda-park")
    assert linda.call("tax_packet", {"year": "2026"})["status"] == "needs"  # whose packet?
    got = linda.call("tax_packet", {"year": "2026", "person": "Daniel Reyes"})
    assert [p["name"] for p in got["payers"]] == ["EquipNet"]


def test_the_ministrys_own_income_is_untouched_by_a_payer_list(solo):  # noqa: F811
    door = _paid(solo)
    assert _line(door, GIFT)["role"] == st.CAME_IN
    assert _line(door, PAY)["role"] == st.THEIRS_IN


def test_both_tools_are_on_the_door(solo):  # noqa: F811
    door = _paid(solo)
    assert {"keep_payers", "tax_packet"} <= {s.name for s in door.specs()}


# ---- the packet's tax half (PR 2, packet.py) --------------------------------------------


def test_the_packet_carries_self_employment_tax_on_the_forms_figure(solo):  # noqa: F811
    door = _paid(solo)
    door.call("keep_payers", {"payer": "EquipNet", "year": "2026", "form_says": "1000.00"})
    got = door.call("tax_packet", {"year": "2026"})
    assert got["schedule_c"] == {"income": "1000.00", "costs": "0.00", "net": "1000.00"}
    se = got["self_employment_tax"]
    assert se["on"] == "923.50" and se["tax"] == "141.29" and se["half_off_income"] == "70.65"
    assert "Revenue Procedure" in se["figures_from"]
    ahead = got["federal_tax"]["without_exclusion"]["estimates"]
    assert [e["due"] for e in ahead] == ["2027-04-15", "2027-06-15", "2027-09-15", "2028-01-18"]
    # c39: the whole tax, under the Form 1040-ES rules; $141.29 is under the
    # $1,000 line, so none are needed (before c39 it split the
    # self-employment tax four ways regardless).
    assert [e["dollars"] for e in ahead] == ["0.00"] * 4
    assert "No estimated payments are needed for 2027" in got["says"]
    assert "never lowers it" in got["says"]
    assert got["days_abroad"]["days_with_spending_abroad"] == 0


def test_a_w2_agencys_pay_carries_no_self_employment_tax(solo):  # noqa: F811
    door, *_rest = _keep_ministry(solo, PAY_CSV)
    door.call("keep_payers", {"payer": "EquipNet", "kind": "agency", "form": "W-2"})
    line = _line(door, PAY)["source"]
    door.call("explain_bank_line", {"line": line, "said": "my pay", "whose": "own"})
    got = door.call("tax_packet", {"year": "2026"})
    assert got["schedule_c"]["income"] == "0.00"
    assert got["self_employment_tax"]["tax"] == "0.00"


# ---- engine tax-packet: the page and the PDF ---------------------------------------------


def test_the_command_writes_the_packet_as_a_page_and_a_pdf(solo, tmp_path, monkeypatch, capsys):  # noqa: F811
    import io
    from pathlib import Path

    from pypdf import PdfReader

    from core.engine.cli import main

    _paid(solo)
    monkeypatch.setenv("ENGINE_TENANTS_ROOT", str(solo["root"]))
    out = tmp_path / "packet"
    args = ["tax-packet", "living-water", "--as", "linda-park", "--person", "Daniel Reyes"]
    args += ["--year", "2026", "--out", str(out)]
    args += ["--ledger-dir", str(Path(solo["ledger_root"]).parent)]
    assert main(args) == 0, capsys.readouterr()
    assert sorted(p.name for p in out.iterdir()) == [
        "living-water-2026-tax-packet-daniel-reyes.html",
        "living-water-2026-tax-packet-daniel-reyes.pdf",
    ]
    pdf = (out / "living-water-2026-tax-packet-daniel-reyes.pdf").read_bytes()
    words = " ".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)
    assert "2026 tax packet for Daniel Reyes" in words and "$400.00" in words
    page = (out / "living-water-2026-tax-packet-daniel-reyes.html").read_text()
    assert "<script" not in page and "Schedule C" in page
    assert "EquipNet paid $400.00" in capsys.readouterr().out


def test_the_command_asks_whose_packet(solo, tmp_path, monkeypatch, capsys):  # noqa: F811
    from pathlib import Path

    from core.engine.cli import main

    _paid(solo)
    monkeypatch.setenv("ENGINE_TENANTS_ROOT", str(solo["root"]))
    args = ["tax-packet", "living-water", "--as", "linda-park", "--year", "2026"]
    args += ["--out", str(tmp_path / "x"), "--ledger-dir", str(Path(solo["ledger_root"]).parent)]
    assert main(args) == 2
    assert not (tmp_path / "x").exists()


# ---- a ministry cost paid from their own pay (Schedule C costs) ---------------------------

OWN_CSV = (
    "Date,Description,Amount\n"
    "08/07/2026,TEAM TAXI KATHMANDU,-60.00\n"
    "08/08/2026,PRINTING TRACTS,-40.00\n"
)


def _from_pay(solo):  # noqa: F811
    from tests.unit.test_bank_balance import _held, _send

    door, desk, token = _paid_desk(solo)
    _send(solo, desk, token, OWN_CSV.encode())
    held = _held(door)["2026-08-07"]
    door.call("bank_statement", {"statement": held["statement"], "account": "own"})
    put = door.call(
        "keep_own_lines", {"statement": held["statement"], "rows": [1, 2], "said": "both ministry"}
    )
    assert put["status"] != "refused", put
    return door


def test_a_cost_paid_from_their_pay_is_never_owed_back(solo):  # noqa: F811
    door = _from_pay(solo)
    assert _line(door, "TEAM TAXI KATHMANDU")["role"] == st.OWED  # before they say
    line = _line(door, "TEAM TAXI KATHMANDU")["source"]
    put = door.call(
        "explain_bank_line",
        {"line": line, "said": "the team's taxi, from my own pay", "whose": "pay"},
    )
    assert put["status"] == "set", put
    assert "from their own pay" in put["say"] and "tax packet" in put["say"]
    assert _line(door, "TEAM TAXI KATHMANDU")["role"] == st.FROM_PAY
    assert _line(door, "PRINTING TRACTS")["role"] == st.OWED  # the one they didn't mark


def test_the_packet_takes_costs_paid_from_their_pay_off_the_1099_income(solo):  # noqa: F811
    door = _from_pay(solo)
    line = _line(door, "TEAM TAXI KATHMANDU")["source"]
    door.call("explain_bank_line", {"line": line, "said": "from my pay", "whose": "pay"})
    door.call("keep_payers", {"payer": "EquipNet", "year": "2026", "form_says": "1000.00"})
    got = door.call("tax_packet", {"year": "2026"})
    assert got["schedule_c"] == {"income": "1000.00", "costs": "60.00", "net": "940.00"}
    assert got["self_employment_tax"]["on"] == "868.09"  # 92.35% of the net, not the income
    assert got["self_employment_tax"]["tax"] == "132.81"


def test_only_their_own_accounts_spending_can_be_paid_from_their_pay(solo):  # noqa: F811
    door = _from_pay(solo)
    line = _line(door, GUEST_HOUSE)["source"]  # on the ministry's account
    put = door.call("explain_bank_line", {"line": line, "said": "my pay", "whose": "pay"})
    assert put["status"] == "asks"
    assert _line(door, GUEST_HOUSE)["role"] != st.FROM_PAY
