"""Tim's own page, where she talks to him (Standalone Tim, shot 1, card T1).

One page on her phone, built to the rule every Tim page keeps: rendered on
the box, a few kilobytes, nothing loaded from anywhere else, one post per
message. The one thing waiting on her sits on top, then the conversation,
then one box to type or dictate in and one Send. A photo button hands off to
the receipt page she already knows.

Who is asking is the browser the doorkeeper already remembers (its cookie,
which the engine never reads on its own: it asks the doorkeeper). That
browser reads and talks; it never says yes to a card. A browser it doesn't
remember sees one sign-in on the page itself: the same Face ID step as
"Finish setting up".
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime
from urllib.parse import urlencode

import pytest

from core.llm.adapters.fixture import FixtureAdapter
from core.talk import Conversation, Waiting, words
from core.tools import talk_page as tp
from core.tools.receipts import ReceiptDesk
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_door_presence import SECRET, _desk, _Doorkeeper, doorkeeper  # noqa: F401
from tests.unit.test_talk_loop import MINISTRY, call, say, tools_for, voice_on
from tests.unit.test_talk_yes import card_id

ORIGIN = "https://tim.example.org"
HOST_COOKIE = "__Host-dk_session"


class SessionDesk:
    """The doorkeeper, as the talk page sees it: whose remembered browser."""

    def __init__(self) -> None:
        self.sessions = {"ck-daniel": "daniel-reyes", "ck-linda": "linda-park"}
        self.asked: list[str] = []

    def session_person(self, token: str) -> str | None:
        self.asked.append(token)
        return self.sessions.get(token)


@pytest.fixture
def talk(solo):  # noqa: F811
    desk, receipts = SessionDesk(), ReceiptDesk()
    model: list = []  # what the scripted model says next, set per test

    def page(origin: str = ORIGIN) -> tp.TalkPage:
        return tp.TalkPage(
            "living-water",
            tenants_root=solo["root"],
            ledger_root=solo["ledger_root"],
            desk=desk,
            receipts=receipts,
            origin=origin,
            ministry=MINISTRY,
            tools_for=lambda person: tools_for(solo, person),
            adapter=FixtureAdapter(model) if model else None,
        )

    return {**solo, "desk": desk, "receipts": receipts, "page": page, "model": model}


def _headers(cookie: str = "ck-daniel", origin: str | None = ORIGIN, name=HOST_COOKIE) -> dict:
    out = {"Content-Type": "application/x-www-form-urlencoded"}
    if cookie:
        out["Cookie"] = f"theme=dark; {name}={cookie}"
    if origin is not None:
        out["Origin"] = origin
    return out


def _get(talk, path: str = tp.TALK_PATH, **kw) -> tuple[int, dict, str]:
    status, headers, data = talk["page"]().answer("GET", path, _headers(**kw), b"")
    return status, headers, data.decode("utf-8")


def _post(talk, path: str, form: dict, **kw) -> tuple[int, dict, str]:
    raw = urlencode(form).encode()
    status, headers, data = talk["page"]().answer("POST", path, _headers(**kw), raw)
    return status, headers, data.decode("utf-8")


def _said(talk, person: str = "daniel-reyes") -> list[dict]:
    return Conversation(talk["ledger_root"], "living-water", person).recent()


# ---- the page she opens ----------------------------------------------------------


def test_her_next_thing_sits_on_top_then_the_conversation_then_one_box(talk):
    Conversation(talk["ledger_root"], "living-water", "daniel-reyes").add(
        "Hey Tim", "Hi Daniel. Three things wait on your yes.", ["start_here"]
    )
    status, headers, html = _get(talk)
    assert status == 200
    nxt = tools_for(talk).call("start_here")["next"]["say"]
    top, chat, box = html.find(nxt), html.find("Three things wait"), html.find(tp.SAY_PATH)
    assert -1 < top < chat < box, "the next thing, then the conversation, then the box"
    assert html.count("<textarea") == 1 and 'name="said"' in html
    assert html.count(f">{tp.words()['send']}</button>") == 1
    assert "<table" not in html  # no tables shown to a person
    assert headers["Cache-Control"] == "no-store"
    # She is signed in, and Tim's voice is off: no script runs on her page
    # at all (where it's on, its one script shows him working).
    assert "<script" not in html and "script-src 'none'" in headers["Content-Security-Policy"]


def test_a_new_person_sees_where_to_start(talk):
    html = _get(talk)[2]
    assert tp.words()["start"].replace("'", "&#x27;") in html


def test_only_the_last_few_exchanges_show(talk):
    talk_file = Conversation(talk["ledger_root"], "living-water", "daniel-reyes")
    for n in range(tp.SHOWN + 4):
        talk_file.add(f"message {n:02d}", f"answer {n:02d}", [])
    html = _get(talk)[2]
    assert f"message {tp.SHOWN + 3:02d}" in html and "message 00" not in html


def test_the_page_is_a_few_kilobytes_and_loads_nothing_from_outside(talk):
    for _ in range(tp.SHOWN):
        Conversation(talk["ledger_root"], "living-water", "daniel-reyes").add(
            "What's waiting on me?", "Your August report is waiting on your yes. " * 4, []
        )
    status, headers, html = _get(talk)
    assert len(html.encode()) < 12_000
    assert "http://" not in html and "https://" not in html  # nothing from anywhere else
    assert "default-src 'none'" in headers["Content-Security-Policy"]
    assert "form-action 'self'" in headers["Content-Security-Policy"]


def test_it_wears_the_same_look_as_every_tim_page(talk):
    from core.tools.receipts import _MARK, _STYLE

    html = _get(talk)[2]
    assert _MARK in html and _STYLE in html and "for Living Water Mozambique, Inc." in html


# ---- one post per message ----------------------------------------------------------


def test_saying_something_runs_tims_reply_as_her_then_goes_back_to_the_page(talk):
    voice_on(talk)
    talk["model"] += [call("start_here"), say("Hi Daniel. Your August report is waiting.")]
    status, headers, _ = _post(talk, tp.SAY_PATH, {"said": "Hey Tim"})
    assert status == 303 and headers["Location"] == tp.TALK_PATH + "#now"
    (kept,) = _said(talk)
    assert (kept["said"], kept["tim"], kept["tools"]) == (
        "Hey Tim",
        "Hi Daniel. Your August report is waiting.",
        ["start_here"],
    )
    html = _get(talk)[2]
    assert "Hey Tim" in html and "Your August report is waiting." in html
    assert 'id="now"' in html  # where the 303 lands: her newest words beside the box


def test_a_reload_never_says_it_twice(talk):
    voice_on(talk)
    talk["model"] += [say("Hi.")]
    _post(talk, tp.SAY_PATH, {"said": "Hey Tim"})
    for _ in range(3):
        assert _get(talk)[0] == 200  # what a reload after the 303 asks for
    assert len(_said(talk)) == 1


def test_every_word_is_escaped(talk):
    voice_on(talk)
    talk["model"] += [say('<img src=x onerror="alert(1)"> done')]
    _post(talk, tp.SAY_PATH, {"said": "<script>alert(1)</script>"})
    html = _get(talk)[2]
    assert "<script>alert" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<img src=x" not in html and "&lt;img src=x" in html


def test_a_post_from_another_site_is_refused_and_nothing_is_said(talk):
    voice_on(talk)
    talk["model"] += [say("Hi.")]
    status, headers, html = _post(talk, tp.SAY_PATH, {"said": "Hey"}, origin="https://evil.example")
    assert status == 403 and "Tim&#x27;s page" in html
    assert _said(talk) == []


def test_empty_words_send_nothing(talk):
    status, headers, _ = _post(talk, tp.SAY_PATH, {"said": "   "})
    assert status == 303 and _said(talk) == []


def test_too_many_words_for_one_message_are_refused_in_plain_words(talk):
    voice_on(talk)
    status, headers, _ = _post(talk, tp.SAY_PATH, {"said": "x" * (tp.MAX_SAID + 1)})
    assert status == 303 and headers["Location"] == tp.TALK_PATH + "?long#now"
    assert _said(talk) == []
    html = _get(talk, path=tp.TALK_PATH + "?long")[2]
    assert tp.words()["too_long"].replace("'", "&#x27;") in html


@pytest.mark.parametrize("status", ["off", "capped", "trouble"])
def test_when_tim_cant_answer_the_page_still_works_and_says_his_plain_line(talk, status):
    if status == "capped":
        voice_on(talk, cap="0")
    elif status == "trouble":
        voice_on(talk)  # and the scripted model has nothing to say
    _post(talk, tp.SAY_PATH, {"said": "Hey Tim"})
    status_code, _, html = _get(talk)
    assert status_code == 200
    assert words()[status].replace("'", "&#x27;") in html
    assert tp.PHOTO_PATH in html  # the receipt button still works


def test_the_deciding_tool_is_never_offered_from_the_page(talk):
    """A yes is her own tap (card V2); the conversation never decides."""
    voice_on(talk)
    talk["model"] += [call("decide_card", card="1", decision="approve"), say("I can't do that.")]
    _post(talk, tp.SAY_PATH, {"said": "approve it"})
    (kept,) = _said(talk)
    assert kept["tools"] == []


# ---- one send, one answer (the second live trial, 2026-11-18) --------------------
#
# 5 to 7 seconds with nothing on screen, so she tapped Send again: "Hey Tim"
# was answered twice, "How much have I spent this year?" three times, each a
# different answer.


def _send(page, said: str) -> tuple[int, dict, bytes]:
    return page.answer("POST", tp.SAY_PATH, _headers(), urlencode({"said": said}).encode())


def test_her_page_shows_tim_working_the_moment_she_taps_send(talk):
    voice_on(talk)
    _, headers, html = _get(talk)
    nonce = headers["Content-Security-Policy"].split("'nonce-")[1].split("'")[0]
    assert html.count(f'<script nonce="{nonce}">') == 1 and "<script src" not in html
    assert json.dumps(tp.words()["working"]) in html  # his bubble, shown before the post lands
    assert "fetch(" not in html  # the form still posts on its own; the script only shows it
    assert "go.disabled=true" in html  # and Send can't go twice


def test_a_second_tap_on_send_gets_the_answer_already_given(talk):
    voice_on(talk)
    talk["model"] += [say("Hi Daniel."), say("A second answer.")]
    page = talk["page"]()
    for _ in range(2):
        status, headers, _ = _send(page, "Hey Tim")
        assert status == 303 and headers["Location"] == tp.TALK_PATH + "#now"
    (kept,) = _said(talk)
    assert kept["tim"] == "Hi Daniel." and len(page.adapter.calls) == 1


def test_a_tap_while_tim_is_still_working_waits_for_his_one_answer(talk):
    voice_on(talk)
    entered, release = threading.Event(), threading.Event()
    replies = [say("Hi Daniel."), say("A second answer.")]

    def slow(bundle, schema):
        entered.set()
        release.wait(5)
        return replies.pop(0)

    page = talk["page"]()
    page.adapter = FixtureAdapter(slow)
    out: list = []
    first = threading.Thread(target=lambda: out.append(_send(page, "Hey Tim")))
    first.start()
    assert entered.wait(5)
    second = threading.Thread(target=lambda: out.append(_send(page, "Hey Tim")))
    second.start()
    release.set()
    first.join(5)
    second.join(5)
    assert [status for status, _, _ in out] == [303, 303]
    (kept,) = _said(talk)
    assert kept["tim"] == "Hi Daniel." and len(page.adapter.calls) == 1


def test_new_words_right_after_are_answered(talk):
    voice_on(talk)
    talk["model"] += [say("Hi Daniel."), say("Your August report.")]
    page = talk["page"]()
    _send(page, "Hey Tim")
    _send(page, "What's waiting on me?")
    assert [e["tim"] for e in _said(talk)] == ["Hi Daniel.", "Your August report."]


def test_the_same_words_a_while_later_are_a_new_question(talk, monkeypatch):
    voice_on(talk)
    talk["model"] += [say("Hi Daniel."), say("Hi again.")]
    page = talk["page"]()
    _send(page, "Hey Tim")
    monkeypatch.setattr(tp, "REPEAT_SECONDS", -1)  # long past
    _send(page, "Hey Tim")
    assert [e["tim"] for e in _said(talk)] == ["Hi Daniel.", "Hi again."]


# ---- what waits under his message (the persona year on his voice, 2026-11-18) ------


def test_a_link_from_tim_shows_as_a_button_under_his_message(talk):
    now = datetime.now(UTC)
    waiting = Waiting(talk["ledger_root"], "living-water", "daniel-reyes")
    waiting.link("Tap to add your receipt", ORIGIN + "/receipt#tok", now=now)
    assert '<a class=go href="/receipt#tok">Tap to add your receipt</a>' in _get(talk)[2]
    waiting.link("Tap to add your receipt", "https://elsewhere.example/receipt#tok", now=now)
    assert "elsewhere.example" not in _get(talk)[2]  # only a link this box made


def test_a_yes_button_whose_card_is_decided_is_gone(talk):
    now = datetime.now(UTC)
    waiting = Waiting(talk["ledger_root"], "living-water", "daniel-reyes")
    button = f">{tp.words()['yes_button']}</a>"
    waiting.yes_page(ORIGIN + "/yes#abc", now=now, card=str(card_id(talk, "card-small")))
    assert button in _get(talk)[2]  # still waiting on her yes
    waiting.yes_page(ORIGIN + "/yes#abc", now=now, card="999999")  # decided, or gone
    assert button not in _get(talk)[2]
    assert waiting.shown() is None


# ---- a receipt from the conversation ------------------------------------------------


def test_the_photo_button_hands_off_to_the_receipt_page(talk):
    status, headers, _ = _post(talk, tp.PHOTO_PATH, {})
    assert status == 303
    where, _, ticket = headers["Location"].partition("#")
    assert where == "/receipt" and ticket
    taken, why = talk["receipts"].take(ticket)
    assert why == "" and taken.person == "daniel-reyes" and taken.claim is None


def test_someone_who_files_no_receipts_sees_no_photo_button(talk):
    """Her reviewer never needs it: Linda looks over Daniel's month."""
    html = _get(talk, cookie="ck-linda")[2]
    assert tp.PHOTO_PATH not in html
    status, headers, _ = _post(talk, tp.PHOTO_PATH, {}, cookie="ck-linda")
    assert status == 303 and headers["Location"] == tp.TALK_PATH


