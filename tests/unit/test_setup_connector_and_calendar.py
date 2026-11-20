"""Two more yeses in a person's setup (chunk 11): Tim in their own AI, and
Tim's dates in their own calendar. Each is offered only where the ministry's
``[setup] offer`` names it, and, like every step, it's done only once Tim
sees it work.

- ``ai_connector``: the screen hands over the one link to add in Claude or
  ChatGPT. Done the first time that person asks Tim something from their AI
  (a tool call through the door as them; the connector's own handshake
  isn't enough).
- ``calendar``: the screen hands over the person's own subscription link
  (one tap on an iPhone, Google's add-by-link on Android or a computer, a
  QR for anywhere). The feed carries the dates Tim watches that this person
  may see, with the reminders ahead. Done the first time a calendar fetches
  it; a browser opening the link doesn't count. Tim only adds dates, and
  never reads the calendar.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlsplit

import pytest

from core.engine.cli import main
from core.onboarding.person import SetupBook
from core.tools.mcp_http import TokenFile, make_server, new_token
from core.tools.setup_page import NEXT_WORDS
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_setup_page import ORIGIN, _call, _yes
from tests.unit.test_setup_page import setup as _setup  # noqa: F401  (fixture)

OBLIGATIONS = """
[[obligation]]
id = "visa"
title = "Mozambique DIRE renewal"
who = "Daniel Reyes"
due = 2026-12-01

[[obligation]]
id = "linda-cpa"
title = "Linda's own license renewal"
who = "Linda Park"
due = 2026-11-15
"""


@pytest.fixture
def setup(_setup):  # noqa: F811
    tenant = _setup["folder"] / "tenant.toml"
    tenant.write_text(
        tenant.read_text().replace(
            'offer = ["mileage"]', 'offer = ["mileage", "calendar", "ai_connector"]'
        )
    )
    return _setup


def _skip_to(setup, step: str) -> dict:
    """Not now on every step before ``step``; the screen it lands on. The
    calendar waits outside sign-up, so it's asked for first, the way the
    last screen's button does."""
    if step == "calendar":
        _call(setup, "/setup/again", {"token": "tok-daniel", "step": step})
    while True:
        screen = _call(setup, "/setup/next", {"token": "tok-daniel"})[1]
        if screen.get("step") == step:
            return screen
        _call(setup, "/setup/later", {"token": "tok-daniel", "step": screen["step"]})


def test_the_ai_comes_before_the_receipt_page_and_the_calendar_waits_until_its_wanted(setup):
    """The sign-up goal (2026-10-27): mileage and the calendar are offered,
    never asked during sign-up; sign-up is the email, the AI, and receipts.
    The receipt page comes last (2026-10-28): Tim in their AI is the point,
    and the home-screen step sends them out of the browser."""
    screen = _call(setup, "/setup/next", {"token": "tok-daniel"})[1]
    assert screen["of"] == 3
    assert _skip_to(setup, "ai_connector")["n"] == 2
    _call(setup, "/setup/later", {"token": "tok-daniel", "step": "ai_connector"})
    receipts = _call(setup, "/setup/next", {"token": "tok-daniel"})[1]
    assert (receipts["step"], receipts["n"]) == ("home_screen", 3)
    _call(setup, "/setup/later", {"token": "tok-daniel", "step": "home_screen"})
    done = _call(setup, "/setup/next", {"token": "tok-daniel"})[1]
    assert done["stage"] == "done"
    assert {"mileage", "calendar"} <= {s["step"] for s in done["not_yet"]}
    calendar = _skip_to(setup, "calendar")  # asked for: one more screen
    assert (calendar["n"], calendar["of"]) == (4, 4)


