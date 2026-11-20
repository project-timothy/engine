"""The first time (docs/screens/README.md, chunk 2, the owner 2026-11-16).

The first time a person meets Face ID, a second currency, ATM cash, a
receipt that was partly theirs, a drive from their phone, or a month ready
to send, Tim says what is happening, why, and the rule, the rule taken word
for word from Tim's own FAQ. After that, nothing more: the answer itself is
the one line. "Why" brings the full words back any time. Tim remembers per
person what they have been shown, in a settings file beside ``setup.json``.

Daniel (made up, as in test_door_my_words) runs Living Water alone and
approves his own; Linda, his board treasurer, looks over his month.
"""

from __future__ import annotations

import json
import re
import sqlite3
import stat
from pathlib import Path

import pytest

from core.agents.expenses import statements as st
from core.agents.expenses.records import DRIVE_EVENT, LANDED_EVENT, RECEIPT_SPLIT_EVENT
from core.capabilities.faq import load_faq
from core.tools import catalog, first_time
from core.tools.decide import AskBook, Decider
from core.tools.viewer import viewer_for
from tests.unit.test_atm_cash import _receipt_on
from tests.unit.test_bank_catch_up import _daniel
from tests.unit.test_door_decide import FakeDesk
from tests.unit.test_door_my_words import _card, solo  # noqa: F401  (fixture)
from tests.unit.test_door_receipts import JPEG
from tests.unit.test_next_thing import _no_cards

ME = "daniel-reyes"


def _e(kind: str, payload: dict) -> dict:
    return {"event_type": kind, "payload": payload}


def _drive(person: str = "Daniel Reyes") -> dict:
    return _e(DRIVE_EVENT, {"person": person, "start": "2026-10-13T09:10:00", "miles": "14"})


def _atm(person: str = "Daniel Reyes") -> dict:
    line = st.Line("2026-10-12", "ATM WITHDRAWAL 000123", -30000)
    return _e(st.LINE_EVENT, line.payload(person, st.MINISTRY))


def _landed(sha: str, person: str = "Daniel Reyes", tag: str = "$18.75") -> dict:
    return _e(
        LANDED_EVENT, {"sha256": sha, "person": person, "file": f"2026-10-12_receipt_{tag}.jpg"}
    )


# ---- what is due, from the books ---------------------------------------------------


def test_nothing_new_is_nothing_to_explain():
    assert first_time.due([], ME, next_kind="nothing", seen=set()) is None


@pytest.mark.parametrize(
    ("log", "moment"),
    [
        ([_atm()], "cash"),
        ([_landed("a", tag="NPR4300.00")], "other_currency"),
        ([_landed("b"), _e(RECEIPT_SPLIT_EVENT, {"sha256": "b", "own": "$5.00"})], "split"),
        ([_drive()], "drives"),
    ],
)
def test_each_moment_comes_from_the_persons_own_books(log, moment):
    assert first_time.due(log, ME, next_kind="nothing", seen=set()) == moment


def test_a_month_ready_to_send_is_the_month_moment():
    assert first_time.due([], ME, next_kind="month_ready", seen=set()) == "month_sent"


def test_someone_elses_books_explain_nothing_to_them():
    log = [_drive("Linda Park"), _atm("Linda Park"), _landed("c", "Linda Park", "NPR10.00")]
    assert first_time.due(log, ME, next_kind="nothing", seen=set()) is None


def test_dollars_are_not_another_currency():
    assert first_time.due([_landed("d")], ME, next_kind="nothing", seen=set()) is None


def test_one_at_a_time_and_never_twice():
    log = [_drive(), _atm()]
    assert first_time.due(log, ME, next_kind="nothing", seen=set()) == "cash"
    assert first_time.due(log, ME, next_kind="nothing", seen={"cash"}) == "drives"
    assert first_time.due(log, ME, next_kind="nothing", seen={"cash", "drives"}) is None


# ---- through the door ---------------------------------------------------------------


def _shown_door(solo, person: str = ME) -> catalog.Tools:  # noqa: F811
    from tests.unit.test_ministry_lines import _door

    door = _door(solo, person)
    door.shown = first_time.Shown(Path(solo["root"]) / "living-water")
    return door


def _add(solo, kind: str, payload: dict, key: str) -> None:  # noqa: F811
    con = sqlite3.connect(Path(solo["ledger_root"]) / "ledger.sqlite3")
    con.execute(
        "INSERT INTO events (idempotency_key, run_id, tenant, agent, event_type, payload_json, "
        "created_at) VALUES (?, (SELECT MAX(id) FROM runs), 'living-water', 'expenses', "
        "?, ?, '2026-10-13T00:00:00+00:00')",
        (key, kind, json.dumps(payload)),
    )
    con.commit()
    con.close()


def test_the_first_drive_is_explained_once_and_then_never(solo):  # noqa: F811
    _add(solo, DRIVE_EVENT, _drive()["payload"], "pg-drive")
    door = _shown_door(solo)
    said = door.call("deadlines", {})
    first = said["first_time"]
    assert first["about"] == "drives"
    assert "never where you went" in first["rule"]
    assert first["once"] == "I'll explain this once. Ask me why any time."
    assert list(said)[-2:] == ["first_time", "next"]  # the next thing still closes it
    assert "first_time" not in door.call("deadlines", {})
    assert "first_time" not in _shown_door(solo).call("deadlines", {})  # a new session too