def test_a_photo_from_another_site_is_refused(talk):
    status, _, _ = _post(talk, tp.PHOTO_PATH, {}, origin="https://evil.example")
    assert status == 403


# ---- who is asking ------------------------------------------------------------------


def test_a_phone_the_box_doesnt_remember_sees_one_sign_in(talk):
    status, headers, html = _get(talk, cookie="")
    assert status == 200
    w = tp.words()
    assert w["sign_in_button"] in html
    # The doorkeeper's own Face ID sign-in, the one "Finish setting up" uses.
    assert "/setup/start/options" in html and "/setup/start/finish" in html
    # No passkey on this phone: the welcome email, or a new welcome link.
    assert 'href="/welcome"' in html
    assert tp.SAY_PATH not in html and tp.PHOTO_PATH not in html
    csp = headers["Content-Security-Policy"]
    assert "script-src 'nonce-" in csp and "connect-src 'self'" in csp
    nonce = csp.split("'nonce-")[1].split("'")[0]
    assert f'<script nonce="{nonce}">' in html


def test_a_cookie_the_doorkeeper_doesnt_know_is_nobody(talk):
    html = _get(talk, cookie="ck-made-up")[2]
    assert tp.words()["sign_in_button"] in html
    assert talk["desk"].asked == ["ck-made-up"]