def test_the_calendar_step_hands_over_the_persons_own_subscription(setup):
    _skip_to(setup, "calendar")
    status, screen = _yes(setup, "daniel-reyes", "calendar")
    assert status == 200 and screen["stage"] == "check"
    feed = urlsplit(screen["feed"])
    assert f"{feed.scheme}://{feed.netloc}" == ORIGIN
    assert feed.path.startswith("/cal/") and feed.path.endswith(".ics")
    assert screen["subscribe"] == "webcal://" + feed.netloc + feed.path
    google = parse_qs(urlsplit(screen["google"]).query)
    assert google["cid"] == [screen["subscribe"]]
    assert screen["qr"] and all(set(r) <= {"0", "1"} for r in screen["qr"])
    again = _call(setup, "/setup/next", {"token": "tok-daniel"})[1]
    assert again["feed"] == screen["feed"]  # a reload keeps the same link


def test_the_ai_step_hands_over_the_one_link_to_add_with_no_face_id_first(setup):
    screen = _skip_to(setup, "ai_connector")
    # the sign-up goal (2026-10-28): the button is the yes; no Face ID screen first
    assert screen["stage"] == "check" and "yes" not in screen
    assert screen["connector"] == ORIGIN + "/mcp"
    assert "Claude" in screen["waiting"] and "ChatGPT" in screen["waiting"]
    # fix 6 of the sign-up goal: the page's button fills in Claude's own add
    # box, so the words name that button and the three taps after it
    assert "Add Tim to Claude" in screen["waiting"]
    assert "Add, Connect, and Allow" in screen["waiting"]
    assert "Settings, then Connectors" not in screen["waiting"]
    # the owner's iPhone walk: Claude refuses a second connector at Tim's
    # address, so the words say where the first one is (Claude moved
    # connectors under Customize)
    assert "already there" in screen["waiting"]
    assert "Customize, then Connectors" in screen["waiting"]


def test_the_ai_step_is_done_once_their_ai_asks_tim_something(setup):
    _skip_to(setup, "ai_connector")
    assert _call(setup, "/setup/seen", {"token": "tok-daniel", "step": "ai_connector"})[1] == {
        "seen": False
    }
    SetupBook(setup["folder"]).saw("daniel-reyes", "ai_connector", now="2026-10-28T12:14:51+00:00")
    assert _call(setup, "/setup/seen", {"token": "tok-daniel", "step": "ai_connector"})[1] == {
        "seen": True
    }


def test_the_words_after_setup_point_at_claudes_customize_page():
    assert "Customize, then Connectors" in NEXT_WORDS
    assert "Settings, Connectors" not in NEXT_WORDS


def test_the_last_screen_drops_the_ai_note_once_its_a_step_of_its_own(setup):
    while (done := _call(setup, "/setup/next", {"token": "tok-daniel"})[1]).get("step"):
        _call(setup, "/setup/later", {"token": "tok-daniel", "step": done["step"]})
    assert done["stage"] == "done"
    assert done["next"] == ""
    assert "ai_connector" in [s["step"] for s in done["not_yet"]]
    assert NEXT_WORDS  # still said where the ministry doesn't offer the step


# ---- the box, end to end ---------------------------------------------------------


@pytest.fixture
def box(setup, tmp_path):
    obligations = tmp_path / "obligations.toml"
    obligations.write_text(OBLIGATIONS)
    tokens = tmp_path / "tokens.json"
    server = make_server(
        "living-water",
        tenants_root=setup["root"],
        ledger_root=setup["ledger_root"],
        obligations_file=obligations,
        verifier=TokenFile(tokens),
        resource=ORIGIN + "/mcp",
        authorization_servers=["https://signin.example.org"],
        port=0,
        desk=setup["desk"],
        today=lambda: "2026-10-23",
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield {
        **setup,
        "base": f"http://127.0.0.1:{server.server_address[1]}",
        "token": new_token(tokens, "daniel-reyes"),
    }
    server.shutdown()


def _get(box, path: str, headers: dict | None = None) -> tuple[int, str, str]:
    req = urllib.request.Request(box["base"] + path, headers=headers or {})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), resp.read().decode()
    except urllib.error.HTTPError as err:
        return err.code, "", ""


