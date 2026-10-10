"""Human-only cards (#356 check 2): an agent can no longer decide them.

Agents decide approval cards since 2026-09-15, so a card is a human check
only when the card says so. ``ap.new_vendor_decision`` is the first one: the
queue CLI refuses to approve or reject it unless a person is at a terminal
and types the card number back. Every headless lane and every agent shell
runs without a terminal on stdin, so the honest lanes stop here; the
decision records ``decided_via=terminal`` so the auditor can tell a card a
person decided from one an agent decided.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.engine import cli
from core.engine.cli import main
from core.engine.runner import resolve_ledger_root
from core.ledger import Ledger

# Today's (no authority.toml) path; #435 adds the other.
pytestmark = pytest.mark.usefixtures("demo_without_authority")

LANDING = Path(__file__).resolve().parents[2] / "core/agents/ap/evals/fixtures/landing"


def _seed(tmp_path) -> None:
    code = main(
        [
            "run", "demo", "ap", "intake", "--shadow", "--ledger-dir", str(tmp_path),
            "--param", f"landing_dir={LANDING}", "--param", "extractor=fixture",
        ]
    )  # fmt: skip
    assert code == 0


def _card(tmp_path, action_type: str) -> dict:
    with Ledger.open(resolve_ledger_root("demo", tmp_path)) as ledger:
        (row,) = [r for r in ledger.list_approvals("demo") if r["action_type"] == action_type]
    return row


def _stored_params(tmp_path, card_id: int) -> dict:
    with Ledger.open(resolve_ledger_root("demo", tmp_path)) as ledger:
        row = ledger.conn.execute(
            "SELECT params_json FROM approval_queue WHERE id = ?", (card_id,)
        ).fetchone()
    return json.loads(row["params_json"])


@pytest.fixture
def at_terminal(monkeypatch):
    def _set(present: bool, typed: str = "") -> None:
        monkeypatch.setattr(cli, "_operator_at_terminal", lambda: present)
        monkeypatch.setattr("builtins.input", lambda prompt="": typed)

    return _set


def test_the_new_vendor_card_says_it_is_human_only(tmp_path):
    _seed(tmp_path)
    assert _card(tmp_path, "ap.new_vendor_decision")["params"]["human_only"] == "true"


@pytest.mark.parametrize("verb", ["approve", "reject"])
def test_an_agent_shell_cannot_decide_a_human_only_card(tmp_path, capsys, at_terminal, verb):
    _seed(tmp_path)
    at_terminal(False)
    card = _card(tmp_path, "ap.new_vendor_decision")

    code = main(["queue", verb, "demo", "--id", str(card["id"]), "--ledger-dir", str(tmp_path)])

    assert code == 2
    assert "human-only" in capsys.readouterr().err
    assert _card(tmp_path, "ap.new_vendor_decision")["status"] == "pending"


def test_a_person_at_a_terminal_decides_it_and_the_card_records_how(tmp_path, at_terminal):
    _seed(tmp_path)
    card = _card(tmp_path, "ap.new_vendor_decision")
    at_terminal(True, typed=str(card["id"]))

    code = main(
        ["queue", "approve", "demo", "--id", str(card["id"]), "--ledger-dir", str(tmp_path)]
    )

    assert code == 0
    assert _card(tmp_path, "ap.new_vendor_decision")["status"] == "approved"
    assert _stored_params(tmp_path, card["id"])["decided_via"] == "terminal"


def test_the_wrong_number_typed_back_decides_nothing(tmp_path, at_terminal):
    _seed(tmp_path)
    card = _card(tmp_path, "ap.new_vendor_decision")
    at_terminal(True, typed="y")

    code = main(
        ["queue", "approve", "demo", "--id", str(card["id"]), "--ledger-dir", str(tmp_path)]
    )

    assert code == 2
    assert _card(tmp_path, "ap.new_vendor_decision")["status"] == "pending"


@pytest.mark.parametrize(
    "param",
    ["human_only=false", "decided_via=terminal", "decided_via=door", "witness={}", "witness_at=x"],
)
def test_no_one_can_write_the_gate_fields_by_hand(tmp_path, at_terminal, param):
    _seed(tmp_path)
    card = _card(tmp_path, "ap.new_vendor_decision")
    at_terminal(True, typed=str(card["id"]))

    code = main(
        ["queue", "approve", "demo", "--id", str(card["id"]), "--ledger-dir", str(tmp_path),
         "--param", param]
    )  # fmt: skip

    assert code == 2
    assert _card(tmp_path, "ap.new_vendor_decision")["status"] == "pending"


def test_an_older_unstamped_card_of_the_type_is_still_human_only(tmp_path, at_terminal):
    # Cards queued before the stamp existed are gated by their action type.
    _seed(tmp_path)
    root = resolve_ledger_root("demo", tmp_path)
    with Ledger.open(root) as ledger:
        ledger.enqueue_approval(
            idempotency_key="old:new-vendor",
            run_id=1,
            tenant="demo",
            agent="ap",
            action_type="ap.new_vendor_decision",
            params={"file": "old.pdf", "extracted_vendor": "Old Co"},
        )
        (card,) = [r for r in ledger.list_approvals("demo") if r["params"].get("file") == "old.pdf"]
    assert "human_only" not in card["params"]
    at_terminal(False)

    code = main(["queue", "reject", "demo", "--id", str(card["id"]), "--ledger-dir", str(tmp_path)])

    assert code == 2


def test_ordinary_cards_still_decide_without_a_terminal(tmp_path, at_terminal):
    _seed(tmp_path)
    at_terminal(False)
    card = _card(tmp_path, "ap.review_needs_ocr")

    code = main(
        ["queue", "approve", "demo", "--id", str(card["id"]), "--ledger-dir", str(tmp_path)]
    )

    assert code == 0
    assert "decided_via" not in _stored_params(tmp_path, card["id"])
