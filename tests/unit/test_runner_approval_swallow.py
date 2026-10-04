"""Issue #161: the stable-subject approval dedup must never lie.

#143 made ``enqueue_approval`` INSERT OR IGNORE on a stable subject key so a
still-flagged subject re-parks nothing — the queued card (or its recorded
resolution) is the memory. Correct for pending collisions; but when a job
asks again after the card was RESOLVED (the close lock's reject-and-reask
contract), the ask was silently swallowed while the run's summary claimed a
card was parked (live incident 2026-09-01, the August close).

Two-sided contract pinned here:
- a collision with a PENDING card stays silent (the designed memory): the
  card exists, so the ask is honored and the run still needs approval;
- a collision with a RESOLVED card records an ``engine.approval_swallowed``
  event naming the existing card, so the ledger records the truth and the
  flow's missing key variation is visible instead of invisible.

Honesty audit 2026-09-03, substrate finding F4 (the summary-side residual of
the 9/1 incident): the run's status, ``approvals_needed``, and the stored
``result_json`` are rebuilt from OUTCOME after the enqueue loop. A swallowed
ask leaves ``approvals_needed`` and becomes an ``engine.approval_swallowed``
anomaly; the run is ``needs_approval`` only when a card actually holds the
ask. Before, the terminal and the stored result read "needs_approval /
approvals queued: 1" with zero cards created.
"""

from __future__ import annotations

import json

from core.engine import runner as runner_mod
from core.engine.contracts import ApprovalSpec, JobHandler, JobOutput
from core.engine.runner import resolve_ledger_root, run
from core.ledger import Ledger

_PARK = JobHandler(
    key=lambda ctx: f"park:{ctx.params.get('n')}",
    run=lambda ctx: JobOutput(
        status="needs_approval",
        summary="approval card parked",
        approvals=[
            ApprovalSpec(
                key="subject:1",
                action_type="demo.park",
                params={"n": str(ctx.params.get("n"))},
            )
        ],
    ),
)


def _swallow_events(ledger):
    rows = ledger.conn.execute(
        "SELECT payload_json FROM events WHERE event_type='engine.approval_swallowed'"
    ).fetchall()
    return [json.loads(r["payload_json"]) for r in rows]


def _card_ids(ledger, status=None):
    sql = "SELECT id, status FROM approval_queue"
    rows = ledger.conn.execute(sql).fetchall()
    return [(r["id"], r["status"]) for r in rows if status is None or r["status"] == status]


def test_collision_with_resolved_card_records_swallow_event(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_mod, "get_job", lambda agent, job: _PARK)
    first = run("demo", "demo", "park", params={"n": "1"}, ledger_dir=tmp_path)
    assert first.status == "needs_approval"

    root = resolve_ledger_root("demo", tmp_path)
    with Ledger.open(root) as ledger:
        (card_id, _status) = _card_ids(ledger, status="pending")[0]
        ledger.resolve_approval("demo", card_id, "rejected")

    second = run("demo", "demo", "park", params={"n": "2"}, ledger_dir=tmp_path)
    # Outcome, not intent: no card holds the ask, so the run does NOT need
    # approval, lists no approval, and carries the swallow as an anomaly.
    assert second.status == "ok"
    assert second.approvals_needed == []
    (anomaly,) = [a for a in second.anomalies if a.code == "engine.approval_swallowed"]
    assert "demo.park" in anomaly.detail
    assert f"#{card_id}" in anomaly.detail
    assert "rejected" in anomaly.detail

    with Ledger.open(root) as ledger:
        # Nothing new was parked (the key collided) …
        assert _card_ids(ledger, status="pending") == []
        # … and the swallow is ON THE RECORD instead of silent.
        swallowed = _swallow_events(ledger)
        # The stored result (what a replay shows) says the same thing.
        row = ledger.conn.execute(
            "SELECT status, result_json FROM runs WHERE idempotency_key = ?",
            (second.idempotency_key,),
        ).fetchone()
    assert len(swallowed) == 1
    assert swallowed[0]["action_type"] == "demo.park"
    assert swallowed[0]["key"] == "subject:1"
    assert swallowed[0]["existing_id"] == card_id
    assert swallowed[0]["existing_status"] == "rejected"
    assert row["status"] == "ok"
    stored = json.loads(row["result_json"])
    assert stored["status"] == "ok"
    assert stored["approvals_needed"] == []
    assert [a["code"] for a in stored["anomalies"]] == ["engine.approval_swallowed"]


def test_collision_with_pending_card_stays_silent(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_mod, "get_job", lambda agent, job: _PARK)
    run("demo", "demo", "park", params={"n": "1"}, ledger_dir=tmp_path)
    second = run("demo", "demo", "park", params={"n": "2"}, ledger_dir=tmp_path)
    # The pending card holds the ask, so the run honestly still needs approval.
    assert second.status == "needs_approval"
    assert len(second.approvals_needed) == 1
    assert not [a for a in second.anomalies if a.code == "engine.approval_swallowed"]

    root = resolve_ledger_root("demo", tmp_path)
    with Ledger.open(root) as ledger:
        # One pending card is the memory; the second ask deduped by design.
        assert len(_card_ids(ledger, status="pending")) == 1
        assert _swallow_events(ledger) == []
