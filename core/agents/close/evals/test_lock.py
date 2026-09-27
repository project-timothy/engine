"""Lock evals: the seal is owner-executes-UI + engine-verifies.

Redesigned after incident 2026-08-03 (first live close): the accounting
system's API silently ignores book-close-date writes — the field is UI-only.
The engine therefore NEVER writes it. The approved card instructs the exact
UI act; the lock run verifies by readback and records the seal. The evals
pin: a readback that does not show the asked date never records a sealed
event, the engine never calls a write, and the owner's UI act breaks the
"awaiting" replay via the run key (the readback date is a key input).
"""

from __future__ import annotations

import json

from core.agents.close import jobs as close_jobs

from .test_preflight import FakeQbo, _job_ctx, _key, _ledger, _seed_auditor

PAYROLL = [
    {"TxnDate": "2026-07-15", "DocNumber": "PayrollCo", "PrivateNote": "Regular Payroll"},
    {"TxnDate": "2026-07-31", "DocNumber": "PayrollCo", "PrivateNote": "Regular Payroll"},
]


class SealableQbo(FakeQbo):
    def __init__(self, *, book_close_date="", **kwargs):
        super().__init__(**kwargs)
        self.book_close_date = book_close_date

    def fetch_book_close_date(self):
        return self.book_close_date

    def set_book_close_date(self, close_date):  # tripwire, any path that writes fails
        raise AssertionError("the engine must never write the book-close date (2026-08-03)")


def _card(ledger, *, month="2026-07", status="pending"):
    run_id = ledger.conn.execute(
        "INSERT INTO runs (idempotency_key, tenant, agent, job, status, result_json, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (_key("run"), "t", "close", "lock", "ok", "{}", "2026-08-01T06:00:00+00:00"),
    ).lastrowid
    cursor = ledger.conn.execute(
        "INSERT INTO approval_queue (idempotency_key, run_id, tenant, agent, action_type, "
        "params_json, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (
            _key("appr"),
            run_id,
            "t",
            "close",
            close_jobs.LOCK_ACTION,
            json.dumps({"month": month, "close_date": "2026-07-31"}),
            status,
            "2026-08-01T06:00:00+00:00",
        ),
    )
    ledger.conn.commit()
    return int(cursor.lastrowid)


def _run(tmp_path, ledger, qbo, monkeypatch, *, shadow=False):
    monkeypatch.setattr(close_jobs, "_qbo_read_client", lambda ctx: qbo)
    monkeypatch.setenv("AUDITOR_STORE_ROOT", str(tmp_path / "aud"))
    ctx = _job_ctx(tmp_path, ledger, {"month": "2026-07"})
    ctx.shadow = shadow
    return close_jobs._lock_run(ctx)


def test_clean_month_parks_one_card(tmp_path, monkeypatch):
    _seed_auditor(tmp_path)
    qbo = SealableQbo(journal_entries=PAYROLL)
    with _ledger(tmp_path) as ledger:
        output = _run(tmp_path, ledger, qbo, monkeypatch)
    assert output.status == "needs_approval"
    card = output.approvals[0]
    assert card.action_type == close_jobs.LOCK_ACTION
    assert card.params["month"] == "2026-07"
    assert card.params["close_date"] == "2026-07-31"
    # the card instructs the owner's UI act — the engine cannot set the date
    assert "UI" in card.reason


def test_blocked_month_parks_nothing(tmp_path, monkeypatch):
    qbo = SealableQbo()  # no auditor store seeded: machinery BLOCKs
    with _ledger(tmp_path) as ledger:
        output = _run(tmp_path, ledger, qbo, monkeypatch)
    assert output.status == "ok"
    assert "NOT parked" in output.summary
    assert not output.approvals


def test_pending_card_waits(tmp_path, monkeypatch):
    qbo = SealableQbo()
    with _ledger(tmp_path) as ledger:
        _card(ledger, status="pending")
        output = _run(tmp_path, ledger, qbo, monkeypatch)
    assert "awaiting approval" in output.summary


def test_owner_ui_act_verifies_and_seals(tmp_path, monkeypatch):
    """RETARGETED 2026-08-17 (seal redesign): the old test asserted the
    engine's own write sealed the month. Now the owner has set the date in
    the UI; the approved-card run VERIFIES by readback and records."""
    qbo = SealableQbo(book_close_date="2026-07-31")
    with _ledger(tmp_path) as ledger:
        _card(ledger, status="approved")
        output = _run(tmp_path, ledger, qbo, monkeypatch)
    assert output.status == "ok"
    assert "SEALED" in output.summary
    assert output.events[0].payload["close_date"] == "2026-07-31"
    assert output.events[0].payload["readback"] == "2026-07-31"  # the date QBO held
    assert output.events[0].payload["month"] == "2026-07"
    assert not any(a.code == "close.seal_date_overshoots" for a in output.anomalies)


