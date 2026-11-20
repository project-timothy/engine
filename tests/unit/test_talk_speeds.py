"""Two speeds and the cap (Standalone Tim, shot 2, card V3).

Tim costs a ministry $5 a month, so everyday talk runs on the everyday
model (job type ``talk``) and a hard question steps up to the strong one
(``talk_hard``). Stepping up is code, decided by rule: never the person
asking for it, never the model deciding it needs help. The rules come from
the Haiku trial of 2026-11-17, whose misses were tax questions.

The cap is the ministry's one monthly cap. Past its soft line Tim stops
stepping up and says so once a day; at the cap he says his capped line,
and everything that needs no model keeps working.

Two fixture tiers play the two models (``everyday`` and ``strong``); the
``llm_calls`` rows say which one served each call.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from core.engine.config import LlmBudget
from core.llm.adapters.fixture import FixtureAdapter
from core.talk import Conversation, Meter, Waiting, reply, speeds, tapped, words
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_talk_loop import call, say, tools_for
from tests.unit.test_talk_yes import books, box, every_spec

TENANT = "living-water"
TIERS = (
    'everyday = { adapter = "fixture", model = "fixture-model", api_key_env = "LW_KEY", '
    'pricing = { input_usd_per_mtok = "1", output_usd_per_mtok = "5" } }\n'
    'strong = { adapter = "fixture", model = "fixture-model", api_key_env = "LW_KEY", '
    'pricing = { input_usd_per_mtok = "5", output_usd_per_mtok = "25" } }\n'
)


def two_speeds(solo, *, cap: str = "25", soft: str = "", hard: bool = True) -> None:  # noqa: F811
    """The ministry's own file names an everyday tier for ``talk`` and,
    unless ``hard`` is off, a strong one for ``talk_hard``."""
    path = solo["root"] / TENANT / "tenant.toml"
    text = path.read_text()
    text = text.replace("[llm.tiers]\n", "[llm.tiers]\n" + TIERS, 1)
    jobs = 'talk = "everyday"\n' + ('talk_hard = "strong"\n' if hard else "")
    text = text.replace("[llm.jobs]\n", "[llm.jobs]\n" + jobs, 1)
    budget = f"monthly_usd = {cap}\n" + (f"soft_percent = {soft}\n" if soft else "")
    path.write_text(text.replace("monthly_usd = 25\n", budget, 1))


def set_cap(solo, cap: str) -> None:  # noqa: F811
    path = solo["root"] / TENANT / "tenant.toml"
    path.write_text(re.sub(r"(?m)^monthly_usd = .*$", f"monthly_usd = {cap}", path.read_text()))


def served(solo) -> list[tuple[str, str]]:  # noqa: F811
    """(job type, tier) of every model call, in order."""
    con = sqlite3.connect(solo["ledger_root"] / "ledger.sqlite3")
    try:
        rows = con.execute("SELECT job_type, tier FROM llm_calls ORDER BY id").fetchall()
    finally:
        con.close()
    return [(r[0], r[1]) for r in rows if r[0] in (speeds.EVERYDAY, speeds.HARD)]


def spent(solo, usd: str) -> None:  # noqa: F811
    """This month's spend so far, as one recorded call."""
    Meter(solo["ledger_root"]).record_call(
        tenant=TENANT,
        job_type="receipt_read",
        run_key="seed",
        tier="everyday",
        adapter="fixture",
        model="everyday-model",
        usd=Decimal(usd),
    )


def ask(solo, said: str, model, tools=None, **kw):  # noqa: F811
    return reply(
        tools or tools_for(solo),
        said,
        tenant=TENANT,
        tenants_root=solo["root"],
        ledger_root=solo["ledger_root"],
        adapter=model,
        **kw,
    )


EVERYDAY = (speeds.EVERYDAY, "everyday")
HARD = (speeds.HARD, "strong")


# ---- everyday talk runs on the everyday model ---------------------------------------


def test_everyday_talk_runs_on_the_everyday_model(solo):  # noqa: F811
    two_speeds(solo)
    model = FixtureAdapter([call("start_here"), say("Hi Daniel.")])
    answer = ask(solo, "Hey Tim, what's waiting on me?", model)
    assert answer.status == "ok" and answer.say == "Hi Daniel."
    assert served(solo) == [EVERYDAY, EVERYDAY]