def test_on_https_only_the_host_only_cookie_counts(talk):
    """A sibling site could set a plain cookie; only __Host- is the box's own."""
    html = _get(talk, name="dk_session")[2]
    assert tp.words()["sign_in_button"] in html


def test_on_loopback_the_plain_cookie_counts(talk):
    page = talk["page"](origin="http://127.0.0.1:8765")
    headers = _headers(name="dk_session", origin=None)
    html = page.answer("GET", tp.TALK_PATH, headers, b"")[2].decode()
    assert tp.SAY_PATH in html


def test_saying_something_when_signed_out_says_nothing(talk):
    voice_on(talk)
    talk["model"] += [say("Hi.")]
    status, headers, _ = _post(talk, tp.SAY_PATH, {"said": "Hey"}, cookie="")
    assert status == 303 and headers["Location"] == tp.TALK_PATH
    assert _said(talk) == []
    assert _post(talk, tp.PHOTO_PATH, {}, cookie="")[1]["Location"] == tp.TALK_PATH


def test_someone_no_longer_in_the_ministry_is_signed_out(talk):
    talk["desk"].sessions["ck-gone"] = "nobody-here"
    html = _get(talk, cookie="ck-gone")[2]
    assert tp.words()["sign_in_button"] in html


# ---- the home screen ----------------------------------------------------------------


