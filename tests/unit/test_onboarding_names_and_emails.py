"""A person's name, apart from the words and address around it (Tim
walkthrough 2, 2026-10-09).

Asked who looks over his spending, Daniel answered "Linda Park, board
treasurer, linda@livingwatermz.org", and onboarding made her person id
``linda-park-board-treasurer-linda-livingwatermz-org`` with that whole line
as her name. The name is what comes before the first comma (or a ``<``, or a
parenthesis); an email address anywhere in the answer is kept as that
person's address, for the sign-in invitation, and written beside them in
authority.toml. Two addresses in one answer, or an address with no name, is
asked again.

The person setting up may give their own address the same way ("Daniel
Reyes, daniel@livingwatermz.org", Caleb's alpha install of 2026-10-13): the
invitation at apply mails them their welcome link too, so the person the
books are for is never the one left without a way in.
"""

from __future__ import annotations

import tomllib

import pytest

from core.authority import parse_policy
from core.onboarding import OnboardingError, apply, next_question, plan, record

DANIEL = {
    "who": "nonprofit-solo",
    "legal_name": "Living Water Mozambique, Inc.",
    "your_name": "Daniel Reyes",
    "timezone": "Africa/Maputo",
    "fiscal_start": "7",
}


def _answers(**over):
    values = {**DANIEL, **over}
    answers: dict = {}
    while (q := next_question(answers)) is not None:
        answers = record(answers, q["id"], values.get(q["id"], q.get("default", "")))
    return answers


@pytest.mark.parametrize(
    "said",
    [
        "Linda Park, board treasurer, linda@livingwatermz.org",
        "Linda Park <linda@livingwatermz.org>",
        "Linda Park (board treasurer) linda@livingwatermz.org",
        "linda@livingwatermz.org, Linda Park",
    ],
)
def test_the_reviewers_name_and_address_come_apart(said):
    p = plan(_answers(reviewer=said), "living-water")
    assert ("linda-park", "Linda Park", "reviewer") in p.people
    assert p.emails == {"linda-park": "linda@livingwatermz.org"}


def test_a_name_alone_is_a_name_with_no_address():
    p = plan(_answers(reviewer="Linda Park, board treasurer"), "living-water")
    assert ("linda-park", "Linda Park", "reviewer") in p.people
    assert p.emails == {}


def test_an_address_with_no_name_is_asked_again():
    with pytest.raises(OnboardingError, match="name"):
        record(_answers(), "reviewer", "linda@livingwatermz.org")


def test_two_addresses_in_one_answer_are_asked_again():
    with pytest.raises(OnboardingError, match="one email"):
        record(_answers(), "reviewer", "Linda Park, linda@a.org, linda@b.org")


def test_a_blank_reviewer_is_still_fine():
    p = plan(_answers(reviewer=""), "living-water")
    assert [pid for pid, _n, _r in p.people] == ["daniel-reyes"]


def test_answers_saved_before_this_change_still_plan():
    answers = _answers()
    answers["reviewer"] = (
        "Linda Park, board treasurer, linda@livingwatermz.org"  # an old raw string
    )
    p = plan(answers, "living-water")
    assert ("linda-park", "Linda Park", "reviewer") in p.people


def test_a_church_member_line_may_carry_an_address():
    answers: dict = {}
    values = {
        "who": "nonprofit-small",
        "legal_name": "Grace Fellowship",
        "your_name": "Carol Jennings",
        "your_role": "treasurer",
        "people": "Ruth Hollis = missionary in Nepal, ruth@example.org\nDon Pruitt = board",
    }
    while (q := next_question(answers)) is not None:
        answers = record(answers, q["id"], values.get(q["id"], q.get("default", "")))
    p = plan(answers, "grace")
    assert ("ruth-hollis", "Ruth Hollis", "missionary") in p.people
    assert p.emails == {"ruth-hollis": "ruth@example.org"}


def test_apply_writes_each_address_beside_its_person(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "tenants"
    monkeypatch.setenv("ENGINE_TENANTS_ROOT", str(root))
    monkeypatch.setenv("ENGINE_LEDGER_ROOT", str(tmp_path / "ledger"))
    p = plan(_answers(reviewer="Linda Park, board treasurer, linda@livingwatermz.org"), "lw")
    result = apply(p, root=root, run_audit=False)
    data = tomllib.loads((result.tenant_dir / "authority.toml").read_text())
    assert data["people"]["linda-park"]["email"] == "linda@livingwatermz.org"
    assert "email" not in data["people"]["daniel-reyes"]
    assert set(parse_policy(data).people) == {"daniel-reyes", "linda-park"}


def test_the_person_setting_up_may_give_their_own_address():
    p = plan(
        _answers(
            your_name="Daniel Reyes, daniel@livingwatermz.org",
            reviewer="Linda Park, linda@livingwatermz.org",
        ),
        "living-water",
    )
    assert ("daniel-reyes", "Daniel Reyes", "missionary") in p.people
    assert p.emails == {
        "daniel-reyes": "daniel@livingwatermz.org",
        "linda-park": "linda@livingwatermz.org",
    }


def test_the_person_setting_up_still_needs_a_name():
    answers = record({}, "who", "nonprofit-solo")
    with pytest.raises(OnboardingError, match="needs an answer"):
        record(answers, "your_name", "")
    with pytest.raises(OnboardingError, match="name"):
        record(answers, "your_name", "daniel@livingwatermz.org")


def test_a_name_saved_before_the_address_was_asked_still_plans():
    answers = _answers()
    assert answers["your_name"] == "Daniel Reyes"
    p = plan(answers, "living-water")
    assert ("daniel-reyes", "Daniel Reyes", "missionary") in p.people
    assert "daniel-reyes" not in p.emails


def test_their_own_address_lands_beside_them(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "tenants"
    monkeypatch.setenv("ENGINE_TENANTS_ROOT", str(root))
    monkeypatch.setenv("ENGINE_LEDGER_ROOT", str(tmp_path / "ledger"))
    p = plan(_answers(your_name="Daniel Reyes <daniel@livingwatermz.org>"), "lw")
    result = apply(p, root=root, run_audit=False)
    data = tomllib.loads((result.tenant_dir / "authority.toml").read_text())
    assert data["people"]["daniel-reyes"]["email"] == "daniel@livingwatermz.org"