# ---- the rules ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "said",
    [
        # The Haiku trial's two tax beats (Y07, Y11), word for word.
        "do i owe florida state tax and can i take a housing allowance like pastors",
        "what tax bracket am i in this year",
        "Did my 1099 from SupportNet come in?",
        "Can I deduct my mileage?",
        "When are my estimated payments due?",
        "I got a letter from the IRS",
    ],
)
def test_a_tax_question_steps_up_without_anyone_asking(solo, said):  # noqa: F811
    two_speeds(solo)
    model = FixtureAdapter([call("tax_packet"), say("Here's where your taxes stand.")])
    answer = ask(solo, said, model)
    assert answer.status == "ok"
    assert served(solo) == [HARD, HARD]


@pytest.mark.parametrize(
    "said",
    [
        "Hey Tim",
        "I took a taxi to the airport, 300 rupees",  # a taxi is not a tax
        "Where's my August report?",
        "Can you send my receipts to Linda?",
    ],
)
def test_words_that_are_not_about_taxes_stay_everyday(solo, said):  # noqa: F811
    two_speeds(solo)
    ask(solo, said, FixtureAdapter([say("Sure.")]))
    assert served(solo) == [EVERYDAY]


def test_a_tax_tool_mid_turn_steps_up_the_rest_of_the_turn(solo):  # noqa: F811
    """She never said tax; the model reached for the packet. The steps
    already taken stand; the rest of the turn runs on the strong model."""
    two_speeds(solo)
    model = FixtureAdapter(
        [call("start_here"), call("tax_packet"), say("Your packet is nearly ready.")]
    )
    answer = ask(solo, "Is my paperwork for April ready?", model)
    assert answer.status == "ok" and answer.calls == ["start_here", "tax_packet"]
    assert served(solo) == [EVERYDAY, EVERYDAY, HARD]


def test_a_statement_tool_mid_turn_steps_up_the_rest_of_the_turn(solo):  # noqa: F811
    two_speeds(solo)
    model = FixtureAdapter(
        [call("explain_bank_line", line="L-7", said="that was the rent"), say("Got it.")]
    )
    ask(solo, "That line on the 3rd was the rent", model, tools=box(solo))
    assert served(solo) == [EVERYDAY, HARD]


def test_the_same_question_again_steps_up(solo):  # noqa: F811
    two_speeds(solo)
    ask(solo, "Where is my August report?", FixtureAdapter([say("It's with Linda.")]))
    ask(solo, "where is my august report??", FixtureAdapter([say("Linda has it.")]))
    ask(solo, "Thanks", FixtureAdapter([say("Any time.")]))
    assert served(solo) == [EVERYDAY, HARD, EVERYDAY]


def test_someone_still_in_setup_gets_the_strong_model(solo):  # noqa: F811
    two_speeds(solo)
    ask(solo, "Hey Tim", FixtureAdapter([say("Hi.")]), in_setup=lambda: True)
    ask(solo, "Hey Tim, hi", FixtureAdapter([say("Hi.")]), in_setup=lambda: False)
    assert served(solo) == [HARD, EVERYDAY]


def test_the_rules_never_ask_about_setup_when_they_cannot_step_up(solo):  # noqa: F811
    two_speeds(solo, hard=False)
    asked: list[str] = []
    ask(solo, "Hey Tim", FixtureAdapter([say("Hi.")]), in_setup=lambda: asked.append("x") or True)
    assert asked == []


def test_the_model_cannot_ask_to_step_up(solo):  # noqa: F811
    """A reply that names the strong model, or asks for help, is only words."""
    two_speeds(solo)
    model = FixtureAdapter([call("talk_hard"), say("I'll think harder.")])
    answer = ask(solo, "Hey Tim", model)
    assert answer.calls == []
    assert served(solo) == [EVERYDAY, EVERYDAY]


