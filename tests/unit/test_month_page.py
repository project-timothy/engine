"""The month page (t-month-page, the owner's yes of 2026-10-14): a reviewer
who doesn't use an AI sees a person's month on their phone.

The "month is ready" email links to the doorkeeper, which asks for the
reviewer's own Face ID every time and then hands the browser here with a
one-use grant, in a form. The engine spends the grant with the doorkeeper
(localhost, the box's shared secret), learns who is looking at whose month,
and shows that month as that reviewer may see it: receipts, drives, bank
lines, the total, and where the review stands. Plain HTML, escaped on the
server, no script. When the month's card waits on this reviewer, an Approve
button asks for their yes the usual way: a witness link, Face ID, the tap
finishes it. The button carries a short-lived signature naming the reviewer
and the card, never a bare card id, and never decides by word."""

from __future__ import annotations

import re
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime
from urllib.parse import urlencode

import pytest

from core.ledger import Ledger
from core.tools import month_page as mp
from core.tools.decide import AskBook, Decider
from core.tools.viewer import viewer_for
from tests.unit.test_door_decide import FakeDesk
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_missionary_month import NPR_OCT_12, _month, _rupees, feed  # noqa: F401
from tests.unit.test_tools_answer_as_a_person import church  # noqa: F401  (fixture)


class MonthDesk(FakeDesk):
    """The doorkeeper, standing in: it also holds the grants a reviewer's
    Face ID minted, and spends each once."""

    def __init__(self) -> None:
        super().__init__()
        self.grants: dict[str, tuple[str, str, str]] = {}

    def grant(self, reviewer: str, person: str, month: str) -> str:
        token = f"grant-{len(self.grants) + 1}"
        self.grants[token] = (reviewer, person, month)
        return token

    def spend_month(self, grant: str) -> tuple[str, str, str] | None:
        return self.grants.pop(grant, None)


def _page(tenant: str, books: dict, desk: MonthDesk, *, now=None) -> mp.MonthPage:
    from core.tools import catalog

    asks = AskBook()

    def tools_for(person: str) -> catalog.Tools:
        return catalog.Tools(
            tenant,
            ledger_root=books["ledger_root"],
            viewer=viewer_for(tenant, person, tenants_root=books["root"]),
            today="2026-10-14",
            decider=Decider(
                tenant,
                tenants_root=books["root"],
                ledger_root=books["ledger_root"],
                person=person,
                desk=desk,
                asks=asks,
            ),
        )

    kwargs = {"now": now} if now else {}
    return mp.MonthPage(desk=desk, tools_for=tools_for, ministry="Living Water", **kwargs)


def _approve_form(html: str) -> dict[str, str]:
    fields = dict(re.findall(r'<input type=hidden name="(\w+)" value="([^"]*)"', html))
    assert set(fields) == {"who", "card", "until", "sig"}, html
    return fields


def _card(books: dict, tenant: str, key: str) -> dict:
    with Ledger.open(books["ledger_root"]) as ledger:
        return next(r for r in ledger.list_approvals(tenant) if r["idempotency_key"] == key)


@pytest.fixture
def october(solo, feed):  # noqa: F811
    """Daniel's October: one rupee receipt, priced at its own day's rate,
    and the month's card waiting on him."""
    feed("2026-10-12", npr=NPR_OCT_12)
    _rupees(solo, datetime(2026, 10, 12, 9, 0, tzinfo=UTC))
    _month(solo)
    return solo


# ---- what the reviewer sees


def test_linda_sees_daniels_month_in_plain_words(october):
    desk = MonthDesk()
    status, html = _page("living-water", october, desk).show(
        {"grant": desk.grant("linda-park", "daniel-reyes", "2026-10")}
    )
    assert status == 200, html
    assert "Daniel Reyes&#x27;s October 2026" in html
    assert "NPR 4,300.00" in html and "$28.06" in html and "Oct 12" in html
    assert "153.26 to the dollar on 2026-10-12" in html
    assert "Waiting for approval" in html
    assert "events#" not in html and "expense_line#" not in html  # sources stay in calls


