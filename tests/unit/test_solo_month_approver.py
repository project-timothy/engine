"""A solo missionary's month approver (board w-solo-reviewer-approves, chunk
c48; the owner's decision of 2026-11-17).

A ministry one person runs approves its own months, and many have to. Setup
asks a solo missionary whether the person who looks over her spending should
also approve each month. If so, that person's yes decides the month, and the
month only: the rest of the ministry's cards (which receipt goes with which
line, a draft, a duplicate) stay the missionary's. She can still approve a
month herself, when they can't, and they are told she did.
"""

from __future__ import annotations

import tomllib

import pytest

from core.agents.expenses import jobs as expense_jobs
from core.agents.expenses.jobs import DRAFT_CARD
from core.agents.expenses.records import REVIEW_CARD
from core.authority import (
    AuthorityError,
    Permission,
    Request,
    card_request,
    evaluate,
    may_grant,
    parse_permission,
    parse_policy,
)
from core.onboarding import apply, load_questions, next_question, plan, record
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)

# ---- a grant for one kind of card ---------------------------------------------------


def test_a_permission_can_name_one_kind_of_card():
    perm = parse_permission("approve:expense.report#month")
    assert (perm.action, perm.resource, perm.kind) == ("approve", "expense.report", "month")
    assert parse_permission("approve:expense.report").kind == ""


@pytest.mark.parametrize("text", ["approve:expense.report#", "approve:expense.report#Month!"])
def test_a_malformed_kind_refuses(text):
    with pytest.raises(AuthorityError):
        parse_permission(text)


def test_a_kind_grant_reaches_only_that_kind():
    month = parse_permission("approve:expense.report#month")
    assert month.matches("approve", "expense.report", "month")
    assert not month.matches("approve", "expense.report", "")
    assert not month.matches("approve", "expense.report", "draft")
    every = parse_permission("approve:expense.report")
    assert every.matches("approve", "expense.report", "month")
    assert every.matches("approve", "expense.report", "")


def test_the_month_card_is_the_month_kind_and_nothing_else_is():
    rules = expense_jobs.CARD_AUTHORITY
    assert rules[REVIEW_CARD].kind == "month"
    assert {t for t, r in rules.items() if r.kind == "month"} == {REVIEW_CARD}


SOLO_POLICY = """
[money]
out = "human"

[safeguards]
self_approval = "allowed"
self_approval_limit = 0
review_after = "monthly"
distinct_payer = "off"
distinct_payer_above = 0
second_approver_above = 0
authority_confirm = "self"
report_self_approvals = "always"
yes_by_word_up_to = 0

[roles.missionary]
grants = ["*:*"]

[roles.reviewer]
grants = ["view:*", "review:*", "approve:expense.report#month"]

[people.daniel-reyes]
roles = ["missionary"]
name = "Daniel Reyes"

[people.linda-park]
roles = ["reviewer"]
name = "Linda Park"
"""


def test_nobody_grants_a_wider_kind_than_they_hold():
    policy = parse_policy(tomllib.loads(SOLO_POLICY))
    month = parse_permission("approve:expense.report#month")
    every = parse_permission("approve:expense.report")
    assert may_grant(policy, "daniel-reyes", month)
    assert may_grant(policy, "linda-park", month)
    assert not may_grant(policy, "linda-park", every)
    assert isinstance(month, Permission)


def _decides(policy, who: str, card_type: str) -> bool:
    rule = expense_jobs.CARD_AUTHORITY[card_type]
    params = {"person": "daniel-reyes", "total_cents": "4200", "month": "2026-10"}
    return evaluate(policy, card_request(rule, who, params, policy)).allowed


def test_the_approver_decides_the_month_and_only_the_month():
    policy = parse_policy(tomllib.loads(SOLO_POLICY))
    assert _decides(policy, "linda-park", REVIEW_CARD)
    assert not _decides(policy, "linda-park", DRAFT_CARD)
    assert not _decides(policy, "linda-park", "expenses.attribute_receipt")


def test_the_missionary_can_still_approve_her_own_month_and_it_is_reported():
    policy = parse_policy(tomllib.loads(SOLO_POLICY))
    rule = expense_jobs.CARD_AUTHORITY[REVIEW_CARD]
    params = {"person": "daniel-reyes", "total_cents": "4200", "month": "2026-10"}
    out = evaluate(policy, card_request(rule, "daniel-reyes", params, policy))
    assert out.allowed and out.report


def test_a_forbid_for_one_kind_leaves_the_others():
    text = SOLO_POLICY.replace(
        'grants = ["*:*"]', 'grants = ["*:*"]\nforbids = ["approve:expense.report#month"]'
    )
    policy = parse_policy(tomllib.loads(text))
    assert not _decides(policy, "daniel-reyes", REVIEW_CARD)
    assert _decides(policy, "daniel-reyes", DRAFT_CARD)


def test_a_request_with_no_kind_is_not_the_month():
    policy = parse_policy(tomllib.loads(SOLO_POLICY))
    req = Request("linda-park", "approve", "expense.report", scope="daniel-reyes")
    assert not evaluate(policy, req).allowed