def test_a_tap_on_a_held_tax_call_is_answered_by_the_strong_model(solo):  # noqa: F811
    two_speeds(solo)
    where = solo
    ask(
        where,
        "Keep Grace Fellowship on my list of who pays",
        FixtureAdapter(
            [call("keep_payers", payer="Grace Fellowship", year=2026), say("Tap Yes to keep it.")]
        ),
        tools=box(where),
    )
    held = Waiting(where["ledger_root"], TENANT, "daniel-reyes").shown()
    assert held is not None and held["tool"] == "keep_payers"
    tapped(
        box(where),
        held["id"],
        yes=True,
        tenant=TENANT,
        tenants_root=where["root"],
        ledger_root=where["ledger_root"],
        adapter=FixtureAdapter([say("Kept.")]),
    )
    # Held, the tax call still steps the rest of her message up (Tim answers
    # her after a hold, V4 run 3), and her tap's answer is the strong model's.
    assert served(solo) == [EVERYDAY, HARD, HARD]


def test_one_speed_named_runs_every_turn_on_it(solo):  # noqa: F811
    two_speeds(solo, hard=False)
    ask(solo, "what tax bracket am i in this year", FixtureAdapter([say("Let me look.")]))
    assert served(solo) == [EVERYDAY]


def test_the_strong_model_alone_is_no_voice(solo):  # noqa: F811
    path = solo["root"] / TENANT / "tenant.toml"
    text = path.read_text().replace("[llm.tiers]\n", "[llm.tiers]\n" + TIERS, 1)
    path.write_text(text.replace("[llm.jobs]\n", '[llm.jobs]\ntalk_hard = "strong"\n', 1))
    model = FixtureAdapter([say("never reached")])
    answer = ask(solo, "what tax bracket am i in this year", model)
    assert answer.status == "off" and model.calls == []


def test_the_rules_live_in_a_file_the_owner_can_read():
    held = speeds.rules()
    assert "tax" in held["taxes"]["words"] and "housing allowance" in held["taxes"]["words"]
    tools = set(held["taxes"]["tools"]) | set(held["statements"]["tools"])
    assert tools == {
        "tax_packet",
        "keep_payers",
        "keep_tax_facts",
        "keep_travel",
        "bank_statement",
        "explain_bank_line",
        "keep_own_lines",
        "split_line",
    }
    assert tools <= set(every_spec()), "a step-up tool the catalog doesn't have"


# ---- the soft line ----------------------------------------------------------------------


def test_past_the_soft_line_tim_stays_everyday_and_says_so_once_a_day(solo):  # noqa: F811
    two_speeds(solo, cap="25")
    spent(solo, "20.00")  # 80% of $25
    day = datetime(2026, 11, 17, 10, 0, tzinfo=UTC)
    tax = "what tax bracket am i in this year"
    first = ask(solo, tax, FixtureAdapter([say("Let me look.")]), now=day)
    again = ask(solo, "Hey Tim", FixtureAdapter([say("Hi.")]), now=day + timedelta(hours=2))
    tomorrow = ask(solo, "Hey Tim", FixtureAdapter([say("Hi.")]), now=day + timedelta(days=1))
    near = words()["near_cap"]
    assert first.say == f"Let me look.\n\n{near}" and first.near
    assert again.say == "Hi." and not again.near
    assert tomorrow.say == f"Hi.\n\n{near}"
    assert served(solo) == [EVERYDAY, EVERYDAY, EVERYDAY]


def test_below_the_soft_line_nothing_is_said(solo):  # noqa: F811
    two_speeds(solo, cap="25")
    spent(solo, "19.99")
    answer = ask(solo, "what tax bracket am i in this year", FixtureAdapter([say("Look.")]))
    assert answer.say == "Look." and not answer.near
    assert served(solo) == [HARD]


def test_the_soft_line_is_the_ministrys_own_key_beside_the_cap(solo):  # noqa: F811
    two_speeds(solo, cap="10", soft="50")
    spent(solo, "5.00")
    ask(solo, "what tax bracket am i in this year", FixtureAdapter([say("Look.")]))
    assert served(solo) == [EVERYDAY]


def test_with_no_cap_there_is_no_soft_line(solo):  # noqa: F811
    path = solo["root"] / TENANT / "tenant.toml"
    two_speeds(solo)
    path.write_text(path.read_text().replace("monthly_usd = 25\n", "", 1))
    spent(solo, "500")
    answer = ask(solo, "what tax bracket am i in this year", FixtureAdapter([say("Look.")]))
    assert answer.say == "Look." and served(solo) == [HARD]