def _seen(box, step: str) -> bool:
    return bool(SetupBook(box["folder"]).answers("daniel-reyes").get(step, {}).get("seen"))


def test_a_calendar_fetching_the_feed_finishes_the_step(box):
    _skip_to(box, "calendar")
    screen = _yes(box, "daniel-reyes", "calendar")[1]
    path = urlsplit(screen["feed"]).path
    # Daniel opening the link in his browser shows it, and isn't the calendar.
    status, kind, _body = _get(box, path, {"Sec-Fetch-Mode": "navigate", "Accept": "text/html"})
    assert status == 200 and not _seen(box, "calendar")
    status, kind, body = _get(box, path, {"User-Agent": "iOS/26.0 dataaccessd/1.0"})
    assert status == 200 and kind.startswith("text/calendar")
    assert body.startswith("BEGIN:VCALENDAR")
    assert "Mozambique DIRE renewal" in body
    assert "BEGIN:VALARM" in body  # the reminders ride along
    # Exactly the dates the deadlines tool gives Daniel, who he may see.
    rows = json.loads(
        _mcp(box, "tools/call", {"name": "deadlines", "arguments": {"days": 400}})["result"][
            "content"
        ][0]["text"]
    )["rows"]
    summaries = {
        line[len("SUMMARY:") :] for line in body.splitlines() if line.startswith("SUMMARY:")
    }
    assert summaries == {r["title"] for r in rows}
    assert _seen(box, "calendar")
    assert _get(box, "/cal/not-a-real-key.ics")[0] == 404


def test_a_retired_calendar_link_stops_working(box, capsys, monkeypatch):
    _skip_to(box, "calendar")
    path = urlsplit(_yes(box, "daniel-reyes", "calendar")[1]["feed"]).path
    assert main(["calendar-link", "living-water", "daniel-reyes", "--revoke"]) == 0
    assert "1 calendar link retired" in capsys.readouterr().out
    assert _get(box, path)[0] == 404


def _mcp(box, method: str, params: dict | None = None) -> dict:
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    req = urllib.request.Request(
        box["base"] + "/mcp",
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Authorization": "Bearer " + box["token"], "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def test_the_first_question_from_their_ai_finishes_the_ai_step(box):
    # the button is the yes (2026-10-28): no Face ID screen before it, so
    # the first question from their AI is all Tim needs to see
    _skip_to(box, "ai_connector")
    _mcp(box, "initialize", {"protocolVersion": "2025-06-18"})
    _mcp(box, "tools/list")
    assert not _seen(box, "ai_connector")  # connected isn't asked
    answer = _mcp(box, "tools/call", {"name": "deadlines", "arguments": {}})
    assert "result" in answer
    assert _seen(box, "ai_connector")
    screen = _call(box, "/setup/next", {"token": "tok-daniel"})[1]
    assert screen["step"] == "home_screen"  # on to the last screen by itself


# ---- Claude knows Tim by name (the owner's iPhone walk, 2026-10-29) ---------------
# "What should I do first?" got Claude's own answer; "Tim, ..." reached Tim.
# The door now tells the person's AI who Tim is and what to bring to Tim.


def test_the_door_tells_their_ai_who_tim_is_by_every_name(box):
    said = _mcp(box, "initialize", {"protocolVersion": "2025-06-18"})["result"]["instructions"]
    for name in ("Tim", "Timothy", "Timmy"):
        assert name in said
    assert "what to do first" in said and "what's waiting" in said
    assert "  " not in said and "—" not in said  # joined as prose


def test_start_here_is_called_when_they_greet_tim_or_ask_what_to_do_first():
    from core.tools.catalog import SPECS

    start = next(s for s in SPECS if s.name == "start_here")
    assert "Timothy or Timmy" in start.description
    assert "what to do first" in start.description


def test_the_ai_step_gives_an_opener_that_names_tim(setup):
    screen = _skip_to(setup, "ai_connector")
    assert "Tim, what's waiting for me?" in screen["waiting"]
