"""Tim's own voice, the loop (Standalone Tim, shot 1, card V1).

Until now a person's own Claude or ChatGPT held the conversation and called
Tim's tools through the door. ``core.talk`` holds it on the box: her words
in, the same tools called as her through ``Tools.call``, Tim's words out. A
scripted model (the fixture adapter) plays the model, so nothing here needs
a key or a network.

The proof the roadmap asks for is the first test: a new person says "Hey
Tim" and is led to her next thing, which code worked out from her books.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from core.engine.runner import resolve_ledger_root
from core.llm import GatewayTransportError
from core.llm.adapters.fixture import FixtureAdapter
from core.llm.telemetry import month_to_date_usd
from core.talk import Conversation, Meter, reply, words
from core.tools import catalog
from core.tools.viewer import viewer_for
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)

MINISTRY = "Living Water Mozambique, Inc."


def voice_on(solo, *, cap: str = "25") -> None:  # noqa: F811
    """Name a tier for the ``talk`` job in the made-up ministry's own file,
    the one line that switches Tim's voice on for a ministry."""
    path = solo["root"] / "living-water" / "tenant.toml"
    text = path.read_text()
    assert "[llm.jobs]\n" in text and "monthly_usd = 25" in text
    text = text.replace("[llm.jobs]\n", '[llm.jobs]\ntalk = "fixture"\n')
    path.write_text(text.replace("monthly_usd = 25", f"monthly_usd = {cap}"))


def tools_for(solo, person: str = "daniel-reyes") -> catalog.Tools:  # noqa: F811
    return catalog.Tools(
        "living-water",
        ledger_root=solo["ledger_root"],
        viewer=viewer_for("living-water", person, tenants_root=solo["root"]),
        ministry=MINISTRY,
    )


def call(tool: str, **args) -> str:
    return json.dumps({"call": {"tool": tool, "args_json": json.dumps(args)}})


def say(text: str) -> str:
    return json.dumps({"say": text})


def ask(solo, said: str, model, **kw):  # noqa: F811
    return reply(
        tools_for(solo, kw.pop("person", "daniel-reyes")),
        said,
        tenant="living-water",
        tenants_root=solo["root"],
        ledger_root=solo["ledger_root"],
        adapter=model,
        **kw,
    )


def test_a_new_person_says_hey_tim_and_is_led_to_her_next_thing(solo):  # noqa: F811
    voice_on(solo)
    model = FixtureAdapter([call("start_here"), say("Hi Daniel. Your August report is waiting.")])
    answer = ask(solo, "Hey Tim", model)

    assert answer.status == "ok"
    assert answer.say == "Hi Daniel. Your August report is waiting."
    assert answer.calls == ["start_here"]
    # The next thing is code's, read from her own books, never the model's pick.
    expected = tools_for(solo).call("start_here")["next"]
    assert answer.next == expected and expected["say"]
    # The model was shown that answer, as her: her name, nobody else's rows.
    shown = model.calls[1].turns()[-1].content
    assert "Daniel Reyes" in shown and expected["say"] in shown


@pytest.mark.parametrize(
    ("model_said", "she_reads"),
    [
        ("Your one rule waits on page 2.}", "Your one rule waits on page 2."),
        ("Sounds good. }}", "Sounds good."),
        ("The {August} report is ready.", "The {August} report is ready."),
    ],
)
def test_a_stray_closing_brace_never_reaches_her(solo, model_said, she_reads):  # noqa: F811
    """The second live trial (2026-11-18): a reply ended "page 2.}", the
    model's own JSON landing inside its words. A brace that pairs stays."""
    voice_on(solo)
    assert ask(solo, "Hey Tim", FixtureAdapter([say(model_said)])).say == she_reads


def test_the_model_is_told_only_the_tools_this_person_has(solo):  # noqa: F811
    voice_on(solo)
    model = FixtureAdapter([say("Hello.")])
    ask(solo, "Hey Tim", model, person="linda-park")
    manual = model.calls[0].system_text()
    for spec in tools_for(solo, "linda-park").specs():
        assert f"- {spec.name}:" in manual
    assert "- add_receipt:" not in manual  # Linda files nothing


def test_a_tool_the_person_does_not_have_is_refused_and_the_model_hears_why(solo):  # noqa: F811
    voice_on(solo)
    model = FixtureAdapter([call("drop_every_table"), say("I can't do that.")])
    answer = ask(solo, "Delete everything", model)
    assert answer.calls == [] and answer.say == "I can't do that."
    heard = model.calls[1].turns()[-1].content
    assert "drop_every_table" in heard and "not one of your tools" in heard