def test_the_soft_line_never_moves_a_jobs_run_key():
    """The nightly jobs fold the whole ``[llm]`` table into their run keys;
    the soft line is Tim's alone, so it stays out of that dump and no 08:00
    job re-runs because the key exists."""
    assert LlmBudget().model_dump(mode="json") == {"monthly_usd": None}
    assert LlmBudget(monthly_usd=Decimal("5"), soft_percent=50).soft_percent == 50
    assert "soft_percent" not in LlmBudget(soft_percent=50).model_dump(mode="json")
    with pytest.raises(ValueError):
        LlmBudget(soft_percent=101)


# ---- the cap ----------------------------------------------------------------------------


def test_at_the_cap_tim_says_his_capped_line_and_calls_nothing(solo):  # noqa: F811
    two_speeds(solo, cap="0")
    model = FixtureAdapter([say("never reached")])
    answer = ask(solo, "what tax bracket am i in this year", model)
    assert answer.status == "capped" and answer.say == words()["capped"] and not answer.near
    assert model.calls == []


def test_at_the_cap_a_yes_already_waiting_still_runs(solo):  # noqa: F811
    two_speeds(solo)
    where = solo
    ask(
        where,
        "Our travel budget this year is $6,000.",
        FixtureAdapter([call("set_budget", kind="Travel", amount="6000", said="$6,000")]),
        tools=box(where),
    )
    held = Waiting(where["ledger_root"], TENANT, "daniel-reyes").shown()
    assert held is not None and held["tool"] == "set_budget"
    set_cap(solo, "0")
    before = books(where)
    model = FixtureAdapter([say("never reached")])
    answer = tapped(
        box(where),
        held["id"],
        yes=True,
        tenant=TENANT,
        tenants_root=where["root"],
        ledger_root=where["ledger_root"],
        adapter=model,
    )
    assert answer is not None and answer.status == "capped"
    assert answer.say == words()["tapped_ran"] and answer.calls == ["set_budget"]
    assert books(where)["events"] != before["events"]  # her tap ran, with no model
    assert model.calls == []


