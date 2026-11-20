"""Setup flips to Tim's own page where his voice is on (Standalone Tim shot
3, card T2; the owner's decision of 2026-11-17: Tim is a standalone product
with his own AI, and the connector for Claude and ChatGPT stays as a second
door, offered as an "also").

Where the ministry's file names a tier for ``talk``:

- "Where do you want to talk to me?" (screen 12) is offered to everyone,
  with Tim's own page first and Claude or ChatGPT as the "also". Her first
  word to Tim on his page finishes it, as her AI's first question does, so
  setup reaches "done" with no connector at all.
- The home-screen step points at Tim's page; his page opened from the home
  screen (its manifest starts at ``/talk?home``) finishes it.
- ``start_here`` names his page, and the FAQ answers about adding Tim, who
  reads receipts, and seeing a month say so.

Where the voice is off, all of it is as it was: tests/unit/test_voice_off_pinned.py.
"""

from __future__ import annotations

import json

import pytest

from core.capabilities import DOOR_SPECS
from core.capabilities.faq import faq_for, load_faq
from core.engine.cli import main
from core.onboarding.person import Facts, SetupBook, load_steps, offered, words
from core.tools import talk_page as tp
from core.tools.mcp_http import _Asked, _seen
from core.tools.receipts import ReceiptDesk, Receipts
from core.tools.start import _start_here
from core.tools.viewer import viewer_for
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_setup_page import ORIGIN, _call, _yes
from tests.unit.test_setup_page import setup as _setup  # noqa: F401  (fixture)
from tests.unit.test_talk_loop import voice_on

TALK = ORIGIN + "/talk"
DOOR = {s.name for s in DOOR_SPECS}


@pytest.fixture
def setup(_setup):  # noqa: F811
    """Living Water, which offers mileage only (no AI step), with Tim's own
    voice switched on in its file."""
    voice_on(_setup)
    return _setup


def _screen(setup) -> dict:  # noqa: F811
    return _call(setup, "/setup/next", {"token": "tok-daniel"})[1]


def _to(setup, step: str) -> dict:  # noqa: F811
    while (screen := _screen(setup)).get("step") != step:
        assert screen.get("stage") != "done", step
        _call(setup, "/setup/later", {"token": "tok-daniel", "step": screen["step"]})
    return screen


# ---- the steps and their words


def test_talking_to_tim_is_offered_to_everyone_only_where_his_voice_is_on():
    facts = Facts(email="e@example.org", files_expenses=True)
    assert "ai_connector" not in offered(facts, ())
    assert "ai_connector" in offered(facts, (), voice=True)
    assert "ai_connector" in offered(Facts(), (), voice=True)  # a reviewer too
    # the order holds: the email, then talking to Tim, then the home screen
    assert offered(facts, (), voice=True) == ("daily_email", "ai_connector", "home_screen")


def test_the_voice_words_stand_in_and_the_rest_stay():
    off, on = words("ai_connector"), words("ai_connector", voice=True)
    assert on["ask"] == "Where do you want to talk to me?"
    assert on["talk"] and on["also"] and "talk" not in off
    assert words("daily_email", voice=True) == words("daily_email")
    home = words("home_screen", voice=True)
    assert "home screen" in home["ask"] and "receipt page" not in home["ask"]


def test_the_voice_words_are_plain_and_carry_no_em_dash():
    for step in load_steps():
        for key, text in words(step.id, voice=True).items():
            for line in text if isinstance(text, list) else [text]:
                assert "—" not in line, (step.id, key)
                for word in ("connector", "mcp", "tenant", "token", "delve"):
                    assert word not in line.lower(), (step.id, key, word)


# ---- the screens


def test_screen_12_offers_tims_own_page_first_and_the_ai_as_also(setup):
    screen = _to(setup, "ai_connector")
    assert screen["stage"] == "check"  # its button is the yes, as before
    assert screen["ask"] == "Where do you want to talk to me?"
    assert screen["talk"] == TALK and screen["talk_label"] == "Talk to Tim here"
    assert screen["also"] and screen["connector"] == ORIGIN + "/mcp"