def test_the_page_sits_on_the_home_screen_and_opens_to_itself(talk):
    import json

    html = _get(talk)[2]
    assert f'<link rel="manifest" href="{tp.MANIFEST_PATH}">' in html
    assert '<link rel="apple-touch-icon" href="/receipt/icon-180.png">' in html
    status, headers, data = talk["page"]().answer("GET", tp.MANIFEST_PATH, {}, b"")
    entry = json.loads(data)
    assert status == 200 and headers["Content-Type"] == "application/manifest+json"
    assert entry["start_url"] == tp.TALK_PATH and entry["display"] == "standalone"
    assert entry["short_name"] == "Tim"
    assert {i["src"] for i in entry["icons"]} == {
        "/receipt/icon-192.png",
        "/receipt/icon-512.png",
    }


# ---- the words -------------------------------------------------------------------------


def test_the_words_carry_no_machinery_and_no_em_dash():
    for key, line in tp.words().items():
        assert "—" not in line, key
        for banned in ("card", "tool", "ledger", "tenant", "model", "cookie", "session", "_", "#"):
            assert banned not in line.lower(), (key, banned)


# ---- the engine asks the doorkeeper -------------------------------------------------


def test_the_desk_asks_whose_remembered_browser_it_is(doorkeeper):  # noqa: F811
    _Doorkeeper.replies["/session/who"] = (200, {"person": "daniel-reyes"})
    assert _desk(doorkeeper).session_person("ck") == "daniel-reyes"
    (sent,) = _Doorkeeper.seen
    assert sent["token"] == "ck" and sent["auth"] == f"Bearer {SECRET}"


