"""The box's end of a person's setup (core/tools/setup_page.py, chunk 10).

The doorkeeper's welcome page shows one screen at a time and asks for the
person's own Face ID on every yes; this side answers with the screen, as
plain text the page puts in place, and writes each yes. It learns who is
asking only from the doorkeeper: a setup token (minted after Face ID) for
looking, and a one-use grant naming the person and the step for a yes."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import pytest

from core.engine.config import tenant_dir
from core.mileage.trackers import person_for
from core.onboarding.person import SetupBook
from core.tools.receipts import home_person
from core.tools.setup_page import SetupPage
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)

ORIGIN = "https://tim.example.org"


class FakeDesk:
    """The doorkeeper, as the engine sees it."""

    def __init__(self) -> None:
        self.tokens = {"tok-daniel": "daniel-reyes", "tok-linda": "linda-park"}
        self.grants: dict[str, tuple[str, str]] = {}
        self.hellos: list[str] = []
        self.heard_from: set[str] = set()

    def setup_person(self, token: str) -> str | None:
        return self.tokens.get(token)

    def spend_setup(self, grant: str) -> tuple[str, str] | None:
        return self.grants.pop(grant, None)

    def hello(self, person: str) -> bool:
        self.hellos.append(person)
        return True

    def heard(self, person: str) -> bool:
        return person in self.heard_from


@pytest.fixture
def setup(solo):  # noqa: F811
    folder = tenant_dir("living-water", tenants_root=solo["root"])
    authority = folder / "authority.toml"
    text = authority.read_text()
    assert "[people.daniel-reyes]" in text
    authority.write_text(
        text.replace(
            "[people.daniel-reyes]\n", '[people.daniel-reyes]\nemail = "daniel@example.org"\n', 1
        )
    )
    tenant = folder / "tenant.toml"
    tenant.write_text(tenant.read_text() + '\n[setup]\noffer = ["mileage"]\n')
    desk = FakeDesk()
    page = SetupPage(
        "living-water",
        tenants_root=solo["root"],
        desk=desk,
        origin=ORIGIN,
        ministry="Living Water Mozambique, Inc.",
        now=lambda: "2026-10-22T14:00:00+00:00",
    )
    return {**solo, "folder": folder, "desk": desk, "page": page}


def _call(setup, path: str, form: dict, origin: str = ORIGIN) -> tuple[int, dict]:
    raw = "&".join(f"{k}={v}" for k, v in form.items()).encode()
    status, body = setup["page"].answer(path, {"Origin": origin}, raw)
    return status, body


def _yes(setup, person: str, step: str) -> tuple[int, dict]:
    setup["desk"].grants["g-" + step] = (person, step)
    return _call(setup, "/setup/yes", {"grant": "g-" + step})


def test_the_first_screen_asks_one_thing_in_plain_words(setup):
    status, screen = _call(setup, "/setup/next", {"token": "tok-daniel"})
    assert status == 200
    assert screen["step"] == "daily_email" and screen["stage"] == "ask"
    assert "daniel@example.org" in screen["ask"]
    assert (screen["n"], screen["of"]) == (1, 2)  # mileage waits until wanted
    assert screen["name"] == "Daniel"
    json.dumps(screen)  # text only: the page sets it with textContent


def test_a_yes_takes_a_grant_and_sends_the_hello(setup):
    status, screen = _yes(setup, "daniel-reyes", "daily_email")
    assert status == 200
    assert setup["desk"].hellos == ["daniel-reyes"]
    assert screen["step"] == "daily_email" and screen["stage"] == "check"
    assert "hello" in screen["waiting"].casefold()
    assert SetupBook(setup["folder"]).answers("daniel-reyes")["daily_email"]["yes"]


def test_a_yes_without_a_live_grant_writes_nothing(setup):
    status, screen = _call(setup, "/setup/yes", {"grant": "made-up"})
    assert status == 400 and screen["say"]
    assert SetupBook(setup["folder"]).answers("daniel-reyes") == {}
    assert setup["desk"].hellos == []


def test_the_email_step_is_done_once_the_hello_is_opened(setup):
    _yes(setup, "daniel-reyes", "daily_email")
    assert _call(setup, "/setup/seen", {"token": "tok-daniel", "step": "daily_email"})[1] == {
        "seen": False
    }
    setup["desk"].heard_from.add("daniel-reyes")
    assert _call(setup, "/setup/seen", {"token": "tok-daniel", "step": "daily_email"})[1] == {
        "seen": True
    }
    status, screen = _call(setup, "/setup/next", {"token": "tok-daniel"})
    assert screen["step"] == "home_screen" and screen["stage"] == "ask"


def test_a_welcome_opened_from_the_mail_skips_the_email_step(setup):
    """The sign-up goal (2026-10-27): the welcome reached that inbox, so the
    doorkeeper heard it; no hello, no round trip, and the count starts at the
    first thing actually asked."""
    setup["desk"].heard_from.add("daniel-reyes")
    status, screen = _call(setup, "/setup/next", {"token": "tok-daniel"})
    assert status == 200
    assert screen["step"] == "home_screen" and screen["stage"] == "ask"
    assert (screen["n"], screen["of"]) == (1, 1)
    assert setup["desk"].hellos == []
    held = SetupBook(setup["folder"]).answers("daniel-reyes")["daily_email"]
    assert held["seen"] and held["proved"] == "welcome"
    for step in ("home_screen", "mileage"):
        _call(setup, "/setup/later", {"token": "tok-daniel", "step": step})
    screen = _call(setup, "/setup/next", {"token": "tok-daniel"})[1]
    assert screen["stage"] == "done" and screen["set"] == ["Tim's email reaches you."]


def test_a_hello_already_asked_for_stays_the_hellos_to_finish(setup):
    _yes(setup, "daniel-reyes", "daily_email")
    setup["desk"].heard_from.add("daniel-reyes")
    screen = _call(setup, "/setup/next", {"token": "tok-daniel"})[1]
    assert screen["step"] == "daily_email" and screen["stage"] == "check"
    assert (screen["n"], screen["of"]) == (1, 2)
    assert "proved" not in SetupBook(setup["folder"]).answers("daniel-reyes")["daily_email"]


def test_the_receipt_step_hands_over_the_persons_own_standing_link(setup):
    _call(setup, "/setup/later", {"token": "tok-daniel", "step": "daily_email"})
    status, screen = _yes(setup, "daniel-reyes", "home_screen")
    assert screen["stage"] == "check"
    opened = urlsplit(screen["open"])
    assert f"{opened.scheme}://{opened.netloc}" == ORIGIN
    key = opened.path.rsplit("/", 1)[1]
    assert home_person(setup["folder"], key) == "daniel-reyes"
    again = _call(setup, "/setup/next", {"token": "tok-daniel"})[1]
    assert again["open"] == screen["open"]  # a reload keeps the same link


def test_mileage_waits_outside_sign_up_until_its_wanted(setup):
    """The sign-up goal (2026-10-27): about 8 actions nobody needs on day
    one. Offered on the last screen; asked once the person taps for it."""
    for step in ("daily_email", "home_screen"):
        _call(setup, "/setup/later", {"token": "tok-daniel", "step": step})
    done = _call(setup, "/setup/next", {"token": "tok-daniel"})[1]
    assert done["stage"] == "done"
    assert {"step": "mileage", "name": "Your mileage"} in done["not_yet"]
    status, screen = _call(setup, "/setup/again", {"token": "tok-daniel", "step": "mileage"})
    assert screen["step"] == "mileage" and screen["stage"] == "ask"
    assert (screen["n"], screen["of"]) == (3, 3)


def test_the_mileage_step_sets_up_the_app_with_one_tap(setup):
    for step in ("daily_email", "home_screen"):
        _call(setup, "/setup/later", {"token": "tok-daniel", "step": step})
    status, screen = _call(setup, "/setup/again", {"token": "tok-daniel", "step": "mileage"})
    assert screen["step"] == "mileage" and screen["stage"] == "ask"
    status, screen = _yes(setup, "daniel-reyes", "mileage")
    q = {k: v[0] for k, v in parse_qs(urlsplit(screen["app_link"]).query).items()}
    assert q["url"] == ORIGIN + "/track"
    assert person_for(setup["folder"], q["id"]) == "daniel-reyes"
    assert screen["start_link"] == "org.traccar.client://action/start"
    assert set(screen["stores"]) == {"iphone", "android"}
    assert screen["qr"] and all(set(r) <= {"0", "1"} for r in screen["qr"])
    assert _call(setup, "/setup/next", {"token": "tok-daniel"})[1]["app_link"] == screen["app_link"]


def test_a_step_the_ministry_doesnt_offer_is_never_asked_or_written(setup):
    tenant = setup["folder"] / "tenant.toml"
    tenant.write_text(tenant.read_text().replace('offer = ["mileage"]', "offer = []"))
    status, screen = _yes(setup, "daniel-reyes", "mileage")
    assert status == 400
    assert "mileage" not in SetupBook(setup["folder"]).answers("daniel-reyes")
    assert _call(setup, "/setup/next", {"token": "tok-daniel"})[1]["of"] == 2


def test_when_every_step_is_answered_the_last_screen_says_what_is_set_and_what_waits(setup):
    _yes(setup, "daniel-reyes", "daily_email")
    setup["desk"].heard_from.add("daniel-reyes")
    _call(setup, "/setup/seen", {"token": "tok-daniel", "step": "daily_email"})
    _call(setup, "/setup/later", {"token": "tok-daniel", "step": "home_screen"})
    _call(setup, "/setup/later", {"token": "tok-daniel", "step": "mileage"})
    status, screen = _call(setup, "/setup/next", {"token": "tok-daniel"})
    assert screen["stage"] == "done"
    assert screen["set"] == ["Tim's email reaches you."]
    assert [s["step"] for s in screen["not_yet"]] == ["home_screen", "mileage"]
    assert "Claude or ChatGPT" in screen["next"]
    status, screen = _call(setup, "/setup/again", {"token": "tok-daniel", "step": "mileage"})
    assert screen["step"] == "mileage" and screen["stage"] == "ask"


def test_someone_who_files_no_receipts_and_has_no_address_is_asked_only_what_fits(setup):
    status, screen = _call(setup, "/setup/next", {"token": "tok-linda"})
    assert status == 200
    assert screen["step"] == "daily_email" and screen["of"] == 1  # no receipts, no drives


def test_no_token_no_screen_and_another_origin_is_refused(setup):
    assert _call(setup, "/setup/next", {"token": "nope"})[0] == 401
    assert _call(setup, "/setup/next", {"token": "tok-daniel"}, "https://evil.example")[0] == 403
    assert _call(setup, "/setup/seen", {"token": "tok-daniel", "step": "bank"})[0] == 400


def test_each_person_sees_only_their_own_setup(setup):
    _yes(setup, "daniel-reyes", "daily_email")
    linda = _call(setup, "/setup/next", {"token": "tok-linda"})[1]
    assert "daniel" not in json.dumps(linda).casefold()


def test_the_box_answers_setup_on_its_own_origin(setup, tmp_path):
    import threading
    import urllib.request

    from core.tools.mcp_http import TokenFile, make_server

    server = make_server(
        "living-water",
        tenants_root=setup["root"],
        ledger_root=setup["ledger_root"],
        obligations_file=None,
        verifier=TokenFile(tmp_path / "tokens.json"),
        resource=ORIGIN + "/mcp",
        authorization_servers=["https://signin.example.org"],
        port=0,
        desk=setup["desk"],
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}/setup/next",
            data=b"token=tok-daniel",
            method="POST",
            headers={"Origin": ORIGIN, "Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req) as resp:
            screen = json.loads(resp.read())
        assert screen["step"] == "daily_email" and screen["stage"] == "ask"
    finally:
        server.shutdown()


def test_tim_hears_whether_someone_still_has_a_setup_step_ahead(setup):
    """Tim's step-up rule (card V3) asks setup itself: a step ahead is
    unfinished; the last screen is finished."""
    page = setup["page"]
    assert page.unfinished("daniel-reyes") is True
    _yes(setup, "daniel-reyes", "daily_email")
    setup["desk"].heard_from.add("daniel-reyes")
    _call(setup, "/setup/seen", {"token": "tok-daniel", "step": "daily_email"})
    assert page.unfinished("daniel-reyes") is True  # the home screen is still ahead
    _call(setup, "/setup/later", {"token": "tok-daniel", "step": "home_screen"})
    assert page.unfinished("daniel-reyes") is False  # mileage waits until he wants it