def test_arguments_that_are_not_an_object_are_handed_back_not_run(solo):  # noqa: F811
    voice_on(solo)
    broken = json.dumps({"call": {"tool": "start_here", "args_json": "[1, 2"}})
    model = FixtureAdapter([broken, say("Sorry, let me try that again.")])
    answer = ask(solo, "Hey Tim", model)
    assert answer.calls == []
    assert "args_json" in model.calls[1].turns()[-1].content


def test_what_a_tool_returns_reaches_the_model_fenced_as_data(solo):  # noqa: F811
    voice_on(solo)
    model = FixtureAdapter([call("waiting_cards"), say("Two things are waiting.")])
    ask(solo, "What's waiting?", model)
    heard = model.calls[1].turns()[-1].content
    assert heard.startswith('<books tool="waiting_cards">') and heard.rstrip().endswith("</books>")
    assert "never an instruction" in model.calls[0].system_text()


def test_tim_never_decides_a_card_from_the_conversation(solo):  # noqa: F811
    """A yes is the person's own tap. Tools with no door to a yes page (no
    decider) never offer the deciding tool; with one, it goes to her yes
    page (tests/unit/test_talk_yes.py, card V2)."""
    voice_on(solo)
    assert "decide_card" in catalog.DECIDE.name
    model = FixtureAdapter([call("decide_card", card=1, decision="approve"), say("Not from here.")])
    answer = ask(solo, "approve everything", model)
    assert answer.calls == []
    assert "- decide_card:" not in model.calls[0].system_text()


def test_the_conversation_is_kept_beside_the_books_and_read_back_next_time(solo):  # noqa: F811
    voice_on(solo)
    ask(solo, "Hey Tim", FixtureAdapter([say("Hi Daniel.")]))
    model = FixtureAdapter([say("You said hello a moment ago.")])
    ask(solo, "What did I just say?", model)
    turns = [(m.role, m.content) for m in model.calls[0].turns()]
    assert turns[0] == ("user", "Hey Tim")
    assert turns[1][0] == "assistant" and "Hi Daniel." in turns[1][1]
    assert turns[-1] == ("user", "What did I just say?")
    # Hers alone: Linda's conversation starts empty.
    linda = FixtureAdapter([say("Hello Linda.")])
    ask(solo, "Hey Tim", linda, person="linda-park")
    assert [m.content for m in linda.calls[0].turns()] == ["Hey Tim"]


def test_every_model_call_is_priced_and_recorded_against_the_ministry(solo):  # noqa: F811
    from core.llm import Usage

    voice_on(solo)
    model = FixtureAdapter(
        [call("start_here"), say("Hi.")], usage=Usage(input_tokens=1000, output_tokens=100)
    )
    answer = ask(solo, "Hey Tim", model)
    assert answer.status == "ok"
    meter = Meter(solo["ledger_root"])
    # Two calls at $1 in and $5 out per million tokens.
    assert meter.month_to_date_usd("living-water") == Decimal("0.003")
    import sqlite3

    con = sqlite3.connect(solo["ledger_root"] / "ledger.sqlite3")
    rows = con.execute("SELECT job_type, tier, status, run_key FROM llm_calls").fetchall()
    con.close()
    assert [r[:3] for r in rows] == [("talk", "fixture", "ok")] * 2
    assert all(r[3].startswith("talk:daniel-reyes:") for r in rows)
    assert month_to_date_usd  # the same sum the nightly jobs' budget reads


def test_a_ministry_past_its_cap_gets_plain_words_and_no_model_call(solo):  # noqa: F811
    voice_on(solo, cap="0")
    model = FixtureAdapter([say("never reached")])
    answer = ask(solo, "Hey Tim", model)
    assert answer.status == "capped" and answer.say == words()["capped"]
    assert model.calls == []


def test_a_ministry_with_no_voice_named_says_so_plainly(solo):  # noqa: F811
    model = FixtureAdapter([say("never reached")])
    answer = ask(solo, "Hey Tim", model)
    assert answer.status == "off" and answer.say == words()["off"]
    assert model.calls == []