@pytest.mark.parametrize(
    "reply", [(400, {"error": "unknown"}), (200, {"person": ""}), (200, {"person": ["x"]})]
)
def test_anything_but_a_clean_answer_is_nobody(doorkeeper, reply):  # noqa: F811
    _Doorkeeper.replies["/session/who"] = reply
    assert _desk(doorkeeper).session_person("ck") is None


def test_an_unreachable_doorkeeper_is_nobody(doorkeeper):  # noqa: F811
    assert _desk(doorkeeper, url="http://127.0.0.1:9").session_person("ck") is None
    assert _desk(doorkeeper).session_person("") is None


# ---- on the box's door ----------------------------------------------------------------


@pytest.fixture
def door(solo, tmp_path):  # noqa: F811
    from core.tools.mcp_http import TokenFile, make_server

    desk = SessionDesk()

    def serve(**kw):
        server = make_server(
            "living-water",
            tenants_root=solo["root"],
            ledger_root=solo["ledger_root"],
            obligations_file=None,
            verifier=TokenFile(tmp_path / "tokens.json"),
            resource=ORIGIN + "/mcp",
            authorization_servers=[ORIGIN],
            port=0,
            **kw,
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}"

    servers: list = []
    try:
        yield {**solo, "serve": serve, "desk": desk}
    finally:
        for server in servers:
            server.shutdown()


def _http(base: str, method: str, path: str, form: dict | None = None, cookie: str = ""):
    headers = {"Content-Type": "application/x-www-form-urlencoded", "Origin": ORIGIN}
    if cookie:
        headers["Cookie"] = f"{HOST_COOKIE}={cookie}"
    data = urlencode(form).encode() if form is not None else None
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)

    class Stay(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):  # a 303 is read, never followed
            return None

    try:
        with urllib.request.build_opener(Stay).open(req) as resp:
            return resp.status, dict(resp.headers), resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode()


def test_the_box_serves_the_talk_page_where_it_has_a_doorkeeper(door):
    base = door["serve"](desk=door["desk"], receipts=ReceiptDesk())
    status, headers, html = _http(base, "GET", tp.TALK_PATH, cookie="ck-daniel")
    assert status == 200 and tp.SAY_PATH in html
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    status, headers, _ = _http(base, "POST", tp.SAY_PATH, {"said": "Hey Tim"}, "ck-daniel")
    assert status == 303 and headers["Location"] == tp.TALK_PATH + "#now"
    (kept,) = Conversation(door["ledger_root"], "living-water", "daniel-reyes").recent()
    assert kept["tim"] == words()["off"]  # this ministry hasn't switched his voice on
    status, headers, _ = _http(base, "POST", tp.PHOTO_PATH, {}, "ck-daniel")
    assert status == 303 and headers["Location"].startswith("/receipt#")
    status, headers, _ = _http(base, "GET", tp.MANIFEST_PATH)
    assert status == 200


def test_a_box_without_a_doorkeeper_has_no_talk_page(door):
    base = door["serve"]()
    assert _http(base, "GET", tp.TALK_PATH)[0] == 404
    assert _http(base, "POST", tp.SAY_PATH, {"said": "hi"})[0] == 404


