"""At a church, the missionary's packet reads the pay the treasurer named
(c34, the proving ground's church-pay-agrees, 2026-11-16).

Gap A (#645) made one function answer "what this ministry paid this person
as pay this year" for both the church's 1099 total and the missionary's
packet. It was proved at a solo ministry, where the missionary keeps the
ministry's own statement. At a church the treasurer keeps it, and the
packet read the bank through the missionary's own view, which never holds
the church's account: the church's export said $2,000.00 and her packet
said nothing. The packet now reads the church's lines that paid her as pay,
and only those."""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from core.agents.expenses import statements as st
from core.engine.runner import resolve_ledger_root, run
from core.onboarding import apply, next_question, plan, record
from core.policy.pay import write
from core.tools import catalog
from core.tools.receipts import ReceiptDesk, Receipts, take_photo
from core.tools.viewer import viewer_for
from tests.unit.test_catch_up import NOW, _link

CHURCH_CSV = """Grace Fellowship Checking ...2208
Statement period,08/01/2026 - 08/31/2026

Date,Description,Debit,Credit,Balance
08/02/2026,DEPOSIT SUNDAY OFFERING,,"3,100.00","15,100.00"
08/12/2026,ONLINE TRANSFER TO RUTH HOLLIS,"2,000.00",,"13,100.00"
08/20/2026,OHIO EDISON,212.40,,"12,887.60"
,Ending balance,,,"12,887.60"
"""


@pytest.fixture
def grace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "tenants"
    monkeypatch.setenv("ENGINE_TENANTS_ROOT", str(root))
    monkeypatch.setenv("ENGINE_LEDGER_ROOT", str(tmp_path / "ledger"))
    values = {
        "who": "nonprofit-small",
        "legal_name": "Grace Fellowship",
        "your_name": "Carol Jennings",
        "your_role": "treasurer",
        "people": "Ruth Hollis = missionary\nDon Pruitt = board",
    }
    answers: dict = {}
    while (q := next_question(answers)) is not None:
        answers = record(answers, q["id"], values.get(q["id"], q.get("default", "")))
    apply(plan(answers, "grace"), root=root, run_audit=False)
    return {"root": root, "ledger_root": resolve_ledger_root("grace")}


def _door(grace, person: str) -> catalog.Tools:
    return catalog.Tools(
        "grace",
        ledger_root=grace["ledger_root"],
        viewer=viewer_for("grace", person, tenants_root=grace["root"]),
        receipts=Receipts(
            "grace",
            tenants_root=grace["root"],
            ledger_root=grace["ledger_root"],
            person=person,
            desk=ReceiptDesk(),
            base_url="https://tim.example.org/receipt",
        ),
        today="2026-10-13",
    )


def _pay_set(grace) -> None:
    """The board's resolution for Ruth, adopted by Don (no family tie)."""
    written = write(
        "Grace Fellowship",
        person="ruth-hollis",
        name="Ruth Hollis",
        yearly=Decimal("24000"),
        day=12,
        start="2026-08",
        adopted_by="board",
    )
    folder = Path(grace["root"]) / "grace" / "rules"
    folder.mkdir(exist_ok=True)
    (folder / written.copy).write_text(written.words, encoding="utf-8")
    (folder / written.file).write_text(written.rules, encoding="utf-8")
    run("grace", "policy", "propose", tenants_root=grace["root"])
    con = sqlite3.connect(Path(grace["ledger_root"]) / "ledger.sqlite3")
    for card_id, params in con.execute(
        "SELECT id, params_json FROM approval_queue WHERE action_type = 'policy.plan'"
    ).fetchall():
        if json.loads(params).get("kin") == "yes":
            con.execute(
                "UPDATE approval_queue SET status = 'approved', resolved_at = ? WHERE id = ?",
                ("2026-08-01T15:00:00+00:00", card_id),
            )
    con.commit()
    con.close()


def _carol_names_ruths_pay(grace) -> catalog.Tools:
    carol = _door(grace, "carol-jennings")
    token = _link(carol.call("catch_up_link", {})).split("#", 1)[1]
    take_photo(
        carol.receipts.desk,
        token,
        CHURCH_CSV.encode(),
        tenant="grace",
        tenants_root=grace["root"],
        ledger_root=grace["ledger_root"],
        now=NOW,
    )
    [held] = carol.call("bank_statement", {})["statements"]
    kept = carol.call("bank_statement", {"statement": held["statement"], "account": "ministry"})
    assert kept["status"] == "kept", kept
    line = next(
        b["source"]
        for p in carol.call("month_report", {"month": "2026-08"})["people"]
        for b in p["bank"]
        if b["date"] == "2026-08-12"
    )
    put = carol.call(
        "explain_bank_line", {"line": line, "said": "Ruth's pay", "pay_of": "Ruth Hollis"}
    )
    assert put["status"] == "set", put
    return carol


def test_the_church_export_and_her_packet_say_the_same_pay(grace):
    _pay_set(grace)
    carol = _carol_names_ruths_pay(grace)
    export = carol.call("cpa_export", {"year": "2026"})
    assert export["filings"]["pay_by_person"]["ruth-hollis"]["pay"] == "2000.00"
    got = _door(grace, "ruth-hollis").call("tax_packet", {"year": "2026"})
    [church] = got["payers"]
    assert church["name"] == "Grace Fellowship" and church["form"] == "1099-NEC"
    assert church["came_in_seen"] == "2000.00"
    assert church["paid_as_pay"] == [{"date": "2026-08-12", "dollars": "2000.00"}]
    assert got["schedule_c"]["income"] == "2000.00"


