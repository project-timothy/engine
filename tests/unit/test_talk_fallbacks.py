"""When Tim's model can't answer, the owner's read of 2026-11-19: "I lost my
train of thought" dodged. Before any fallback line, Tim tries his other model
once; when he still can't answer, he says what happened, and a turn that broke
partway names what it finished, in the words of its Yes button. A person's own
write waits a few seconds for the books instead of being turned away on the
spot (``core.engine.runner.DOOR_WAIT``); a scheduled run never waits."""

from __future__ import annotations

import re
import threading
import time

import pytest

from core.engine import runner
from core.engine.runner import LedgerLocked, ledger_write_lock
from core.llm import GatewayTransportError
from core.llm.adapters.fixture import FixtureAdapter
from core.talk import hold, speeds, words
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_talk_loop import call, say, voice_on
from tests.unit.test_talk_speeds import spent, two_speeds
from tests.unit.test_talk_yes import ask, pile


def _down(cause: str = "transport_error") -> GatewayTransportError:
    return GatewayTransportError("down", cause=cause, transient=False)


def _down_on(job_type: str, *replies: str) -> FixtureAdapter:
    """A model whose ``job_type`` never answers; the other gives ``replies``."""
    left = list(replies)

    def seed(bundle, schema):
        if bundle.job_type == job_type:
            raise _down()
        return left.pop(0)

    return FixtureAdapter(seed)


def _then_down(*replies: str) -> FixtureAdapter:
    """A model that gives ``replies``, then stops responding."""
    left = list(replies)

    def seed(bundle, schema):
        if left:
            return left.pop(0)
        raise _down()

    return FixtureAdapter(seed)


def _jobs(model: FixtureAdapter) -> list[str]:
    return [bundle.job_type for bundle in model.calls]


# ---- the other model, before any fallback line ---------------------------------------


def test_when_the_everyday_model_is_down_tim_answers_on_the_strong_one(solo):  # noqa: F811
    two_speeds(solo)
    model = _down_on(speeds.EVERYDAY, say("Hi Daniel."))
    answer = ask(solo, "Hey Tim", model)
    assert answer.status == "ok" and answer.say == "Hi Daniel."
    assert _jobs(model) == [speeds.EVERYDAY, speeds.HARD]


def test_when_the_strong_model_is_down_tim_answers_on_the_everyday_one(solo):  # noqa: F811
    two_speeds(solo)
    model = _down_on(speeds.HARD, say("Here's where your taxes stand."))
    answer = ask(solo, "what tax bracket am i in this year", model)
    assert answer.status == "ok" and answer.say == "Here's where your taxes stand."
    assert _jobs(model) == [speeds.HARD, speeds.EVERYDAY]


def test_the_other_model_carries_the_rest_of_the_turn(solo):  # noqa: F811
    two_speeds(solo)
    model = _down_on(speeds.EVERYDAY, call("start_here"), say("Hi Daniel."))
    answer = ask(solo, "Hey Tim", model)
    assert answer.status == "ok" and _jobs(model) == [speeds.EVERYDAY, speeds.HARD, speeds.HARD]


def test_with_both_down_tim_says_what_happened_after_one_try_on_each(solo):  # noqa: F811
    two_speeds(solo)
    model = FixtureAdapter(_down())
    answer = ask(solo, "Hey Tim", model)
    assert answer.status == "trouble" and answer.say == words()["trouble"]
    assert _jobs(model) == [speeds.EVERYDAY, speeds.HARD]


def test_a_refusal_is_never_tried_on_the_other_model(solo):  # noqa: F811
    two_speeds(solo)
    model = FixtureAdapter(_down("refusal"))
    answer = ask(solo, "Hey Tim", model)
    assert answer.status == "declined" and _jobs(model) == [speeds.EVERYDAY]


def test_past_the_soft_line_a_down_model_never_steps_up(solo):  # noqa: F811
    two_speeds(solo, cap="25")
    spent(solo, "20.00")  # 80% of $25
    model = FixtureAdapter(_down())
    answer = ask(solo, "Hey Tim", model)
    assert answer.status == "trouble" and _jobs(model) == [speeds.EVERYDAY]