def test_too_big_a_post_is_never_read_into_the_next_request(door):
    base = door["serve"](desk=door["desk"], receipts=ReceiptDesk())
    status, headers, _ = _http(base, "POST", tp.SAY_PATH, {"said": "x" * 100_000}, "ck-daniel")
    assert status == 303 and headers["Location"] == tp.TALK_PATH + "?long#now"
    assert _http(base, "GET", tp.TALK_PATH, cookie="ck-daniel")[0] == 200


def test_someone_still_in_setup_is_answered_by_the_strong_model(talk):
    """Card V3: the page asks setup whether her setup has a step ahead, and
    a yes steps Tim up for her message."""
    import sqlite3

    from tests.unit.test_talk_speeds import two_speeds

    two_speeds(talk)
    asked: list[str] = []
    page = talk["page"]()
    page.in_setup = lambda person: asked.append(person) or person == "daniel-reyes"
    talk["model"].extend([say("Hi."), say("Hi.")])
    page.adapter = FixtureAdapter(talk["model"])
    page.answer("POST", tp.SAY_PATH, _headers(), b"said=Hey+Tim")
    page.answer("POST", tp.SAY_PATH, _headers(cookie="ck-linda"), b"said=Hey+Tim")
    con = sqlite3.connect(talk["ledger_root"] / "ledger.sqlite3")
    rows = con.execute("SELECT job_type FROM llm_calls ORDER BY id").fetchall()
    con.close()
    assert asked == ["daniel-reyes", "linda-park"]
    assert [r[0] for r in rows] == ["talk_hard", "talk"]


# ---- a yes names what it does, and stays as one dated line (t-hark-patterns, 2026-11-20) ----


def _held_fee(talk) -> dict:
    """A cost Tim inferred, held for her tap, with the line code writes for it."""
    from core.talk import hold

    voice_on(talk)
    args = {"day": "2026-10-12", "amount": "2975.00", "said": "state filing fee"}
    line = hold.summary("paid_it_myself", args, you="")
    waiting = Waiting(talk["ledger_root"], "living-water", "daniel-reyes")
    return waiting.hold("paid_it_myself", args, line, now=datetime.now(UTC))


def test_the_yes_button_names_the_act_and_the_amount(talk):
    held = _held_fee(talk)
    assert held["summary"] == "Record a ministry cost you paid yourself: $2,975.00"
    page = _get(talk)[2]
    assert (
        "<button name=answer value=yes>Record a ministry cost you paid yourself: $2,975.00</button>"
        in page
    )
    assert f">{tp.words()['yes']}</button>" not in page  # never a bare "Yes"


def test_after_her_tap_the_button_becomes_one_dated_line(talk):
    held = _held_fee(talk)
    talk["model"][:] = [say("It's in, and it waits on your yes.")]
    _post(talk, tp.YES_PATH, {"id": held["id"], "answer": "yes"})
    kept = _said(talk)[-1]
    assert kept["tap"] == {"answer": "yes", "what": held["summary"]}
    page = _get(talk)[2]
    assert "name=answer value=yes" not in page  # the button is gone
    from core.tools.receipts import person_zone

    line = tp.tapped_line(kept, person_zone("living-water", talk["root"], "daniel-reyes"))
    assert line.startswith("Record a ministry cost you paid yourself: $2,975.00. You said yes ")
    assert line in page
    assert line.endswith(("AM.", "PM."))


def test_not_now_stays_as_one_dated_line_too(talk):
    held = _held_fee(talk)
    _post(talk, tp.YES_PATH, {"id": held["id"], "answer": "no"})
    kept = _said(talk)[-1]
    assert kept["tap"] == {"answer": "no", "what": held["summary"]}
    line = tp.tapped_line(kept, "UTC")
    assert line.startswith("Record a ministry cost you paid yourself: $2,975.00. You said not now ")


def test_the_dated_line_reads_on_her_own_clock():
    entry = {
        "at": "2026-11-20T13:14:00+00:00",
        "tap": {"answer": "yes", "what": "Add a receipt: $9.00"},
    }
    assert tp.tapped_line(entry, "America/New_York") == (
        "Add a receipt: $9.00. You said yes Friday, November 20, 8:14 AM."
    )
    assert tp.tapped_line(entry, "Asia/Kathmandu") == (
        "Add a receipt: $9.00. You said yes Friday, November 20, 6:59 PM."
    )