def test_a_capped_room_still_files_a_receipt_through_its_page(solo, tmp_path):  # noqa: F811
    """The card's proof, over the wire: the ministry is at its cap with both
    Tim's voice and the receipt reader switched on. Tim says his capped
    line; the photo button still hands over a receipt ticket, and the
    receipt page still files the photo."""
    import threading
    import urllib.request
    from urllib.parse import urlencode

    from core.tools import talk_page as tp
    from core.tools.mcp_http import TokenFile, make_server
    from core.tools.receipts import PAGE_PATH, ReceiptDesk
    from tests.unit.test_door_receipts import JPEG
    from tests.unit.test_talk_page import HOST_COOKIE, ORIGIN, SessionDesk

    two_speeds(solo, cap="0")
    path = solo["root"] / TENANT / "tenant.toml"
    reader = '[llm.jobs]\nreceipt_read = "fixture"\n'  # the reader's own proven tier
    path.write_text(path.read_text().replace("[llm.jobs]\n", reader, 1))
    server = make_server(
        TENANT,
        tenants_root=solo["root"],
        ledger_root=solo["ledger_root"],
        obligations_file=None,
        verifier=TokenFile(tmp_path / "tokens.json"),
        resource=ORIGIN + "/mcp",
        authorization_servers=[ORIGIN],
        port=0,
        desk=SessionDesk(),
        receipts=ReceiptDesk(),
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    class Stay(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):  # a 303 is read, never followed
            return None

    def post(where: str, body: bytes, **headers: str):
        headers = {"Origin": ORIGIN, "Cookie": f"{HOST_COOKIE}=ck-daniel", **headers}
        req = urllib.request.Request(base + where, data=body, method="POST", headers=headers)
        try:
            with urllib.request.build_opener(Stay).open(req) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    try:
        form = {"Content-Type": "application/x-www-form-urlencoded"}
        status, _, _ = post(tp.SAY_PATH, urlencode({"said": "Here's a receipt"}).encode(), **form)
        assert status == 303
        [kept] = Conversation(solo["ledger_root"], TENANT, "daniel-reyes").recent()
        assert kept["tim"] == words()["capped"]
        status, headers, _ = post(tp.PHOTO_PATH, b"", **form)
        assert status == 303 and headers["Location"].startswith(PAGE_PATH + "#")
        ticket = headers["Location"].split("#", 1)[1]
        status, _, body = post(
            PAGE_PATH, JPEG, **{"Content-Type": "image/jpeg", "X-Receipt-Ticket": ticket}
        )
        assert status == 200, body
        answer = json.loads(body)
        assert answer["ok"] is True and "ask" not in answer  # filed, with nothing read
    finally:
        server.shutdown()
    filed = [p for p in Path(solo["root"]).parent.rglob("*.jpg") if "Daniel" in str(p)]
    assert filed, "the photo is in his receipts folder"
    assert not [r for r in served(solo) if r[1] != "everyday"]
    con = sqlite3.connect(solo["ledger_root"] / "ledger.sqlite3")
    try:
        statuses = {r[0] for r in con.execute("SELECT status FROM llm_calls").fetchall()}
    finally:
        con.close()
    assert statuses == {"budget_refused"}  # no model was called, and both refusals are kept


# ---- what each room costs ---------------------------------------------------------------


def test_engine_spend_says_what_the_month_cost_by_job_type(solo, capsys):  # noqa: F811
    from core.engine import cli
    from core.llm import Usage

    two_speeds(solo, cap="5")
    usage = Usage(input_tokens=100_000, output_tokens=10_000)
    ask(solo, "Hey Tim", FixtureAdapter([say("Hi.")], usage=usage))
    ask(solo, "what tax bracket am i in this year", FixtureAdapter([say("Look.")], usage=usage))
    capsys.readouterr()

    assert cli.main(["spend", TENANT, "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["tenant"] == TENANT and out["month"] == datetime.now(UTC).strftime("%Y-%m")
    assert out["cap_usd"] == "5" and out["soft_usd"] == "4.00" and out["soft_percent"] == 80
    # $0.10 in + $0.05 out on the everyday tier; five times that on the strong one.
    assert out["by_job"]["talk"] == {"calls": 1, "refused": 0, "usd": "0.15"}
    assert out["by_job"]["talk_hard"] == {"calls": 1, "refused": 0, "usd": "0.75"}
    assert Decimal(out["spent_usd"]) == Decimal("0.90")
    assert out["past_soft_line"] is False and out["capped"] is False

    assert cli.main(["spend", TENANT]) == 0
    line = capsys.readouterr().out.strip()
    assert "\n" not in line
    assert line.startswith(f"{TENANT} ") and "$0.90 of $5.00" in line and "talk_hard" in line


def test_engine_spend_on_a_ledger_no_model_has_touched(solo, capsys):  # noqa: F811
    from core.engine import cli

    assert cli.main(["spend", TENANT, "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["spent_usd"] == "0" and out["by_job"] == {} and out["cap_usd"] == "25"


def test_tims_near_line_carries_no_machinery():
    line = words()["near_cap"]
    assert "—" not in line and "budget" not in line.lower()


def test_a_hard_question_never_steps_up_to_a_tier_that_runs_an_agent_session(
    solo,  # noqa: F811
    monkeypatch,
):
    """Issue #681: the owner's Claude seat can't hold Tim's conversation, at
    either speed. With it named for hard questions, a tax question stays on
    the everyday model, even where no eval set stands in the way."""
    from tests.unit.test_talk_loop import SEAT, no_eval_sets

    no_eval_sets(monkeypatch, solo)
    two_speeds(solo)
    path = solo["root"] / TENANT / "tenant.toml"
    text = path.read_text().replace("[llm.tiers]\n", "[llm.tiers]\n" + SEAT, 1)
    path.write_text(text.replace('talk_hard = "strong"', 'talk_hard = "seat"', 1))
    model = FixtureAdapter([call("tax_packet"), say("Here's where your taxes stand.")])
    answer = ask(solo, "what tax bracket am i in this year", model)
    assert answer.status == "ok"
    assert served(solo) == [EVERYDAY, EVERYDAY]
