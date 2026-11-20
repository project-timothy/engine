"""A ministry expense someone paid from their own pocket (board chunk c28;
the owner, 2026-11-12: a board member paid the state's filing fee on their
own card, and Tim had no way to book it).

Anyone at the ministry says what they paid, the day and the dollars. It
raises one card, decided as the ministry's own safeguards say (the owner,
2026-11-12: a tiny ministry's director approves their own; a church or an
organization never lets the one who paid).
Approved, the books count it as the ministry's spending, owed back to them;
the bank line that pays them back is explained as paying them back, and
what they're owed comes down by it. The double-entry books balance
throughout."""

from __future__ import annotations

import json
import sqlite3
import tomllib
from decimal import Decimal
from pathlib import Path

from core.agents.expenses import advances
from core.agents.expenses.jobs import CARD_AUTHORITY
from core.authority import card_request, evaluate, parse_policy
from tests.unit.test_bank_balance import _keep_ministry
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_ministry_lines import _door
from tests.unit.test_statements import MINISTRY_CSV
from tests.unit.test_year_close import _db, _yes

TEMPLATES = Path(__file__).resolve().parents[2] / "tenants" / "_templates" / "authority"
RULE = CARD_AUTHORITY[advances.ADVANCE_CARD]


def _cards(solo, status: str = "") -> list[dict]:  # noqa: F811
    con = sqlite3.connect(_db(solo))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, status, params_json FROM approval_queue WHERE action_type = ? ORDER BY id",
        (advances.ADVANCE_CARD,),
    ).fetchall()
    con.close()
    out = [{"id": r["id"], "status": r["status"], **json.loads(r["params_json"])} for r in rows]
    return [c for c in out if not status or c["status"] == status]


def _linda_paid(solo, day: str = "2026-08-10"):  # noqa: F811
    linda = _door(solo, "linda-park")
    said = linda.call(
        "paid_it_myself",
        {"day": day, "amount": "86.40", "said": "the state's annual filing fee"},
    )
    return linda, said


# ---- who may say yes ------------------------------------------------------------


def _policy(shape: str, people: dict):
    data = tomllib.loads((TEMPLATES / f"nonprofit-{shape}.toml.tmpl").read_text())
    data["people"] = people
    return parse_policy(data)


def _decides(policy, who: str, paid: str) -> bool:
    params = {"person": paid, "cents": "31250"}
    return evaluate(policy, card_request(RULE, who, params, policy)).allowed


def test_a_solo_ministrys_director_approves_what_they_paid_themselves():
    policy = _policy(
        "solo",
        {
            "caleb": {"roles": ["missionary"], "name": "Caleb Meer"},
            "dad": {"roles": ["reviewer"], "name": "Paul Weston"},
        },
    )
    assert policy.safeguards.self_approval == "allowed"
    assert _decides(policy, "caleb", "caleb")  # their own yes, where the ministry allows it


def test_a_church_the_one_who_paid_never_decides_and_an_approver_does():
    policy = _policy(
        "small",
        {
            "carol": {"roles": ["treasurer"], "name": "Carol Jennings"},
            "mark": {"roles": ["pastor"], "name": "Mark Ellison"},
        },
    )
    assert not _decides(policy, "mark", "mark")
    assert _decides(policy, "mark", "carol")


# ---- the door -----------------------------------------------------------------------


def test_it_raises_one_card_and_nothing_counts_until_the_yes(solo):  # noqa: F811
    linda, said = _linda_paid(solo)
    assert said["status"] == "waiting", said
    assert "someone else" in said["say"]  # a reviewer looks over, never decides
    [card] = _cards(solo)
    assert (card["person"], card["name"], card["cents"]) == ("linda-park", "Linda Park", "8640")
    again = linda.call(
        "paid_it_myself",
        {"day": "2026-08-10", "amount": "86.40", "said": "the state's annual filing fee"},
    )
    assert again["card"] == card["id"] and len(_cards(solo)) == 1  # the same words, one card
    # Other words for the same cost (the voice gate, 2026-11-20, A08): still one card.
    worded = linda.call(
        "paid_it_myself",
        {"day": "2026-08-10", "amount": "86.40", "said": "filing fee for the state, paid myself"},
    )
    assert worded["status"] == "already" and worded["card"] == card["id"], worded
    assert len(_cards(solo)) == 1
    # A second cost of the same day and dollars goes in on her word that it is one.
    second = linda.call(
        "paid_it_myself",
        {"day": "2026-08-10", "amount": "86.40", "said": "a second filing fee", "again": True},
    )
    assert second["status"] == "waiting" and len(_cards(solo)) == 2
    books = linda.call("books", {"period": "August 2026"})
    assert books["owed_back_to_people"] == "0.00"  # waiting is not owed


