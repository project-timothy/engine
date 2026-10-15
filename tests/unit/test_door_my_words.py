"""A solo missionary's own words name his own cards (the owner's decision of
2026-10-12): "approve my report" said by the person the card is about names
it, the same as saying his own name, and a bill is named by whom it pays,
the way people say it ("Maputo Water", not "Maputo Water Co.").

Daniel Reyes runs Living Water Mozambique alone; a solo tenant approves its
own, under a $250 line for a yes by word. Linda Park, his board treasurer,
reviews the month and decides nothing."""

from __future__ import annotations

import re

import pytest

from core.agents.expenses.jobs import REVIEW_CARD
from core.engine.runner import resolve_ledger_root
from core.ledger import Ledger
from core.onboarding import apply, next_question, plan, record
from core.tools import catalog
from core.tools.decide import AskBook, Decider, card_words
from core.tools.viewer import viewer_for
from tests.unit.test_door_decide import FakeDesk

PAY_CARD = "ap.payment_recommendation"


@pytest.fixture
def solo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "tenants"
    monkeypatch.setenv("ENGINE_TENANTS_ROOT", str(root))
    monkeypatch.setenv("ENGINE_LEDGER_ROOT", str(tmp_path / "ledger"))
    values = {
        "who": "nonprofit-solo",
        "legal_name": "Living Water Mozambique, Inc.",
        "your_name": "Daniel Reyes",
        "reviewer": "Linda Park, linda@example.org",
    }
    answers: dict = {}
    while (q := next_question(answers)) is not None:
        answers = record(answers, q["id"], values.get(q["id"], q.get("default", "")))
    apply(plan(answers, "living-water"), root=root, run_audit=False)
    authority = root / "living-water" / "authority.toml"
    authority.write_text(
        authority.read_text() + '\n[presence]\ndoor = ["expense.report", "payment"]\n'
    )
    assert re.search(r"(?m)^yes_by_word_up_to = 250$", authority.read_text())
    ledger_root = resolve_ledger_root("living-water")
    cards = (
        (
            "card-small",
            "expenses",
            REVIEW_CARD,
            {"person": "Daniel Reyes", "month": "2026-08", "total_cents": "18640"},
        ),
        (
            "card-big",
            "expenses",
            REVIEW_CARD,
            {"person": "Daniel Reyes", "month": "2026-09", "total_cents": "64210"},
        ),
        (
            "card-water",
            "ap",
            PAY_CARD,
            {"payee": "Maputo Water Co.", "invoice_ref": "W-9", "amount": "180.00"},
        ),
    )
    with Ledger.open(ledger_root) as ledger:
        ledger.conn.execute(
            "INSERT INTO runs (idempotency_key, tenant, agent, job, status, result_json, "
            "created_at) VALUES ('seed-run', 'living-water', 'expenses', 'review', 'ok', '{}', "
            "'now')"
        )
        run_id = ledger.conn.execute("SELECT MAX(id) FROM runs").fetchone()[0]
        ledger.conn.commit()
        for key, agent, action, params in cards:
            ledger.enqueue_approval(
                idempotency_key=key,
                run_id=run_id,
                tenant="living-water",
                agent=agent,
                action_type=action,
                params=params,
            )
    return {"root": root, "ledger_root": ledger_root, "desk": FakeDesk()}


def _card(solo, key: str) -> dict:
    with Ledger.open(solo["ledger_root"]) as ledger:
        return next(r for r in ledger.list_approvals("living-water") if r["idempotency_key"] == key)


def _says(solo, person: str, key: str, words: str, decision: str = "approve") -> dict:
    tools = catalog.Tools(
        "living-water",
        ledger_root=solo["ledger_root"],
        obligations_file=solo["root"] / "none.toml",
        viewer=viewer_for("living-water", person, tenants_root=solo["root"]),
        decider=Decider(
            "living-water",
            tenants_root=solo["root"],
            ledger_root=solo["ledger_root"],
            person=person,
            desk=solo["desk"],
            asks=AskBook(),
        ),
    )
    card = _card(solo, key)["id"]
    return tools.call("decide_card", {"card": card, "decision": decision, "said": words})