def test_setup_finishes_with_no_connector_once_she_talks_to_tim_on_his_page(setup):
    """The card's proof: her first word on his page finishes the step, the
    way her AI's first question does; nothing is connected anywhere."""
    _to(setup, "ai_connector")
    _Asked(SetupBook(setup["folder"]))("daniel-reyes")  # what the talk page calls
    assert _call(setup, "/setup/seen", {"token": "tok-daniel", "step": "ai_connector"})[1] == {
        "seen": True
    }
    _call(setup, "/setup/later", {"token": "tok-daniel", "step": "daily_email"})
    screen = _to(setup, "home_screen")
    assert screen["stage"] == "ask"
    _yes(setup, "daniel-reyes", "home_screen")
    _seen(SetupBook(setup["folder"]), "daniel-reyes", "home_screen")  # opened from home
    done = _screen(setup)
    assert done["stage"] == "done" and done["next"] == ""
    assert "Tim answers you." in done["set"] and "Tim is on your home screen." in done["set"]
    assert {n["step"] for n in done["not_yet"]} == {"daily_email", "mileage"}


def test_the_home_screen_step_points_at_tims_page_and_makes_no_receipt_link(setup):
    _call(setup, "/setup/later", {"token": "tok-daniel", "step": "daily_email"})
    _call(setup, "/setup/later", {"token": "tok-daniel", "step": "ai_connector"})
    status, screen = _yes(setup, "daniel-reyes", "home_screen")
    assert status == 200 and screen["stage"] == "check"
    assert screen["open"] == TALK and screen["open_label"] == "Open Tim's page"
    assert not (setup["folder"] / "receipt_links.toml").exists()


def test_with_the_voice_off_the_ai_step_stays_as_it_was(_setup):  # noqa: F811
    """Living Water offers no AI step; with the voice off it's never asked."""
    steps = []
    while (screen := _screen(_setup)).get("stage") != "done":
        steps.append(screen["step"])
        _call(_setup, "/setup/later", {"token": "tok-daniel", "step": screen["step"]})
    assert "ai_connector" not in steps and screen["next"].startswith("Next, add Tim to Claude")


# ---- the talk page tells setup


class _Desk:
    def session_person(self, token: str) -> str | None:
        return {"ck-daniel": "daniel-reyes"}.get(token)


def _page(solo, talked: list, homed: list) -> tp.TalkPage:  # noqa: F811
    return tp.TalkPage(
        "living-water",
        tenants_root=solo["root"],
        ledger_root=solo["ledger_root"],
        desk=_Desk(),
        origin=ORIGIN,
        tools_for=lambda person: object(),
        receipts=ReceiptDesk(),
        ministry="Living Water",
        on_talked=talked.append,
        on_home=homed.append,
    )


COOKIE = {"Cookie": "__Host-dk_session=ck-daniel", "Origin": ORIGIN}


@pytest.mark.parametrize("on", [True, False], ids=["voice-on", "voice-off"])
def test_her_word_and_the_home_screen_reach_setup_only_where_his_voice_is_on(solo, on, monkeypatch):  # noqa: F811
    if on:
        voice_on(solo)
    talked: list = []
    homed: list = []
    page = _page(solo, talked, homed)
    monkeypatch.setattr(page, "_say", lambda person, tools, said: None)
    manifest = json.loads(page.answer("GET", tp.MANIFEST_PATH, {}, b"")[2])
    assert manifest["start_url"] == ("/talk?home" if on else "/talk")
    assert manifest["scope"] == "/talk"
    monkeypatch.setattr(tp, "render", lambda *a, **k: "page")
    monkeypatch.setattr(tp, "_next", lambda tools: None)
    page.answer("GET", "/talk?home", COOKIE, b"")
    status, _, _ = page.answer("POST", tp.SAY_PATH, COOKIE, b"said=Hey+Tim")
    assert status == 303
    assert talked == (["daniel-reyes"] if on else [])
    assert homed == (["daniel-reyes"] if on else [])