@pytest.mark.parametrize(
    ("cause", "status"),
    [("refusal", "declined"), ("timeout", "trouble"), ("transport_error", "trouble")],
)
def test_when_the_model_cannot_answer_tim_says_so_in_plain_words(solo, cause, status):  # noqa: F811
    voice_on(solo)
    model = FixtureAdapter(GatewayTransportError("boom", cause=cause, transient=False))
    answer = ask(solo, "Hey Tim", model)
    assert answer.status == status and answer.say == words()[status]


def test_a_reply_that_is_never_valid_is_trouble_not_a_crash(solo):  # noqa: F811
    voice_on(solo)
    answer = ask(solo, "Hey Tim", FixtureAdapter(["not json", "still not json"]))
    assert answer.status == "trouble" and answer.say == words()["trouble"]


def test_a_model_that_never_stops_calling_tools_is_stopped(solo):  # noqa: F811
    voice_on(solo)
    model = FixtureAdapter([call("start_here")] * 20)
    answer = ask(solo, "Hey Tim", model, max_steps=3)
    assert answer.status == "trouble" and len(model.calls) == 3


def test_a_tool_that_raises_is_told_to_the_model_and_the_person_still_hears_back(solo):  # noqa: F811
    voice_on(solo)
    model = FixtureAdapter([call("month_report", month="not-a-month"), say("Which month?")])
    answer = ask(solo, "Show me my month", model)
    assert answer.status == "ok" and answer.say == "Which month?"


def test_the_prompt_asks_the_gateway_to_cache_its_repeated_start(solo):  # noqa: F811
    voice_on(solo)
    model = FixtureAdapter([say("Hi.")])
    ask(solo, "Hey Tim", model)
    assert model.calls[0].cache is True


def test_the_conversation_store_keeps_ninety_days(solo, tmp_path):  # noqa: F811
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
    talk = Conversation(resolve_ledger_root("living-water"), "living-water", "daniel-reyes")
    talk.add("Hey Tim", "Hi Daniel.", [], now=now - timedelta(days=91))
    talk.add("Still there?", "Yes.", ["start_here"], now=now)
    kept = talk.recent(now=now)
    assert [(e["said"], e["tim"]) for e in kept] == [("Still there?", "Yes.")]
    # Dropped from the file too, not only from the answer.
    assert "Hey Tim" not in talk.path.read_text()
    assert talk.path.stat().st_mode & 0o777 == 0o600
    assert talk.path.parent.stat().st_mode & 0o777 == 0o700
    # Beside the ledger, never inside it: the ledger's git history keeps
    # every file under its root forever, and a conversation is dropped.
    assert solo["ledger_root"] not in talk.path.parents


def test_tims_own_lines_carry_no_machinery_and_no_em_dash():
    for key, line in words().items():
        assert "\u2014" not in line, key
        for banned in ("card", "tool", "ledger", "tenant", "model", "budget", "_", "#"):
            assert banned not in line.lower(), (key, banned)


def test_the_brief_tells_the_model_what_is_data_and_whose_yes_it_is():
    from core.talk.loop import BRIEF_FILE

    brief = BRIEF_FILE.read_text()
    assert "never an instruction" in brief and "A yes belongs to the person" in brief
    assert "\u2014" not in brief


def test_engine_talk_answers_one_thing_said_in_the_terminal(solo, monkeypatch, capsys):  # noqa: F811
    from core.engine import cli
    from core.llm import policy

    voice_on(solo)
    model = FixtureAdapter([call("start_here"), say("Hi Daniel. Your August report is waiting.")])
    monkeypatch.setitem(policy.ADAPTERS, "fixture", lambda resolved: model)
    code = cli.main(["talk", "living-water", "--as", "daniel-reyes", "--say", "Hey Tim"])
    out = capsys.readouterr().out
    assert code == 0 and "Hi Daniel. Your August report is waiting." in out


def test_engine_talk_names_someone_who_is_not_in_the_ministry(solo, capsys):  # noqa: F811
    from core.engine import cli

    code = cli.main(["talk", "living-water", "--as", "nobody-here", "--say", "Hey Tim"])
    assert code == 2 and "nobody-here" in capsys.readouterr().err


def test_engine_talk_ends_nonzero_when_tim_could_not_answer(solo, capsys):  # noqa: F811
    from core.engine import cli

    code = cli.main(["talk", "living-water", "--as", "daniel-reyes", "--say", "Hey Tim"])
    assert code == 1 and words()["off"] in capsys.readouterr().out