def test_with_one_model_named_it_is_tried_once(solo):  # noqa: F811
    voice_on(solo)
    model = FixtureAdapter(_down())
    answer = ask(solo, "Hey Tim", model)
    assert answer.status == "trouble" and len(model.calls) == 1


# ---- a turn that broke partway names what it finished -------------------------------


def test_a_turn_that_breaks_after_a_write_names_what_it_did(solo):  # noqa: F811
    voice_on(solo)
    line = pile(solo, payee="Shoprite")
    args = {"line": line, "said": "43.00"}
    answer = ask(solo, "my July receipt was 43.00", _then_down(call("set_amount", **args)))
    done = hold.summary("set_amount", args, you="Daniel Reyes")
    assert answer.status == "trouble"
    assert answer.say == words()["trouble_done"].format(done=done)
    assert "Set how much a receipt was" in answer.say and "Nothing else changed" in answer.say


def test_a_turn_that_breaks_after_reading_only_says_nothing_changed(solo):  # noqa: F811
    voice_on(solo)
    answer = ask(solo, "what's waiting on me?", _then_down(call("start_here")))
    assert answer.status == "trouble" and answer.say == words()["trouble"]


def test_a_write_that_was_turned_away_is_not_named(solo):  # noqa: F811
    voice_on(solo)
    line = pile(solo, payee="Shoprite")
    model = _then_down(call("set_category", line=line, said="it was for the moon", category="Moon"))
    answer = ask(solo, "put my July receipt under moon", model)
    assert answer.status == "trouble" and answer.say == words()["trouble"]


def test_the_fallback_lines_carry_no_machinery_and_no_dodge():
    lines = words()
    for key in ("trouble", "trouble_after", "trouble_done"):
        assert "train of thought" not in lines[key] and "—" not in lines[key]
        assert "the service that does my thinking" in lines[key]
    assert "{done}" in lines["trouble_done"]


# ---- her own write waits a moment for the books --------------------------------------


def _hold_books(root, seconds: float) -> threading.Thread:
    ready = threading.Event()

    def held() -> None:
        with ledger_write_lock(root):
            ready.set()
            time.sleep(seconds)

    t = threading.Thread(target=held)
    t.start()
    ready.wait(5)
    return t


def test_a_scheduled_run_never_waits_for_the_books(tmp_path):
    t = _hold_books(tmp_path, 0.5)
    started = time.monotonic()
    with pytest.raises(LedgerLocked), ledger_write_lock(tmp_path):
        pass
    assert time.monotonic() - started < 0.2
    t.join()


def test_a_door_write_waits_for_the_books_to_come_free(tmp_path):
    t = _hold_books(tmp_path, 0.3)
    with ledger_write_lock(tmp_path, wait=runner.DOOR_WAIT):
        got = True
    t.join()
    assert got and 0 < runner.DOOR_WAIT <= 5


def test_a_door_write_still_gives_up_when_the_books_stay_busy(tmp_path):
    t = _hold_books(tmp_path, 0.6)
    started = time.monotonic()
    with pytest.raises(LedgerLocked), ledger_write_lock(tmp_path, wait=0.2):
        pass
    assert 0.2 <= time.monotonic() - started < 0.5
    t.join()


def test_every_door_tool_runs_its_jobs_through_the_waiting_door():
    """The door's tools never call the runner's ``run`` straight (a run
    started there would not wait); the CLI and the scheduled jobs do."""
    from pathlib import Path

    tools = Path(runner.__file__).parents[1] / "tools"
    imports = re.compile(r"engine\.runner import ([^#\n]+)")
    found = [
        path.name
        for path in sorted(tools.glob("*.py"))
        for names in imports.findall(path.read_text("utf-8"))
        if "run" in [name.split(" as ")[0].strip() for name in names.split(",")]
    ]
    assert found == []
    for path in sorted(tools.glob("*.py")):
        for line in path.read_text("utf-8").splitlines():
            if "ledger_write_lock(" in line and "def " not in line:
                assert "wait=DOOR_WAIT" in line, f"{path.name}: {line.strip()}"