def test_what_was_shown_is_kept_per_person_beside_setup(solo):  # noqa: F811
    _add(solo, DRIVE_EVENT, _drive()["payload"], "pg-drive")
    _shown_door(solo).call("deadlines", {})
    path = Path(solo["root"]) / "living-water" / first_time.FILE
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    held = json.loads(path.read_text())
    assert set(held) == {ME} and set(held[ME]) == {"drives"}
    # Linda has seen nothing; Daniel's drive is not hers to hear about.
    assert "first_time" not in _shown_door(solo, "linda-park").call("deadlines", {})


def test_start_here_explains_too(solo):  # noqa: F811
    _add(solo, DRIVE_EVENT, _drive()["payload"], "pg-drive")
    said = _shown_door(solo).call("start_here", {})
    assert next(iter(said)) == "next" and said["first_time"]["about"] == "drives"


def test_a_door_with_nowhere_to_remember_explains_nothing(solo):  # noqa: F811
    from tests.unit.test_ministry_lines import _door

    _add(solo, DRIVE_EVENT, _drive()["payload"], "pg-drive")
    assert "first_time" not in _door(solo, ME).call("deadlines", {})


def test_a_month_ready_comes_with_what_sending_it_means(solo):  # noqa: F811
    _no_cards(solo)
    door, desk, token = _daniel(solo)
    _receipt_on(door, desk, token, solo, "2026-09-07", "$30.00", JPEG + b"m")
    door.shown = first_time.Shown(Path(solo["root"]) / "living-water")
    said = door.call("start_here", {})
    assert said["next"]["kind"] == "month_ready"
    assert said["first_time"]["about"] == "month_sent"
    assert "stays approved" in said["first_time"]["rule"]


def test_why_brings_the_full_words_back(solo):  # noqa: F811
    _add(solo, DRIVE_EVENT, _drive()["payload"], "pg-drive")
    door = _shown_door(solo)
    door.call("deadlines", {})
    again = door.call("why", {"about": "drives"})
    assert {k: again[k] for k in ("about", "what", "why", "rule")} == {
        k: first_time.words("drives")[k] for k in ("about", "what", "why", "rule")
    }
    assert "first_time" not in again


def test_why_about_something_unknown_says_what_it_can_explain(solo):  # noqa: F811
    said = _shown_door(solo).call("why", {"about": "the weather"})
    assert said["status"] == "needs" and "Face ID" in said["say"]


# ---- Face ID, the first yes over the line -------------------------------------------


def _decides(solo, desk) -> catalog.Tools:  # noqa: F811
    return catalog.Tools(
        "living-water",
        ledger_root=solo["ledger_root"],
        obligations_file=solo["root"] / "none.toml",
        viewer=viewer_for("living-water", ME, tenants_root=solo["root"]),
        decider=Decider(
            "living-water",
            tenants_root=solo["root"],
            ledger_root=solo["ledger_root"],
            person=ME,
            desk=desk,
            asks=AskBook(),
        ),
        shown=first_time.Shown(Path(solo["root"]) / "living-water"),
    )


def test_the_first_face_id_says_why_and_the_page_carries_it(solo):  # noqa: F811
    desk = FakeDesk()
    big = _card(solo, "card-big")["id"]
    said = _decides(solo, desk).call("decide_card", {"card": big, "decision": "approve"})
    assert said["status"] == "tap_to_confirm"
    assert said["first_time"]["about"] == "face_id"
    [asked] = desk.asked
    assert asked["summary"].endswith(first_time.page_words("face_id"))
    assert "\nThe rule: " in asked["summary"]


def test_the_second_face_id_is_just_the_card(solo):  # noqa: F811
    desk = FakeDesk()
    door = _decides(solo, desk)
    door.call("decide_card", {"card": _card(solo, "card-big")["id"], "decision": "approve"})
    water = _card(solo, "card-water")["id"]
    said = _decides(solo, desk).call(
        "decide_card", {"card": water, "decision": "approve", "said": "approve them all"}
    )
    assert said["status"] == "tap_to_confirm" and said.get("first_time") is None
    assert "The rule" not in desk.asked[-1]["summary"]


def test_an_emailed_link_is_not_face_id_so_it_waits_for_the_real_first(solo):  # noqa: F811
    desk = FakeDesk()
    desk.emailed.add(ME)
    big = _card(solo, "card-big")["id"]
    said = _decides(solo, desk).call("decide_card", {"card": big, "decision": "approve"})
    assert said["status"] == "emailed" and "first_time" not in said
    assert "face_id" not in first_time.Shown(Path(solo["root"]) / "living-water").seen(ME)


# ---- the words ----------------------------------------------------------------------


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]


@pytest.mark.parametrize("moment", first_time.MOMENTS)
def test_every_rule_is_taken_word_for_word_from_its_faq_answer(moment):
    answers = {e["id"]: e["answer"] for e in load_faq()}
    said = first_time.words(moment)
    source = answers[first_time.FAQ[moment]]
    for sentence in _sentences(said["rule"]):
        assert sentence in source, (moment, sentence)


@pytest.mark.parametrize("moment", first_time.MOMENTS)
def test_the_words_carry_no_machinery_and_no_em_dash(moment):
    for key, text in first_time.words(moment).items():
        if key == "about":
            continue
        assert "—" not in text, (moment, key)
        for banned in ("card", "tool", "ledger", "tenant", "_", "#"):
            assert banned not in text.lower(), (moment, key, banned)


def test_why_has_its_faq_entry():
    [entry] = [e for e in load_faq() if e["id"] == "explain-once"]
    assert "why" in entry["covers"]
