"""policy/pay: a board sets a missionary's pay by one resolution, and the
missionary's family abstains (c34, the sender model, the owner's go of
2026-11-14).

Grace Fellowship sends Ruth Hollis. Her sister Linda sits on the board
beside Don Pruitt. Tim drafts the resolution the board asked for, as words
and as one plan rule, and raises one card. Ruth never adopts her own pay,
and neither does Linda: authority.toml says they are family. Don, with no
family tie, says yes, and the pay is in force from its month through that
December. Nothing is paid here; Tim never moves money.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from core.agents.policy.jobs import PLAN_CARD, pay_in_force
from core.engine.authority_gate import decide_card, load_policy
from core.engine.runner import resolve_ledger_root, run
from core.ledger import Ledger
from core.onboarding import apply, next_question, plan, record


@pytest.fixture
def church(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "tenants"
    monkeypatch.setenv("ENGINE_TENANTS_ROOT", str(root))
    monkeypatch.setenv("ENGINE_LEDGER_ROOT", str(tmp_path / "ledger"))
    values = {
        "who": "nonprofit-small",
        "legal_name": "Grace Fellowship",
        "your_name": "Carol Jennings",
        "your_role": "treasurer",
        "people": "Ruth Hollis = missionary\nDon Pruitt = board\nLinda Hollis = board",
    }
    answers: dict = {}
    while (q := next_question(answers)) is not None:
        answers = record(answers, q["id"], values.get(q["id"], q.get("default", "")))
    apply(plan(answers, "grace"), root=root, run_audit=False)
    authority = root / "grace" / "authority.toml"
    text = authority.read_text(encoding="utf-8")
    authority.write_text(
        text.replace(
            "[people.linda-hollis]\n", '[people.linda-hollis]\nfamily = ["ruth-hollis"]\n'
        ),
        encoding="utf-8",
    )
    return {"root": root, "dir": root / "grace"}


def _pay(church, **params):
    said = {"person": "don-pruitt", "for": "Ruth Hollis", "yearly": "24000", "day": "1"}
    said.update({"from": "2027-01"}, **params)
    return run("grace", "policy", "pay", params=said, tenants_root=church["root"])


def _propose(church):
    return run("grace", "policy", "propose", tenants_root=church["root"])


def _cards():
    with Ledger.open(resolve_ledger_root("grace")) as ledger:
        rows = ledger.conn.execute(
            "SELECT * FROM approval_queue WHERE action_type = ? ORDER BY id", (PLAN_CARD,)
        ).fetchall()
    return [{**dict(r), "params": json.loads(r["params_json"])} for r in rows]


def _pay_card():
    [card] = [c for c in _cards() if c["params"].get("kin") == "yes"]
    return card


def _decide(church, who, row):
    policy = load_policy("grace", tenants_root=church["root"]).policy
    return decide_card(
        policy,
        who,
        row,
        decision="approved",
        at_terminal=True,
        delegations=[],
        today=date(2026, 11, 14),
        now="2026-11-14T15:00:00+00:00",
    )


def _set(card_id, status):
    with Ledger.open(resolve_ledger_root("grace")) as ledger:
        ledger.conn.execute("UPDATE approval_queue SET status = ? WHERE id = ?", (status, card_id))
        ledger.conn.commit()


def _in_force(church, month):
    authority = load_policy("grace", tenants_root=church["root"])
    with Ledger.open(resolve_ledger_root("grace")) as ledger:
        return pay_in_force(ledger.conn, "grace", authority.rules, "ruth-hollis", month)


def test_tim_drafts_the_resolution_and_raises_one_card(church):
    result = _pay(church)
    assert result.status == "ok", result.summary
    folder: Path = church["dir"] / "rules"
    words = (folder / "pay-ruth-hollis-2027-01.txt").read_text(encoding="utf-8")
    assert "Ruth Hollis" in words and "$24,000 a year" in words and "abstains" in words
    assert (folder / "pay-ruth-hollis-2027-01.toml").is_file()
    assert _propose(church).status == "needs_approval"
    card = _pay_card()["params"]
    assert card["covers"] == "ruth-hollis" and card["adopted_by"] == "board"
    assert "$2,000 on the 1st of each month" in card["lines"]
    assert "no family tie" in card["stays"]


def test_nothing_is_in_force_until_the_yes(church):
    _pay(church)
    _propose(church)
    assert _in_force(church, "2027-01") is None


def test_ruth_never_adopts_her_own_pay(church):
    _pay(church)
    _propose(church)
    decision = _decide(church, "ruth-hollis", _pay_card())
    assert decision.refusal
    assert "covers" in decision.refusal


def test_linda_is_family_and_abstains(church):
    _pay(church)
    _propose(church)
    decision = _decide(church, "linda-hollis", _pay_card())
    assert decision.refusal
    assert "Linda Hollis is family of Ruth Hollis" in decision.refusal


def test_don_says_yes_and_the_pay_is_in_force_through_december(church):
    _pay(church)
    _propose(church)
    card = _pay_card()
    assert not _decide(church, "don-pruitt", card).refusal
    _set(card["id"], "approved")
    pay = _in_force(church, "2027-03")
    assert pay is not None and pay.cents == 200000 and pay.day == 1
    assert _in_force(church, "2026-12") is None
    assert _in_force(church, "2028-01") is None


def test_linda_still_adopts_the_accountable_plan(church):
    """Family abstains only from pay: the accountable plan is unchanged."""
    _propose(church)
    [plan_card] = [c for c in _cards() if c["params"].get("kin") != "yes"]
    assert not _decide(church, "linda-hollis", plan_card).refusal


def test_drafting_the_same_resolution_twice_is_one_resolution(church):
    _pay(church)
    again = _pay(church)
    assert again.status in ("ok", "replayed")
    _propose(church)
    assert len([c for c in _cards() if c["params"].get("kin") == "yes"]) == 1


@pytest.mark.parametrize(
    ("params", "said"),
    [
        ({"person": "ruth-hollis"}, "only who decides the rules sets pay"),
        ({"for": "Nobody Here"}, "is not a person"),
        ({"day": "31"}, "1 to 28"),
        ({"yearly": "lots"}, "an amount"),
        ({"from": "next year"}, "a month, like 2027-01"),
    ],
)
def test_a_pay_that_cannot_be_set_is_refused_and_nothing_is_written(church, params, said):
    result = _pay(church, **params)
    assert result.status == "error"
    assert said in result.summary
    assert not list((church["dir"] / "rules").glob("pay-*"))