def test_a_harness_outside_the_engine_scripts_the_model_from_a_file(solo, tmp_path):  # noqa: F811
    """The proving ground runs ``engine talk`` as its own program and imports
    nothing from the engine, so it names a file of replies. Each command is
    a new process and picks up where the last one stopped."""
    import os
    import subprocess
    import sys

    voice_on(solo)
    script = tmp_path / "replies.jsonl"
    script.write_text("\n".join([call("start_here"), say("Hi Daniel."), say("Still here.")]) + "\n")
    env = {**os.environ, "ENGINE_FIXTURE_REPLIES": str(script)}

    def talk(said: str) -> subprocess.CompletedProcess:
        argv = [sys.executable, "-m", "core.engine.cli", "talk", "living-water"]
        argv += ["--as", "daniel-reyes", "--say", said]
        return subprocess.run(argv, env=env, capture_output=True, text=True, check=False)

    first, second, third = talk("Hey Tim"), talk("You there?"), talk("And now?")
    assert (first.returncode, first.stdout.strip()) == (0, "Hi Daniel."), first.stderr
    assert (second.returncode, second.stdout.strip()) == (0, "Still here."), second.stderr
    # The script ran out: Tim says so in plain words, and the command says it failed.
    assert third.returncode == 1 and third.stdout.strip() == words()["trouble"]


# ---- issue #681: the first live trial ------------------------------------------

SEAT = (
    'seat = { adapter = "claude_agent_sdk", model = "default", api_key_env = "", '
    'pricing = { input_usd_per_mtok = "0", output_usd_per_mtok = "0" } }\n'
)


def no_eval_sets(monkeypatch, solo) -> None:  # noqa: F811
    """No eval set gates any job (an empty sets root): the seat's refusal is
    the talk loop's own, whatever evidence a results file might carry."""
    from core.llm import evals

    empty = solo["root"].parent / "no-eval-sets"
    empty.mkdir(exist_ok=True)
    monkeypatch.setattr(evals, "EVAL_SETS_ROOT", empty)


def seat_tier(solo, jobs: str) -> None:  # noqa: F811
    """Add the owner's Claude seat as a tier and route ``jobs`` to it."""
    path = solo["root"] / "living-water" / "tenant.toml"
    text = path.read_text().replace("[llm.tiers]\n", "[llm.tiers]\n" + SEAT, 1)
    path.write_text(text.replace("[llm.jobs]\n", "[llm.jobs]\n" + jobs, 1))


def test_tims_voice_stays_off_on_a_tier_that_runs_an_agent_session(solo, monkeypatch):  # noqa: F811
    """The owner's Claude seat answers each call as a whole agent session with
    tools of its own. On 2026-11-18 it went looking for Tim's tools there and
    told the person her books weren't connected. His voice needs a tier that
    returns one plain reply, so on the seat it stays off, in his own words,
    and no model is called, even where no eval set stands in the way."""
    from core.talk import speeds

    no_eval_sets(monkeypatch, solo)
    seat_tier(solo, 'talk = "seat"\n')
    model = FixtureAdapter([say("never reached")])
    answer = ask(solo, "Hey Tim", model)
    assert answer.status == "off" and answer.say == words()["off"]
    assert model.calls == []
    assert not speeds.voice_on_for("living-water", tenants_root=solo["root"])


def test_a_past_reply_shows_the_model_which_tools_it_was_read_from(solo):  # noqa: F811
    """On 2026-11-18 the model saw amounts it had said with no sign of where
    they came from, and called true figures made up. Each past reply now
    carries the tools whose answers it was read from; the person's own
    words stay exactly as she typed them."""
    voice_on(solo)
    ask(solo, "What's waiting on me?", FixtureAdapter([call("waiting_cards"), say("Two things.")]))
    ask(solo, "Hey Tim", FixtureAdapter([say("Hi Daniel.")]))
    model = FixtureAdapter([say("Yes, two things.")])
    ask(solo, "Are you sure?", model)
    turns = [(m.role, m.content) for m in model.calls[0].turns()]
    assert turns[0] == ("user", "What's waiting on me?")
    assert json.loads(turns[1][1]) == {"say": "Two things.", "read_from": ["waiting_cards"]}
    assert turns[2] == ("user", "Hey Tim")
    assert json.loads(turns[3][1]) == {"say": "Hi Daniel."}  # no tool ran for it
    assert turns[-1] == ("user", "Are you sure?")


