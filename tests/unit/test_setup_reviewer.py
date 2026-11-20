"""The reviewer's side of setup (docs/screens/README.md, R2 and R3; screens
chunk 3, the owner's go of 2026-11-16).

Someone a waiting expense plan asks to adopt it meets two screens before
anything else: what the person they look over is asking of them (R2), then
the plan itself (R3). The role's yes is setup's own Face ID and writes only
the answer. The plan asks no Face ID on the setup screen: its button is the
plan card's own yes link, where Face ID adopts it, so a card is never
decided on a Face ID minutes old. The step is done once the plan is in
force. Nobody the plan covers is ever shown either screen."""

from __future__ import annotations

import pytest

from core.agents.policy.jobs import PLAN_CARD
from core.engine.config import tenant_dir
from core.engine.runner import run
from core.ledger import Ledger
from core.onboarding.person import SetupBook
from core.tools import catalog
from core.tools.decide import AskBook, Decider, Finisher
from core.tools.setup_page import SetupPage
from core.tools.viewer import viewer_for
from tests.unit.test_door_decide import FakeDesk
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)

ORIGIN = "https://tim.example.org"


class ReviewerDesk(FakeDesk):
    """The doorkeeper, standing in: setup's tokens and grants, and the
    witness links a yes page answers."""

    def __init__(self) -> None:
        super().__init__()
        self.tokens = {"tok-daniel": "daniel-reyes", "tok-linda": "linda-park"}
        self.grants: dict[str, tuple[str, str]] = {}

    def setup_person(self, token: str) -> str | None:
        return self.tokens.get(token)

    def spend_setup(self, grant: str) -> tuple[str, str] | None:
        return self.grants.pop(grant, None)

    def hello(self, person: str) -> bool:
        return True

    def heard(self, person: str) -> bool:
        return False


@pytest.fixture
def books(solo):  # noqa: F811
    authority = solo["root"] / "living-water" / "authority.toml"
    text = authority.read_text()
    old = 'door = ["expense.report", "payment"]'
    assert old in text
    # The plan opens to a yes through the door, as in the solo-plan scenario.
    authority.write_text(text.replace(old, 'door = ["expense.report", "payment", "authority"]'))
    run("living-water", "policy", "propose")  # the box's month timer raises the plan's card
    desk = ReviewerDesk()
    asks = AskBook()

    def tools_for(person: str) -> catalog.Tools:
        return catalog.Tools(
            "living-water",
            ledger_root=solo["ledger_root"],
            viewer=viewer_for("living-water", person, tenants_root=solo["root"]),
            today="2026-11-16",
            decider=Decider(
                "living-water",
                tenants_root=solo["root"],
                ledger_root=solo["ledger_root"],
                person=person,
                desk=desk,
                asks=asks,
            ),
        )

    page = SetupPage(
        "living-water",
        tenants_root=solo["root"],
        desk=desk,
        origin=ORIGIN,
        ministry="Living Water Mozambique, Inc.",
        tools_for=tools_for,
        now=lambda: "2026-11-16T14:00:00+00:00",
    )
    folder = tenant_dir("living-water", tenants_root=solo["root"])
    return {**solo, "desk": desk, "page": page, "folder": folder}


def _call(books, path: str, form: dict) -> tuple[int, dict]:
    raw = "&".join(f"{k}={v}" for k, v in form.items()).encode()
    return books["page"].answer(path, {"Origin": ORIGIN}, raw)


def _yes(books, person: str, step: str) -> tuple[int, dict]:
    books["desk"].grants["g-" + step] = (person, step)
    return _call(books, "/setup/yes", {"grant": "g-" + step})


def _plan_status(books) -> str:
    with Ledger.open(books["ledger_root"]) as ledger:
        rows = [r for r in ledger.list_approvals("living-water") if r["action_type"] == PLAN_CARD]
    return rows[-1]["status"]


def test_the_reviewer_first_hears_what_daniel_is_asking(books):
    status, screen = _call(books, "/setup/next", {"token": "tok-linda"})
    assert status == 200
    assert (screen["step"], screen["stage"]) == ("role", "ask")
    assert screen["ask"] == "Here's what Daniel is asking."
    assert screen["points_heading"] == "What you'll see, and what you won't"
    assert "Daniel's own bank account" in screen["points"][1]
    assert (screen["yes"], screen["later"]) == ("I'll do it", "I can't take this on")
    assert screen["n"] == 1 and screen["name"] == "Linda"


def test_the_missionary_the_plan_covers_never_sees_either_screen(books):
    _, screen = _call(books, "/setup/next", {"token": "tok-daniel"})
    assert screen.get("step") not in ("role", "plan")


def test_ill_do_it_is_done_at_once_and_the_plan_comes_next(books):
    status, screen = _yes(books, "linda-park", "role")
    assert status == 200
    assert SetupBook(books["folder"]).answers("linda-park")["role"]["seen"]
    assert (screen["step"], screen["stage"]) == ("plan", "check")
    assert screen["ask"] == "The ministry's expense plan needs your yes."
    assert "isn't counted as income" in screen["help"]
    assert screen["points_heading"] == "The rules in it"
    assert screen["points"] == [
        "Cash on hand stays at or under $1,500 unless a director approves more.",
        "Every expense is accounted for within 60 days of paying it.",
        "Cash not accounted for goes back to the ministry within 120 days of its monthly "
        "statement.",
        "A receipt is needed for any expense of $75 or more.",
    ]
    assert screen["yes"] == "Yes, adopt the plan"
    assert screen["adopt"].startswith("https://tim.example.org/yes#")
    assert "Living Water Mozambique, Inc." in screen["whole"]  # the plan's own words
    assert _plan_status(books) == "pending"  # the setup screen decided nothing


def test_the_plan_is_done_only_once_her_face_id_adopts_it(books):
    _yes(books, "linda-park", "role")
    _call(books, "/setup/next", {"token": "tok-linda"})
    assert _call(books, "/setup/seen", {"token": "tok-linda", "step": "plan"})[1] == {"seen": False}
    books["desk"].tap()  # her Face ID on the plan's yes page
    Finisher(
        "living-water",
        tenants_root=books["root"],
        ledger_root=books["ledger_root"],
        desk=books["desk"],
    ).run_once()
    assert _plan_status(books) == "approved"
    assert _call(books, "/setup/seen", {"token": "tok-linda", "step": "plan"})[1] == {"seen": True}
    _, screen = _call(books, "/setup/next", {"token": "tok-linda"})
    assert screen["step"] == "daily_email"


def test_a_plan_screen_yes_through_setup_is_refused(books):
    """The plan's yes is its own page's Face ID, never a setup grant."""
    _yes(books, "linda-park", "role")
    status, _screen = _yes(books, "linda-park", "plan")
    assert status == 400
    assert _plan_status(books) == "pending"


def test_i_cant_take_this_on_skips_the_plan(books):
    _call(books, "/setup/later", {"token": "tok-linda", "step": "role"})
    _, screen = _call(books, "/setup/next", {"token": "tok-linda"})
    assert screen["step"] == "daily_email"
    assert _plan_status(books) == "pending"


def test_with_no_plan_waiting_a_reviewer_walks_the_usual_setup(solo):  # noqa: F811
    desk = ReviewerDesk()
    page = SetupPage(
        "living-water",
        tenants_root=solo["root"],
        desk=desk,
        origin=ORIGIN,
        ministry="Living Water Mozambique, Inc.",
        now=lambda: "2026-11-16T14:00:00+00:00",
    )
    _, screen = page.answer("/setup/next", {"Origin": ORIGIN}, b"token=tok-linda")
    assert screen["step"] == "daily_email"