def test_linda_reviews_and_decides_nothing_so_there_is_no_approve_button(october):
    desk = MonthDesk()
    _, html = _page("living-water", october, desk).show(
        {"grant": desk.grant("linda-park", "daniel-reyes", "2026-10")}
    )
    assert "/month/approve" not in html and "<script" not in html


def test_a_grant_is_spent_once(october):
    desk = MonthDesk()
    page = _page("living-water", october, desk)
    grant = desk.grant("linda-park", "daniel-reyes", "2026-10")
    assert page.show({"grant": grant})[0] == 200
    status, html = page.show({"grant": grant})
    assert status == 400 and "link in your email" in html and "$" not in html


def test_a_made_up_grant_shows_nothing(october):
    status, html = _page("living-water", october, MonthDesk()).show({"grant": "grant-99"})
    assert status == 400 and "28.06" not in html


def test_a_person_outside_the_reviewers_units_is_refused(church):  # noqa: F811
    desk = MonthDesk()
    status, html = _page("grace", church, desk).show(
        {"grant": desk.grant("ruth-hollis", "tom-and-jen-baker", "2026-09")}
    )
    assert status == 403 and "Baker" not in html and "2412.75" not in html


def test_a_month_with_nothing_in_it_says_so(october):
    desk = MonthDesk()
    status, html = _page("living-water", october, desk).show(
        {"grant": desk.grant("linda-park", "daniel-reyes", "2026-07")}
    )
    assert status == 200 and "Nothing" in html and "July 2026" in html


def test_every_word_on_the_page_is_escaped():
    person = {
        "person": "Daniel Reyes",
        "total": "1.00",
        "review": "<b>x</b>",
        "lines": [
            {
                "date": "2026-10-01",
                "said": "<script>alert(1)</script>",
                "dollars": "1.00",
                "rate": None,
                "category": "Meals & <i>",
                "fund": None,
                "waiting_on": None,
            }
        ],
        "drives": [],
        "bank": [],
    }
    html = mp.render(person, title="Daniel's October", ministry="A & B", approve=None, nonce="n")
    assert "<script>alert" not in html and "&lt;script&gt;" in html
    assert "<b>x</b>" not in html and "Meals &amp; &lt;i&gt;" in html


# ---- approving from the page


def test_daniel_approves_his_own_report_with_face_id(solo):  # noqa: F811
    desk = MonthDesk()
    page = _page("living-water", solo, desk)
    _, html = page.show({"grant": desk.grant("daniel-reyes", "daniel-reyes", "2026-09")})
    assert "/month/approve" in html
    status, location = page.approve(_approve_form(html))[:2]
    assert status == 303 and location.startswith("https://tim.example.org/yes#")
    [asked] = desk.asked
    assert asked["person"] == "daniel-reyes" and asked["verb"] == "approve"
    assert asked["card"] == str(_card(solo, "living-water", "card-big")["id"])


def test_the_page_never_decides_by_word_even_under_the_line(solo):  # noqa: F811
    desk = MonthDesk()
    page = _page("living-water", solo, desk)
    _, html = page.show({"grant": desk.grant("daniel-reyes", "daniel-reyes", "2026-08")})
    status, _location = page.approve(_approve_form(html))[:2]
    assert status == 303 and len(desk.asked) == 1  # a link, never a decision
    assert _card(solo, "living-water", "card-small")["status"] == "pending"


def test_a_second_tap_hands_back_the_same_link(solo):  # noqa: F811
    desk = MonthDesk()
    page = _page("living-water", solo, desk)
    _, html = page.show({"grant": desk.grant("daniel-reyes", "daniel-reyes", "2026-09")})
    form = _approve_form(html)
    first, second = page.approve(form)[1], page.approve(form)[1]
    assert first == second and len(desk.asked) == 1