@pytest.mark.parametrize(
    "words",
    ["approve my August report", "Approve my august expenses", "yes, approve mine for August"],
)
def test_daniels_my_names_his_own_card_and_his_word_decides_it(solo, words):
    said = _says(solo, "daniel-reyes", "card-small", words)
    assert said["status"] == "approved" and solo["desk"].asked == []
    row = _card(solo, "card-small")
    assert row["params"]["decided_via"] == "door-word"
    assert row["params"]["said"] == words


def test_over_his_line_my_report_still_takes_his_face_id(solo):
    said = _says(solo, "daniel-reyes", "card-big", "approve my report")
    assert said["status"] == "tap_to_confirm" and len(solo["desk"].asked) == 1


@pytest.mark.parametrize(
    "words",
    ["pay Maputo Water", "approve the Maputo Water Co. bill", "the $180 one is fine"],
)
def test_a_bill_is_named_by_whom_it_pays(solo, words):
    said = _says(solo, "daniel-reyes", "card-water", words)
    assert said["status"] == "approved" and solo["desk"].asked == []


@pytest.mark.parametrize("words", ["approve my bill", "pay the water bill", "approve them all"])
def test_words_that_do_not_name_the_bill_take_his_face_id(solo, words):
    said = _says(solo, "daniel-reyes", "card-water", words)
    assert said["status"] == "tap_to_confirm"
    assert _card(solo, "card-water")["status"] == "pending"


def test_linda_reviews_the_month_and_decides_nothing(solo):
    with pytest.raises(ValueError):
        _says(solo, "linda-park", "card-small", "approve my report")
    assert _card(solo, "card-small")["status"] == "pending"


def test_a_bill_reads_as_whom_it_pays(solo):
    assert card_words(_card(solo, "card-water")) == "a payment to Maputo Water Co., $180.00"


def test_a_report_reads_with_its_month_so_two_of_his_are_told_apart():
    # Variance 2026-10-12: with two of Daniel's reports waiting, "approve my
    # August report" could not be matched (both read "an expense report for
    # Daniel Reyes"), so every model asked which. The card carries its month.
    row = {
        "agent": "expenses",
        "action_type": REVIEW_CARD,
        "params": {"person": "Daniel Reyes", "month": "2026-08", "total_cents": "18640"},
    }
    assert card_words(row) == "an expense report for Daniel Reyes, August 2026, $186.40"
    row["params"]["month"] = "not a month"
    assert card_words(row) == "an expense report for Daniel Reyes, $186.40"


# ---- two of his waiting: the words must say which (variance 2026-10-12) ----
# GPT-5.4-mini, told "approve my September report", approved August's
# $186.40 by word: "my" named both of Daniel's reports. When the words fit
# more than one waiting card, they also say its month or its amount.


@pytest.mark.parametrize("words", ["approve my report", "approve Daniel's report", "approve mine"])
def test_words_that_fit_both_of_his_reports_take_his_face_id(solo, words):
    said = _says(solo, "daniel-reyes", "card-small", words)
    assert said["status"] == "tap_to_confirm"
    assert _card(solo, "card-small")["status"] == "pending"


@pytest.mark.parametrize(
    "words", ["approve my August report", "august is fine, approve mine", "the $186.40 one"]
)
def test_the_month_or_the_amount_says_which(solo, words):
    said = _says(solo, "daniel-reyes", "card-small", words)
    assert said["status"] == "approved"


def test_the_wrong_month_never_decides_the_other_card(solo):
    said = _says(solo, "daniel-reyes", "card-small", "approve my September report")
    assert said["status"] == "tap_to_confirm"
    assert _card(solo, "card-small")["status"] == "pending"


def test_once_the_other_is_decided_my_report_is_enough_again(solo):
    _says(solo, "daniel-reyes", "card-big", "the $642.10 one")  # over the line: a link
    with Ledger.open(solo["ledger_root"]) as ledger:
        ledger.conn.execute(
            "UPDATE approval_queue SET status = 'approved' WHERE idempotency_key = 'card-big'"
        )
        ledger.conn.commit()
    said = _says(solo, "daniel-reyes", "card-small", "approve my report")
    assert said["status"] == "approved"