def test_her_packet_carries_only_the_lines_that_paid_her(grace):
    _pay_set(grace)
    _carol_names_ruths_pay(grace)
    got = json.dumps(_door(grace, "ruth-hollis").call("tax_packet", {"year": "2026"}))
    for theirs in ("OHIO EDISON", "212.40", "SUNDAY OFFERING", "3100.00"):
        assert theirs not in got
    assert got.count("2000.00") >= 1


# Engine #651 (the owner 2026-11-16, option 1): Ruth keeps the deposit from her
# own statement. A deposit on her own account that matches a line the church
# paid her as pay (her, the same dollars, on or a few days after) is her pay
# received, never the church paying her back. Code decides; nobody is asked.
RUTH_CSV = (
    "Date,Description,Amount\n"
    "08/13/2026,GRACE FELLOWSHIP PAYROLL,2000.00\n"
    "08/21/2026,GRACE FELLOWSHIP REIMB,45.00\n"
    "08/22/2026,PUBLIX SUPER MARKET,-61.18\n"
)


def _ruth_keeps_her_deposits(grace) -> catalog.Tools:
    ruth = _door(grace, "ruth-hollis")
    token = _link(ruth.call("catch_up_link", {})).split("#", 1)[1]
    take_photo(
        ruth.receipts.desk,
        token,
        RUTH_CSV.encode(),
        tenant="grace",
        tenants_root=grace["root"],
        ledger_root=grace["ledger_root"],
        now=NOW,
    )
    [held] = ruth.call("bank_statement", {})["statements"]
    shown = ruth.call("bank_statement", {"statement": held["statement"], "account": "own"})
    assert shown["status"] != "refused", shown
    put = ruth.call(
        "keep_own_lines",
        {"statement": held["statement"], "rows": [1, 2], "said": "my pay and a payback"},
    )
    assert put["status"] != "refused", put
    return ruth


def _her_bank(ruth: catalog.Tools) -> dict[str, dict]:
    return {
        b["words"]: b
        for p in ruth.call("month_report", {"month": "2026-08"})["people"]
        for b in p["bank"]
    }


def test_her_pay_deposit_reads_as_her_pay_received(grace):
    _pay_set(grace)
    _carol_names_ruths_pay(grace)
    ruth = _ruth_keeps_her_deposits(grace)
    bank = _her_bank(ruth)
    assert bank["GRACE FELLOWSHIP PAYROLL"]["role"] == st.PAY_IN
    # A deposit that matches no pay line stays the church paying her back.
    assert bank["GRACE FELLOWSHIP REIMB"]["role"] == st.PAID_BACK
    # Her books count only that one as paid back; the pay was never owed her.
    books = ruth.call("books", {"period": "2026"})
    assert books["paid_back_to_people"] == "45.00", books


def test_her_packet_shows_the_deposit_as_the_same_money(grace):
    _pay_set(grace)
    _carol_names_ruths_pay(grace)
    got = _ruth_keeps_her_deposits(grace).call("tax_packet", {"year": "2026"})
    [church] = got["payers"]
    assert church["came_in_seen"] == "2000.00"  # never $4,000.00
    assert church["deposits"] == [{"date": "2026-08-13", "dollars": "2000.00"}]
    assert got["schedule_c"]["income"] == "2000.00"
    assert "the same money" in json.dumps(got)


def test_without_a_pay_line_her_deposits_stay_paid_back(grace):
    """T2T's shape: no pay line at all, so nothing on her account changes."""
    ruth = _ruth_keeps_her_deposits(grace)
    roles = {w: b["role"] for w, b in _her_bank(ruth).items()}
    assert roles == {
        "GRACE FELLOWSHIP PAYROLL": st.PAID_BACK,
        "GRACE FELLOWSHIP REIMB": st.PAID_BACK,
    }


def _kept(person: str, account: str, day: str, cents: int, n: int = 0) -> dict:
    return {"person": person, "account": account, "day": day, "words": "X", "cents": cents, "n": n}


PAY = {"pay": _kept("carol", "ministry", "2026-08-12", -200000)}
PAID = {"pay": {"pay_of": "ruth"}}


def test_only_her_own_deposit_of_the_same_dollars_near_the_pay_is_her_pay():
    def got(**deposits: dict) -> set[str]:
        return st.pay_received({**PAY, **deposits}, PAID, set())

    assert got(d=_kept("ruth", "own", "2026-08-13", 200000)) == {"d"}
    assert not got(d=_kept("ruth", "own", "2026-08-20", 200000))  # too long after
    assert not got(d=_kept("ruth", "own", "2026-08-13", 199999))  # other dollars
    assert not got(d=_kept("don", "own", "2026-08-13", 200000))  # someone else's
    assert not got(d=_kept("ruth", "ministry", "2026-08-13", 200000))  # the church's own
    # One pay line answers one deposit, the nearest to it.
    near = got(
        far=_kept("ruth", "own", "2026-08-14", 200000, 1),
        near=_kept("ruth", "own", "2026-08-13", 200000),
    )
    assert near == {"near"}
    # A pay line already matched as a move between accounts is still pay (#653).
    deposit = {"d": _kept("ruth", "own", "2026-08-13", 200000)}
    assert st.pay_received({**PAY, **deposit}, PAID, {"pay"}) == {"d"}