def test_readback_past_month_end_seals_and_records_the_overshoot(tmp_path, monkeypatch):
    """Honesty audit 2026-09-03 (F10): the verification is ``readback >=
    month_end``, so a later date (a typo, or two months sealed in one UI
    act) passes. The books ARE closed through at least the asked day, so it
    still seals; but the record must say what was read, never a date the
    accounting system never held. ``close_date`` stays the asked month end,
    ``readback`` carries the real date, and the overshoot is an anomaly."""
    qbo = SealableQbo(book_close_date="2026-09-30")
    with _ledger(tmp_path) as ledger:
        _card(ledger, status="approved")
        output = _run(tmp_path, ledger, qbo, monkeypatch)
    assert output.status == "ok"
    assert "SEALED" in output.summary
    payload = output.events[0].payload
    assert payload["close_date"] == "2026-07-31"  # what was asked
    assert payload["readback"] == "2026-09-30"  # what QBO actually holds
    overshoot = [a for a in output.anomalies if a.code == "close.seal_date_overshoots"]
    assert len(overshoot) == 1
    assert "2026-07-31" in overshoot[0].detail and "2026-09-30" in overshoot[0].detail
    assert "2026-09-30" in output.summary and "2026-07-31" in output.summary


def test_shadow_never_records(tmp_path, monkeypatch):
    qbo = SealableQbo(book_close_date="2026-07-31")
    with _ledger(tmp_path) as ledger:
        _card(ledger, status="approved")
        output = _run(tmp_path, ledger, qbo, monkeypatch, shadow=True)
    assert "would" in output.summary
    assert not output.events


def test_sealed_month_with_no_ceremony_noops(tmp_path, monkeypatch):
    """RETARGETED 2026-08-17: with an approved card in flight, a set date is
    the VERIFICATION (it seals and records). "Already sealed" is only the
    no-ceremony case: date set, no live card."""
    qbo = SealableQbo(book_close_date="2026-07-31")
    with _ledger(tmp_path) as ledger:
        output = _run(tmp_path, ledger, qbo, monkeypatch)
    assert "already sealed" in output.summary
    assert not output.events


def test_unverified_readback_never_seals_and_instructs(tmp_path, monkeypatch):
    """RETARGETED 2026-08-17 (the incident's pin): a readback that does not
    show the asked date NEVER records a sealed event. It is not an error —
    the owner simply has not made the UI change yet — so the run instructs
    the exact act and flags the unverified seal."""
    qbo = SealableQbo(book_close_date="2026-06-30")
    with _ledger(tmp_path) as ledger:
        _card(ledger, status="approved")
        output = _run(tmp_path, ledger, qbo, monkeypatch)
    assert output.status == "ok"
    assert not output.events
    assert "2026-07-31" in output.summary  # the exact date to set
    assert any(a.code == "close.seal_unverified" for a in output.anomalies)


def test_rejected_card_means_asking_again_parks_fresh(tmp_path, monkeypatch):
    """Incident 2026-09-01 (issue #161): the owner rejects the lock card to
    re-ask (the July #49->#50 flow). Under the #143 stable-subject dedup the
    runner's enqueue is INSERT OR IGNORE, so a re-park on the SAME ap.key
    collides with the rejected row and is silently swallowed — the ask must
    ride a fresh key naming the rejected card it supersedes."""
    _seed_auditor(tmp_path)
    qbo = SealableQbo(journal_entries=PAYROLL)
    with _ledger(tmp_path) as ledger:
        rejected_id = _card(ledger, status="rejected")
        output = _run(tmp_path, ledger, qbo, monkeypatch)
    assert output.status == "needs_approval"
    assert output.approvals[0].key == f"lock:2026-07:reask-{rejected_id}"


def test_second_reject_reasks_on_a_distinct_key(tmp_path, monkeypatch):
    """A rejected re-ask card must itself be re-askable: the key follows the
    NEWEST rejected card, so every reject in the chain frees a new key."""
    _seed_auditor(tmp_path)
    qbo = SealableQbo(journal_entries=PAYROLL)
    with _ledger(tmp_path) as ledger:
        first_id = _card(ledger, status="rejected")
        second_id = _card(ledger, status="rejected")
        output = _run(tmp_path, ledger, qbo, monkeypatch)
    assert output.status == "needs_approval"
    assert output.approvals[0].key == f"lock:2026-07:reask-{second_id}"
    assert output.approvals[0].key != f"lock:2026-07:reask-{first_id}"


def test_key_changes_with_card_state_and_readback(tmp_path, monkeypatch):
    """The owner's UI act must break the replay: after approval the run
    records "awaiting the UI act"; without the readback date in the key,
    the verification re-run would replay that result forever and the seal
    would never record."""
    qbo = SealableQbo()
    monkeypatch.setattr(close_jobs, "_qbo_read_client", lambda ctx: qbo)
    monkeypatch.setenv("AUDITOR_STORE_ROOT", str(tmp_path / "aud"))
    with _ledger(tmp_path) as ledger:
        ctx = _job_ctx(tmp_path, ledger, {"month": "2026-07"})
        key_none = close_jobs._lock_key(ctx)
        _card(ledger, status="pending")
        key_pending = close_jobs._lock_key(ctx)
        ledger.conn.execute(
            "UPDATE approval_queue SET status='approved' WHERE action_type=?",
            (close_jobs.LOCK_ACTION,),
        )
        ledger.conn.commit()
        key_approved_unset = close_jobs._lock_key(ctx)
        qbo.book_close_date = "2026-07-31"  # the owner's UI act
        key_approved_set = close_jobs._lock_key(ctx)
    assert len({key_none, key_pending, key_approved_unset, key_approved_set}) == 4