def test_a_new_home_screen_signs_in_and_comes_back_to_the_home_start(solo):  # noqa: F811
    voice_on(solo)
    page = _page(solo, [], [])
    html = page.answer("GET", "/talk?home", {}, b"")[2].decode()
    assert "location.replace('/talk?home')" in html


# ---- start_here and the FAQ


def _tools(solo, talk_page: str = ""):  # noqa: F811
    from core.tools import catalog

    return catalog.Tools(
        "living-water",
        ledger_root=solo["ledger_root"],
        viewer=viewer_for("living-water", "daniel-reyes", tenants_root=solo["root"]),
        ministry="Living Water",
        talk_page=talk_page,
        receipts=Receipts(
            "living-water",
            tenants_root=solo["root"],
            ledger_root=solo["ledger_root"],
            person="daniel-reyes",
            desk=ReceiptDesk(),
            base_url=ORIGIN + "/receipt",
        ),
    )


def test_start_here_names_tims_page_and_hears_the_voice_answers(solo):  # noqa: F811
    on = _start_here(_tools(solo, TALK), {"about": ["add-tim"]})
    assert on["you"]["talk_page"] == TALK
    assert on["answers"][0]["ask"] == "Do I need Claude or ChatGPT to use Tim?"
    off = _start_here(_tools(solo), {"about": ["add-tim"]})
    assert "talk_page" not in off["you"]
    assert off["answers"][0]["ask"] == "How do I add Tim to my AI?"


def test_each_voice_has_whole_answers_with_unique_ids_for_every_door_tool():
    for voice in (False, True):
        entries = load_faq(voice=voice)
        assert len({e["id"] for e in entries}) == len(entries)
        assert DOOR - {name for e in entries for name in e["covers"]} == set()
    on = {e["id"]: e for e in load_faq(voice=True)}
    off = {e["id"]: e for e in load_faq()}
    changed = {i for i in on if on[i] != off.get(i)}
    # phone-or-computer and talk-could-not-finish (2026-11-19) are about Tim's
    # own page, so voice on only; take-back's voice-on answer names his Take
    # back button. talk-first-yes (2026-11-19) is code putting up her yes
    # button on Tim's own page, so voice on only. talk-payback-taxes
    # (2026-11-20) is Tim's own brief answering a payback's taxes; talk-stands-by
    # (2026-11-20) is his fresh reads within a sitting; talk-who-approves
    # (2026-11-20) is the books' own words on who approves; talk-bank-line-yes
    # (2026-11-20) is the Yes card a bank line's purpose waits on.
    # talk-yes-names-it (2026-11-20) is the Yes button naming its act and amount.
    assert changed == {
        "add-tim",
        "who-reads",
        "no-ai-month",
        "phone-or-computer",
        "talk-could-not-finish",
        "take-back",
        "send-several",
        "talk-first-yes",
        "talk-payback-taxes",
        "talk-stands-by",
        "talk-who-approves",
        "talk-bank-line-yes",
        "talk-yes-names-it",
    }
    assert (
        "his own page" in on["add-tim"]["answer"] and "Tim's page" not in off["add-tim"]["answer"]
    )
    for e in on.values():
        assert "—" not in e["answer"]


def test_a_voice_on_ministrys_faq_page_says_his_page_comes_first(solo, capsys):  # noqa: F811
    voice_on(solo)
    assert main(["faq", "--format", "json", "--tenant", "living-water"]) == 0
    entries = {e["id"]: e for e in json.loads(capsys.readouterr().out)}
    assert entries["add-tim"]["ask"] == "Do I need Claude or ChatGPT to use Tim?"
    assert faq_for({"month_report"}, voice=True) != faq_for({"month_report"})


def test_the_box_names_tims_page_only_where_his_voice_is_on_and_it_has_one(solo):  # noqa: F811
    from core.tools.mcp_http import _own_page

    assert _own_page(object(), "living-water", solo["root"], ORIGIN) == ""
    voice_on(solo)
    assert _own_page(object(), "living-water", solo["root"], ORIGIN) == TALK
    assert _own_page(None, "living-water", solo["root"], ORIGIN) == ""