def test_the_card_reads_in_their_name_never_their_id():
    """The A01 spike (2026-11-16): Eli's yes page read "...on my own card for
    eli-porter, $312.50", the id Tim keeps for him reaching him. The card's
    own words already name who paid; the id is never said back."""
    from core.tools.decide import card_words, done_words, plain_words

    what = "Eli Porter paid $312.50 on 2026-11-10 for the ministry: the state renewal fee"
    row = {
        "agent": "expenses",
        "action_type": advances.ADVANCE_CARD,
        "params": {"person": "eli-porter", "name": "Eli Porter", "cents": "31250", "what": what},
    }
    for words in (card_words(row), done_words(row, "approved"), plain_words(row, "approve")):
        assert "eli-porter" not in words, words
        assert words.count("Eli Porter") == 1, words
    no_what = {**row, "params": {"person": "eli-porter", "cents": "31250"}}
    assert "for Eli Porter" in card_words(no_what)  # an id alone is said as a name


def test_approved_it_is_spending_owed_back_until_the_bank_pays_them(solo):  # noqa: F811
    daniel, *_rest = _keep_ministry(solo, MINISTRY_CSV)
    _linda, _said = _linda_paid(solo)
    [card] = _cards(solo)
    _yes(solo, card["id"], at="2026-08-11T15:00:00+00:00")
    again = _linda.call(
        "paid_it_myself", {"day": "2026-08-10", "amount": "86.40", "said": "my filing fee"}
    )
    assert again["status"] == "already" and "approved" in again["say"]  # A08, 2026-11-20
    august = daniel.call("books", {"period": "August 2026"})
    assert august["owed_back_to_people"] == "86.40"
    de = august["double_entry"]
    assert de["balanced"] and de["ties_to_the_bank"]
    assert de["accounts_at_end"]["Liabilities:People:Linda Park"] == "86.40"
    # The bank's Aug 12 line paid her back.
    line = next(
        b["source"]
        for p in daniel.call("month_report", {"month": "2026-08"})["people"]
        for b in p["bank"]
        if b["date"] == "2026-08-12"
    )
    done = daniel.call(
        "explain_bank_line",
        {"line": line, "said": "paid Linda back for the filing fee", "pays_back": "Linda Park"},
    )
    assert done["status"] == "set", done
    assert "paid Linda Park back $86.40" in done["say"]
    after = daniel.call("books", {"period": "August 2026"})
    assert after["paid_back_to_people"] == "86.40"
    de = after["double_entry"]
    assert de["balanced"] and de["ties_to_the_bank"]
    assert "Liabilities:People:Linda Park" not in de["accounts_at_end"]  # cleared to zero
    # The payback is never spending: before it was explained the line read as
    # spending of its own; now only the fee itself is.
    assert Decimal(after["spent"]) == Decimal(august["spent"]) - Decimal("86.40")


def test_a_solo_missionary_is_told_their_own_yes_approves_it(solo):  # noqa: F811
    daniel = _door(solo, "daniel-reyes")
    said = daniel.call(
        "paid_it_myself", {"day": "2026-08-10", "amount": "20.00", "said": "a filing fee"}
    )
    [card] = _cards(solo)
    assert said["status"] == "waiting" and f"decide_card with card {card['id']}" in said["say"]


def test_a_payback_names_only_someone_the_ministry_owes(solo):  # noqa: F811
    daniel, *_rest = _keep_ministry(solo, MINISTRY_CSV)
    line = next(
        b["source"]
        for p in daniel.call("month_report", {"month": "2026-08"})["people"]
        for b in p["bank"]
        if b["date"] == "2026-08-12"
    )
    asked = daniel.call(
        "explain_bank_line", {"line": line, "said": "paid Linda back", "pays_back": "Linda Park"}
    )
    assert asked["status"] == "asks" and "nothing approved" in asked["say"]


def test_it_asks_for_what_it_needs_and_never_a_day_to_come(solo):  # noqa: F811
    linda = _door(solo, "linda-park")
    assert (
        linda.call("paid_it_myself", {"day": "2026-08-10", "amount": "0", "said": "x"})["status"]
        == "needs"
    )
    assert (
        linda.call("paid_it_myself", {"day": "2026-13-40", "amount": "5", "said": "x"})["status"]
        == "needs"
    )
    assert (
        linda.call("paid_it_myself", {"day": "2099-01-01", "amount": "5", "said": "x"})["status"]
        == "needs"
    )
    assert _cards(solo) == []
