"""A job may declare that its new card supersedes older pending ones.

Proposal 99e8ae57 (PR #377): the runner owns all persistence, so a lane
DECLARES ``ApprovalSpec.supersedes_keys`` and only the runner closes a queue
row, as ``superseded`` (never ``approved``: an execute path that selects
approved cards moves nothing on a closed card's behalf). Fences pinned here:
same agent and action type only, pending rows only, never when the new ask
itself was swallowed, and every supersession is an event naming both cards.
"""

from __future__ import annotations

import json

from core.engine import runner as runner_mod
from core.engine.contracts import ApprovalSpec, JobHandler, JobOutput
from core.engine.runner import resolve_ledger_root, run
from core.ledger import Ledger


def _job(key, *, action="demo.skip", supersedes=()):
    return JobHandler(
        key=lambda ctx: f"run:{key}",
        run=lambda ctx: JobOutput(
            status="needs_approval",
            summary="parked",
            approvals=[
                ApprovalSpec(
                    key=key, action_type=action, params={"k": key}, supersedes_keys=list(supersedes)
                )
            ],
        ),
    )


def _park(tmp_path, monkeypatch, key, **kw):
    monkeypatch.setattr(runner_mod, "get_job", lambda agent, job: _job(key, **kw))
    return run("demo", "demo", "park", params={"k": key}, ledger_dir=tmp_path)


def _rows(tmp_path):
    with Ledger.open(resolve_ledger_root("demo", tmp_path)) as ledger:
        cards = [
            (r["params_json"], r["action_type"], r["status"], r["resolved_at"] is not None)
            for r in ledger.conn.execute("SELECT * FROM approval_queue ORDER BY id")
        ]
        events = [
            json.loads(r["payload_json"])
            for r in ledger.conn.execute(
                "SELECT payload_json FROM events WHERE event_type = 'engine.approval_superseded'"
            )
        ]
    return cards, events


def test_a_declared_supersession_closes_the_older_pending_card(tmp_path, monkeypatch):
    _park(tmp_path, monkeypatch, "small")
    _park(tmp_path, monkeypatch, "big", supersedes=("small",))
    cards, events = _rows(tmp_path)
    assert [(json.loads(p)["k"], s, done) for p, _, s, done in cards] == [
        ("small", "superseded", True),
        ("big", "pending", False),
    ]
    assert runner_mod.SUPERSEDED_STATUS == "superseded"
    (event,) = events
    assert event["action_type"] == "demo.skip"
    assert event["superseded_key"] == "small"
    assert event["by_key"] == "big"
    assert isinstance(event["superseded_id"], int)


def test_supersession_never_crosses_action_types_or_touches_answered_cards(tmp_path, monkeypatch):
    _park(tmp_path, monkeypatch, "same-key", action="demo.other")
    _park(tmp_path, monkeypatch, "answered")
    with Ledger.open(resolve_ledger_root("demo", tmp_path)) as ledger:
        answered_id = ledger.conn.execute(
            "SELECT id FROM approval_queue WHERE params_json LIKE '%answered%'"
        ).fetchone()["id"]
        ledger.resolve_approval("demo", answered_id, "approved")
    _park(tmp_path, monkeypatch, "big", supersedes=("same-key", "answered", "never-existed"))
    cards, events = _rows(tmp_path)
    assert [(json.loads(p)["k"], s) for p, _, s, _ in cards] == [
        ("same-key", "pending"),
        ("answered", "approved"),
        ("big", "pending"),
    ]
    assert events == []


def test_a_swallowed_ask_supersedes_nothing(tmp_path, monkeypatch):
    _park(tmp_path, monkeypatch, "small")
    _park(tmp_path, monkeypatch, "big")
    with Ledger.open(resolve_ledger_root("demo", tmp_path)) as ledger:
        big_id = ledger.conn.execute(
            "SELECT id FROM approval_queue WHERE params_json LIKE '%big%'"
        ).fetchone()["id"]
        ledger.resolve_approval("demo", big_id, "rejected")
    # The same "big" ask again collides with a RESOLVED card: no card holds it,
    # so it must not retire the pending "small" card either.
    _park(tmp_path, monkeypatch, "big", supersedes=("small",))
    cards, events = _rows(tmp_path)
    assert [(json.loads(p)["k"], s) for p, _, s, _ in cards] == [
        ("small", "pending"),
        ("big", "rejected"),
    ]
    assert events == []