# ---- setup asks it ------------------------------------------------------------------


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "tenants"
    monkeypatch.setenv("ENGINE_TENANTS_ROOT", str(root))
    monkeypatch.setenv("ENGINE_LEDGER_ROOT", str(tmp_path / "ledger"))
    return root


SOLO = {
    "who": "nonprofit-solo",
    "legal_name": "Living Water Mozambique, Inc.",
    "your_name": "Daniel Reyes",
    "reviewer": "Linda Park, linda@example.org",
    "cash_on_hand": "2,000",
}


def _walk(values: dict) -> tuple[dict, list[str]]:
    answers: dict = {}
    asked: list[str] = []
    while (q := next_question(answers)) is not None:
        asked.append(q["id"])
        answers = record(answers, q["id"], values.get(q["id"], q.get("default", "")))
    return answers, asked


def _authority(root, slug: str, values: dict) -> dict:
    answers, _ = _walk(values)
    directory = apply(plan(answers, slug), root=root, run_audit=False).tenant_dir
    return tomllib.loads((directory / "authority.toml").read_text(encoding="utf-8"))


def test_setup_asks_only_a_solo_ministry_that_named_someone():
    _, asked = _walk(SOLO)
    assert "reviewer_approves" in asked
    _, alone = _walk({**SOLO, "reviewer": ""})
    assert "reviewer_approves" not in alone
    [q] = [q for q in load_questions() if q.id == "reviewer_approves"]
    assert q.applies_to("nonprofit-solo")
    assert not q.applies_to("nonprofit-small")
    assert not q.applies_to("commercial-solo")


def test_the_default_keeps_the_reviewer_looking_it_over(world):
    data = _authority(world, "living-water", SOLO)
    assert data["roles"]["reviewer"]["grants"] == ["view:*", "review:*"]
    assert data["safeguards"]["report_self_approvals"] == "never"


def test_approve_each_month_gives_the_month_and_tells_them(world):
    data = _authority(world, "living-water", {**SOLO, "reviewer_approves": "Approve each month"})
    assert data["roles"]["reviewer"]["grants"] == [
        "view:*",
        "review:*",
        "approve:expense.report#month",
    ]
    assert data["safeguards"]["report_self_approvals"] == "always"
    assert data["safeguards"]["self_approval"] == "allowed"
    assert data["roles"]["missionary"]["grants"] == ["*:*"]


# ---- the month page: the approver's button, and the missionary's own yes -----------


def _approver(solo) -> dict:  # noqa: F811
    """Living Water, set up as if Daniel had said Linda approves each month."""
    from core.onboarding import _month_approver

    _month_approver(solo["root"] / "living-water" / "authority.toml")
    return solo


def _finish(books: dict, desk) -> None:
    from core.tools.decide import Finisher

    desk.tap()  # the Face ID on the yes page
    Finisher(
        "living-water", tenants_root=books["root"], ledger_root=books["ledger_root"], desk=desk
    ).run_once()


def _september(books: dict) -> dict:
    from tests.unit.test_month_page import _card

    return _card(books, "living-water", "card-big")


def _show(page, desk, reviewer: str) -> str:
    status, html = page.show({"grant": desk.grant(reviewer, "daniel-reyes", "2026-09")})
    assert status == 200, html
    return html


def test_linda_approves_daniels_month_with_her_face_id(solo):  # noqa: F811
    from tests.unit.test_month_page import MonthDesk, _approve_form, _page

    books = _approver(solo)
    desk = MonthDesk()
    page = _page("living-water", books, desk)
    html = _show(page, desk, "linda-park")
    assert "<button>Approve September</button>" in html
    status, _where = page.approve(_approve_form(html))[:2]
    assert status == 303
    [asked] = desk.asked
    assert asked["person"] == "linda-park"


def test_daniel_approves_without_linda_and_she_sees_he_did(solo):  # noqa: F811
    from tests.unit.test_month_page import MonthDesk, _approve_form, _page

    books = _approver(solo)
    desk = MonthDesk()
    page = _page("living-water", books, desk)
    page.approve(_approve_form(_show(page, desk, "daniel-reyes")))
    _finish(books, desk)
    card = _september(books)
    assert card["status"] == "approved"
    stamp = card["params"]
    assert stamp["decided_by"] == "daniel-reyes" and stamp["self_approved"] == "true"
    html = _show(page, desk, "linda-park")
    assert "Daniel approved September without you" in html
    assert "/month/approve" not in html


def test_where_the_reviewer_only_looks_it_over_nothing_is_said(solo):  # noqa: F811
    from tests.unit.test_month_page import MonthDesk, _approve_form, _page

    desk = MonthDesk()
    page = _page("living-water", solo, desk)
    page.approve(_approve_form(_show(page, desk, "daniel-reyes")))
    _finish(solo, desk)
    assert _september(solo)["status"] == "approved"
    assert "without you" not in _show(page, desk, "linda-park")