@pytest.mark.parametrize(
    "tamper",
    [
        lambda f: {**f, "sig": "0" * len(f["sig"])},
        lambda f: {**f, "card": str(int(f["card"]) + 1)},
        lambda f: {**f, "until": str(int(f["until"]) + 600)},
        lambda f: {k: v for k, v in f.items() if k != "sig"},
        lambda f: {**f, "who": "linda-park"},
    ],
)
def test_a_tampered_button_asks_for_nothing(solo, tamper):  # noqa: F811
    desk = MonthDesk()
    page = _page("living-water", solo, desk)
    _, html = page.show({"grant": desk.grant("daniel-reyes", "daniel-reyes", "2026-09")})
    status = page.approve(tamper(_approve_form(html)))[0]
    assert status == 400 and desk.asked == []


def test_the_button_lives_a_few_minutes(solo):  # noqa: F811
    desk, clock = MonthDesk(), [1_800_000_000.0]
    page = _page("living-water", solo, desk, now=lambda: clock[0])
    _, html = page.show({"grant": desk.grant("daniel-reyes", "daniel-reyes", "2026-09")})
    clock[0] += mp.BUTTON_TTL + 1
    status, _location, body = page.approve(_approve_form(html))
    assert status == 400 and "link in your email" in body and desk.asked == []


def test_a_month_already_decided_has_no_button(solo):  # noqa: F811
    with Ledger.open(solo["ledger_root"]) as ledger:
        ledger.resolve_approval(
            "living-water", _card(solo, "living-water", "card-big")["id"], "approved"
        )
    desk = MonthDesk()
    _, html = _page("living-water", solo, desk).show(
        {"grant": desk.grant("daniel-reyes", "daniel-reyes", "2026-09")}
    )
    assert "/month/approve" not in html and "Approved" in html


# ---- over HTTP, on the box's door


@pytest.fixture
def door(october, tmp_path):
    from core.tools.mcp_http import TokenFile, make_server

    desk = MonthDesk()
    server = make_server(
        "living-water",
        tenants_root=october["root"],
        ledger_root=october["ledger_root"],
        obligations_file=None,
        verifier=TokenFile(tmp_path / "tokens.json"),
        resource="https://tim.example.org/mcp",
        authorization_servers=["https://tim.example.org"],
        port=0,
        desk=desk,
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield {"base": f"http://127.0.0.1:{server.server_address[1]}", "desk": desk}
    finally:
        server.shutdown()


def _post(door, path: str, form: dict, origin: str = ""):
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if origin:
        headers["Origin"] = origin
    req = urllib.request.Request(
        door["base"] + path, data=urlencode(form).encode(), method="POST", headers=headers
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, dict(resp.headers), resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode()


def test_the_door_shows_the_month_to_a_grant_posted_from_the_doorkeepers_page(door):
    grant = door["desk"].grant("linda-park", "daniel-reyes", "2026-10")
    status, headers, html = _post(door, mp.SHOW_PATH, {"grant": grant}, "https://tim.example.org")
    assert status == 200 and "$28.06" in html
    csp = headers["Content-Security-Policy"]
    assert "form-action 'self'" in csp and "script-src 'none'" in csp
    assert headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("origin", ["https://evil.example", "null"])
def test_the_door_refuses_a_post_from_another_site(door, origin):
    grant = door["desk"].grant("linda-park", "daniel-reyes", "2026-10")
    status, headers, html = _post(door, mp.SHOW_PATH, {"grant": grant}, origin)
    assert status == 403 and "28.06" not in html and "your email" in html
    assert door["desk"].grants  # not even spent
    assert headers["Referrer-Policy"] == "same-origin"  # so its own Approve form says where from


def test_the_month_is_never_served_on_a_get(door):
    with pytest.raises(urllib.error.HTTPError) as got:
        urllib.request.urlopen(door["base"] + mp.SHOW_PATH)
    assert got.value.code in (404, 405)
