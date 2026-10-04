"""Inbox classifier pre-stage evals (issue #112).

The Taildrop watcher sweeps every camera image, so the inbox holds receipts
and personal strays side by side. Contract under test:
- Read-and-propose: nothing moves without an approved card.
- LABEL-ONLY (owner decision 2026-08-12, code-enforced): a non-receipt's
  card and event carry a filename and a confidence, never content — even
  when a (misbehaving) classifier returns vendor/amount for it.
- Approved receipt with a project files into the drop tree and flows into
  normal intake; without a project it lands at the person ROOT and the
  existing attribution card takes over (card-never-guess).
- Approved skips move to _not-receipts/ — kept, never deleted.
- One LLM look per image ever: classification is event-memory.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from core.engine.runner import resolve_ledger_root, run
from core.ledger import Ledger

PERSON = "Pat Owner"


def _params(tmp_path, **extra):
    return {
        "inbox_dir": str(tmp_path / "inbox"),
        "inbox_person": PERSON,
        "drop_dir": str(tmp_path / "drop"),
        "filing_dir": str(tmp_path / "filing"),
        "classifier": "fixture",
        "extractor": "fixture",
        **extra,
    }


def _run_inbox(tmp_path, **extra):
    return run(
        "demo",
        "expenses",
        "inbox",
        params=_params(tmp_path, **extra),
        ledger_dir=tmp_path / "ledger",
    )


def _seed(tmp_path, name, body, label=None):
    inbox = tmp_path / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / name).write_bytes(body)
    if label is not None:
        (inbox / (name + ".label.json")).write_text(json.dumps(label))
    return hashlib.sha256(body).hexdigest()


def _ledger(tmp_path):
    return Ledger.open(resolve_ledger_root("demo", tmp_path / "ledger"))


def _pending(tmp_path, action_type):
    with _ledger(tmp_path) as ledger:
        return [
            c
            for c in ledger.list_approvals("demo", status="pending")
            if c["action_type"] == action_type
        ]


def _approve(tmp_path, card_id, overrides=None):
    with _ledger(tmp_path) as ledger:
        ledger.resolve_approval("demo", card_id, "approved", param_overrides=overrides or {})


def test_receipt_proposes_a_filing_card_and_nothing_moves(tmp_path):
    _seed(
        tmp_path,
        "IMG_1001.jpg",
        b"receipt-bytes",
        {
            "receipt": True,
            "confidence": 0.9,
            "vendor": "Roadside Coffee",
            "amount": "12.34",
            "expense_date": "2026-08-20",
        },
    )
    result = _run_inbox(tmp_path)

    assert result.status == "needs_approval"
    (card,) = _pending(tmp_path, "expenses.inbox_file_receipt")
    assert card["params"]["file"] == "IMG_1001.jpg"
    assert card["params"]["person"] == PERSON
    assert card["params"]["vendor"] == "Roadside Coffee"
    assert (tmp_path / "inbox" / "IMG_1001.jpg").is_file()  # proposal only


def test_non_receipt_is_label_only_even_from_a_misbehaving_classifier(tmp_path):
    """The invariant: the sidecar (a stand-in for a model that ignores the
    prompt) returns vendor/amount for a personal photo — code strips them.
    The card and the event carry the filename and confidence, nothing else."""
    _seed(
        tmp_path,
        "IMG_2002.jpg",
        b"beach-photo",
        {
            "receipt": False,
            "confidence": 0.2,
            "vendor": "LEAKED NAME",
            "amount": "999.99",
            "expense_date": "2026-01-01",
        },
    )
    _run_inbox(tmp_path)

    (card,) = _pending(tmp_path, "expenses.inbox_skip")
    assert card["params"]["files"] == "IMG_2002.jpg"
    joined = json.dumps(card["params"])
    assert "LEAKED" not in joined and "999" not in joined
    with _ledger(tmp_path) as ledger:
        events = [
            e for e in ledger.read_event_log() if e["event_type"] == "expense.inbox_classified"
        ]
    (event,) = events
    assert "vendor" not in event["payload"]
    assert "LEAKED" not in json.dumps(event["payload"])


def test_approved_receipt_files_into_the_drop_tree_and_intake_takes_it(tmp_path):
    _seed(
        tmp_path,
        "IMG_3003.jpg",
        b"lunch-receipt",
        {
            "receipt": True,
            "confidence": 0.95,
            "vendor": "Cafe",
            "amount": "9.00",
            "expense_date": "2026-08-19",
        },
    )
    _run_inbox(tmp_path)
    (card,) = _pending(tmp_path, "expenses.inbox_file_receipt")
    _approve(tmp_path, card["id"], {"project": "P26_2001"})

    again = _run_inbox(tmp_path)

    assert again.status in ("ok", "needs_approval")
    dest = tmp_path / "drop" / PERSON / "P26_2001" / "IMG_3003.jpg"
    assert dest.is_file()
    assert not (tmp_path / "inbox" / "IMG_3003.jpg").exists()
    intake = run(
        "demo", "expenses", "intake", params=_params(tmp_path), ledger_dir=tmp_path / "ledger"
    )
    assert "filed 1" in intake.summary


def test_approved_receipt_without_project_lands_at_person_root(tmp_path):
    _seed(tmp_path, "IMG_4004.jpg", b"mystery-receipt", {"receipt": True, "confidence": 0.7})
    _run_inbox(tmp_path)
    (card,) = _pending(tmp_path, "expenses.inbox_file_receipt")
    _approve(tmp_path, card["id"])

    _run_inbox(tmp_path)

    assert (tmp_path / "drop" / PERSON / "IMG_4004.jpg").is_file()
    intake = run(
        "demo", "expenses", "intake", params=_params(tmp_path), ledger_dir=tmp_path / "ledger"
    )
    cards = [a for a in intake.approvals_needed if a.action_type == "expenses.attribute_receipt"]
    assert len(cards) == 1  # normal attribution takes over, never a guess


def test_approved_skips_move_to_not_receipts_never_deleted(tmp_path):
    _seed(tmp_path, "IMG_5005.jpg", b"dog-photo", {"receipt": False, "confidence": 0.1})
    _seed(tmp_path, "IMG_5006.jpg", b"cat-photo", {"receipt": False, "confidence": 0.1})
    _run_inbox(tmp_path)
    (card,) = _pending(tmp_path, "expenses.inbox_skip")
    assert card["params"]["count"] == "2"
    _approve(tmp_path, card["id"])

    _run_inbox(tmp_path)

    kept = tmp_path / "inbox" / "_not-receipts"
    assert (kept / "IMG_5005.jpg").is_file()
    assert (kept / "IMG_5006.jpg").is_file()


def test_classification_is_event_memory_one_look_ever(tmp_path):
    _seed(
        tmp_path,
        "IMG_6006.jpg",
        b"receipt-x",
        {
            "receipt": True,
            "confidence": 0.9,
            "vendor": "V",
            "amount": "1.00",
            "expense_date": "2026-08-01",
        },
    )
    _run_inbox(tmp_path)
    first = _run_inbox(tmp_path)
    assert first.status == "noop"  # unchanged inbox replays

    # a NEW file re-fires the run; the old sha is remembered, not re-classified
    _seed(
        tmp_path,
        "IMG_7007.jpg",
        b"receipt-y",
        {
            "receipt": True,
            "confidence": 0.9,
            "vendor": "W",
            "amount": "2.00",
            "expense_date": "2026-08-02",
        },
    )
    _run_inbox(tmp_path)
    with _ledger(tmp_path) as ledger:
        events = [
            e for e in ledger.read_event_log() if e["event_type"] == "expense.inbox_classified"
        ]
    assert len(events) == 2  # one per image, ever


@pytest.mark.parametrize(
    "erase_record", [False, True], ids=["healed-from-record", "no-record-anomaly"]
)
def test_moved_inbox_file_with_no_event_is_recorded_on_the_next_run(
    tmp_path, monkeypatch, erase_record
):
    """Honesty audit 2026-09-03, 03-F9 (S3), closed 2026-09-10. A run that
    dies between the move and the event leaves the receipt in the drop tree
    with no expense.inbox_filed event. Since record-then-move, the filer
    writes a durable job record BEFORE the move, so the next run heals the
    event from the record (an action). With no record either (the
    pre-record shape, or a hand move), the file found where the card said,
    hash-verified, is recorded with an anomaly naming the gap; never read as
    "already filed"."""
    import shutil

    from core.agents.expenses import jobs as exp_jobs

    sha = _seed(tmp_path, "IMG_8008.jpg", b"lunch-receipt", {"receipt": True, "confidence": 0.9})
    _run_inbox(tmp_path)
    (card,) = _pending(tmp_path, "expenses.inbox_file_receipt")
    _approve(tmp_path, card["id"], {"project": "P26_2001"})
    real_move = shutil.move

    def move_then_die(src, dst):
        real_move(src, dst)
        raise RuntimeError("died after the move")

    monkeypatch.setattr(exp_jobs.shutil, "move", move_then_die)
    crashed = _run_inbox(tmp_path)
    assert crashed.status == "error"
    dest = tmp_path / "drop" / PERSON / "P26_2001" / "IMG_8008.jpg"
    assert dest.is_file()
    with _ledger(tmp_path) as ledger:
        assert [
            e for e in ledger.read_event_log() if e["event_type"] == "expense.inbox_filed"
        ] == []

    monkeypatch.setattr(exp_jobs.shutil, "move", real_move)
    if erase_record:
        # The pre-record shape (a run from before 2026-09-10, or a hand move):
        # nothing but the file itself says what happened.
        with _ledger(tmp_path) as ledger:
            ledger.conn.execute("DELETE FROM job_records")
            ledger.conn.commit()
    result = _run_inbox(tmp_path)

    assert result.status == "ok"
    healed = [a for a in result.actions if "from its job record" in a]
    unrecorded = [a for a in result.anomalies if a.code == "expenses.inbox_filing_unrecorded"]
    if erase_record:
        assert unrecorded and not healed, "no record, no event: an anomaly, never silence"
    else:
        assert healed and not unrecorded, "record-then-move: healed with an action, no anomaly"
    assert "recorded 1 move(s) a dying run left without an event" in result.summary
    with _ledger(tmp_path) as ledger:
        (filed,) = [e for e in ledger.read_event_log() if e["event_type"] == "expense.inbox_filed"]
    assert filed["payload"]["sha256"] == sha
    assert filed["payload"]["dest"] == str(dest)
    intake = run(
        "demo", "expenses", "intake", params=_params(tmp_path), ledger_dir=tmp_path / "ledger"
    )
    assert "filed 1" in intake.summary