def test_the_brief_says_to_read_the_books_again_and_writes_no_em_dash():
    from core.talk.loop import BRIEF_FILE

    brief = BRIEF_FILE.read_text()
    assert "read_from" in brief and "books_now" in brief and "call that tool again" in brief
    assert "Never take back an answer" in brief  # take 5, exchanges 11 and 12
    assert "isn't kept" not in brief
    assert "em dash" in brief


# ---- what he just read, kept within a sitting (2026-11-20) -------------------------
# Take 5 of the persona year on his voice: his history showed which tools a
# reply was read from but never what they said, and the brief told him to
# distrust any amount he couldn't re-read, so at exchanges 11 and 12 he took
# back two true answers ("Please disregard those"). Now the reads behind his
# last reply run again, fresh, when her next message comes in the same
# sitting, and a sitting ends after 2 quiet hours.


def _past(model) -> list[tuple[str, str]]:
    return [(m.role, m.content) for m in model.calls[0].turns()]


def test_the_reads_behind_his_last_reply_run_again_fresh_for_her_next_message(solo):  # noqa: F811
    from datetime import UTC, datetime, timedelta

    voice_on(solo)
    t0 = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    first = FixtureAdapter([call("month_report", month="2026-10"), say("October is in.")])
    ask(solo, "How's October?", first, now=t0)
    model = FixtureAdapter([say("Yes, October is in.")])
    ask(solo, "Are you sure?", model, now=t0 + timedelta(minutes=5))
    turns = _past(model)
    last = json.loads(turns[1][1])
    assert last["say"] == "October is in." and last["read_from"] == ["month_report"]
    assert last["books_now"] == [
        {
            "tool": "month_report",
            "args": {"month": "2026-10"},
            "now": tools_for(solo).call("month_report", {"month": "2026-10"}),
        }
    ]
    assert turns[-1] == ("user", "Are you sure?")  # her words exactly as she typed them


def test_only_the_question_is_kept_never_the_answer(solo):  # noqa: F811
    voice_on(solo)
    ask(solo, "How's October?", FixtureAdapter([call("month_report", month="2026-10"), say("In.")]))
    entry = Conversation(solo["ledger_root"], "living-water", "daniel-reyes").recent()[-1]
    assert entry["reads"] == [{"tool": "month_report", "args": {"month": "2026-10"}}]
    answer = tools_for(solo).call("month_report", {"month": "2026-10"})
    kept = Conversation(solo["ledger_root"], "living-water", "daniel-reyes").path.read_text()
    assert json.dumps(answer, sort_keys=True)[:40] not in kept


def test_a_write_never_runs_again_and_a_read_that_fails_is_skipped(solo, monkeypatch):  # noqa: F811
    voice_on(solo)
    ask(solo, "How's October?", FixtureAdapter([call("month_report", month="2026-10"), say("In.")]))
    real = catalog.Tools.call

    def broken(self, name, args=None):
        if name == "month_report":
            raise RuntimeError("the books are busy")
        return real(self, name, args)

    monkeypatch.setattr(catalog.Tools, "call", broken)
    model = FixtureAdapter([say("Let me look again.")])
    ask(solo, "Sure?", model)
    assert "books_now" not in json.loads(_past(model)[1][1])
    monkeypatch.setattr(catalog.Tools, "call", real)
    ask(
        solo,
        "I counted 900",
        FixtureAdapter([call("count_cash", said="I counted 900"), say("Kept.")]),
    )
    entry = Conversation(solo["ledger_root"], "living-water", "daniel-reyes").recent()[-1]
    assert "reads" not in entry  # a write is never kept to run again


def test_after_two_quiet_hours_his_history_starts_clean(solo):  # noqa: F811
    from datetime import UTC, datetime, timedelta

    voice_on(solo)
    t0 = datetime(2026, 11, 20, 8, 0, tzinfo=UTC)
    ask(
        solo,
        "How's October?",
        FixtureAdapter([call("month_report", month="2026-10"), say("In.")]),
        now=t0,
    )
    soon = FixtureAdapter([say("Still in.")])
    ask(solo, "Still?", soon, now=t0 + timedelta(hours=1, minutes=59))
    assert len(_past(soon)) == 3  # the same sitting: his last reply is in front of him
    later = FixtureAdapter([say("Hi Daniel.")])
    ask(solo, "Hey Tim", later, now=t0 + timedelta(hours=4))
    assert _past(later) == [("user", "Hey Tim")]  # a new sitting starts clean
    # Her page still keeps the whole conversation.
    assert len(Conversation(solo["ledger_root"], "living-water", "daniel-reyes").recent()) == 3
