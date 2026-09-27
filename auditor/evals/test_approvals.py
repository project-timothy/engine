"""Approval-hygiene lens evals: the queue is a checkpoint, not a parking lot."""

from __future__ import annotations

from auditor.lenses import approvals

from .fixtures import add_approval, add_event, add_invoice, make_context, make_ledger

FRESH = "2026-07-19T06:00:00+00:00"  # 2 days before fixture NOW
OLD = "2026-07-11T06:00:00+00:00"  # 10 days before fixture NOW
ANCIENT = "2026-06-20T06:00:00+00:00"  # 31 days before fixture NOW


def _conditions(tmp_path):
    ctx = make_context(tmp_path)
    with ctx.ledger:
        return sorted(f.condition for f in approvals.check(ctx))


def _findings(tmp_path):
    ctx = make_context(tmp_path)
    with ctx.ledger:
        return approvals.check(ctx)


def test_young_pending_card_is_quiet(tmp_path):
    conn = make_ledger(tmp_path)
    add_approval(conn, action_type="ap.payment_recommendation", created_at=FRESH)
    assert _conditions(tmp_path) == []


def test_stale_pending_card_is_a_finding(tmp_path):
    conn = make_ledger(tmp_path)
    add_approval(conn, action_type="ap.payment_recommendation", created_at=OLD)
    assert _conditions(tmp_path) == ["stale-pending"]


def test_fully_executed_batch_is_quiet(tmp_path):
    conn = make_ledger(tmp_path)
    a = add_invoice(conn, invoice_number="A", qbo_bill_id="77")
    b = add_invoice(conn, invoice_number="B", qbo_bill_id="78")
    add_approval(
        conn,
        params={"row_ids": f"{a},{b}"},
        status="approved",
        created_at=FRESH,
        resolved_at=FRESH,
    )
    assert _conditions(tmp_path) == []


def test_approved_batch_with_an_unexecuted_row_is_a_finding(tmp_path):
    conn = make_ledger(tmp_path)
    done = add_invoice(conn, invoice_number="A", qbo_bill_id="77")
    ghost = add_invoice(conn, invoice_number="B")  # approved, never written
    add_approval(
        conn,
        params={"row_ids": f"{done},{ghost}"},
        status="approved",
        created_at=FRESH,
        resolved_at=FRESH,
    )
    assert _conditions(tmp_path) == ["approved-not-executed"]


def test_row_parked_behind_a_mapping_card_is_accounted_for(tmp_path):
    conn = make_ledger(tmp_path)
    parked = add_invoice(conn, invoice_number="B")
    add_approval(
        conn,
        params={"row_ids": str(parked)},
        status="approved",
        created_at=FRESH,
        resolved_at=FRESH,
    )
    add_approval(
        conn,
        action_type="ap.qbo_map_vendor",
        params={"row_id": str(parked), "vendor": "V"},
        status="pending",
        created_at=FRESH,
    )
    assert _conditions(tmp_path) == []


def test_settled_row_counts_as_consumed(tmp_path):
    # Live false positive, 2026-07-20: an annual premium the owner paid in
    # full on the card, with an explicit no-bill note — the row settled to
    # Paid after the batch approval, closing the AP question by another
    # route. Whether the accounting system agrees is lens 7's question.
    conn = make_ledger(tmp_path)
    settled = add_invoice(conn, invoice_number="IUJ", status="Paid")
    add_approval(
        conn,
        params={"row_ids": str(settled)},
        status="approved",
        created_at=FRESH,
        resolved_at=FRESH,
    )
    assert _conditions(tmp_path) == []


def test_rejected_batches_carry_no_obligation(tmp_path):
    conn = make_ledger(tmp_path)
    ghost = add_invoice(conn, invoice_number="B")
    add_approval(
        conn,
        params={"row_ids": str(ghost)},
        status="rejected",
        created_at=FRESH,
        resolved_at=FRESH,
    )
    assert _conditions(tmp_path) == []


# ---- the ask the queue dropped ----------------------------------------------
#
# The engine records a swallowed ask twice over: an ``engine.approval_swallowed``
# event and an anomaly on the run (tests/unit/test_runner_approval_swallow.py
# pins both, "so the ledger records the truth ... instead of invisible"). No
# lens has ever read either one, so the record reached nobody: a drop is
# visible in the ledger and silent on the report, which is the same place the
# owner looks. These evals are that delivery surface.


def _swallow(
    conn,
    *,
    key="subject:1",
    action_type="demo.park",
    existing_id=7,
    existing_status="rejected",
    created_at=FRESH,
):
    add_event(
        conn,
        event_type="engine.approval_swallowed",
        created_at=created_at,
        payload={
            "action_type": action_type,
            "key": key,
            "existing_id": existing_id,
            "existing_status": existing_status,
        },
    )


def test_a_dropped_ask_is_a_finding(tmp_path):
    conn = make_ledger(tmp_path)
    _swallow(conn)
    assert _conditions(tmp_path) == ["swallowed"]


def test_the_finding_names_the_ask_and_the_card_that_blocked_it(tmp_path):
    """The subject has to identify the ask (so one drop is one checklist item
    the owner can answer or mute), and the detail has to name the resolved
    card that absorbed it — without that id nobody can tell whether the drop
    was a stale rejection or a decision that still stands."""
    conn = make_ledger(tmp_path)
    _swallow(conn, key="inbox:abc123", action_type="demo.file_thing", existing_id=81)
    (finding,) = _findings(tmp_path)
    assert finding.severity == "WARN"
    assert "demo.file_thing" in finding.subject
    assert "inbox:abc123" in finding.subject
    assert "#81" in finding.detail
    assert "rejected" in finding.detail


def test_one_ask_dropped_many_times_is_one_finding_that_counts_them(tmp_path):
    """The lane re-asks whenever its input re-keys, and every re-ask hits the
    same resolved card. Three drops of one ask are one open question, not
    three, and the count is what tells the owner it keeps happening."""
    conn = make_ledger(tmp_path)
    for when in (OLD, "2026-07-15T06:00:00+00:00", FRESH):
        _swallow(conn, created_at=when)
    (finding,) = _findings(tmp_path)
    assert "3 times" in finding.detail
    assert "2026-07-11" in finding.detail  # first drop
    assert "2026-07-19" in finding.detail  # latest drop


def test_two_different_asks_are_two_findings(tmp_path):
    conn = make_ledger(tmp_path)
    _swallow(conn, key="subject:1")
    _swallow(conn, key="subject:2")
    assert [f.condition for f in _findings(tmp_path)] == ["swallowed", "swallowed"]


def test_a_drop_older_than_the_lookback_ages_out(tmp_path):
    """A drop is instantaneous, so it can never "resolve" on its own. Aging it
    out of the report is what lets the checklist reconciliation close the item
    once the lane stops re-asking — the rule the heartbeat lens applies to
    preflight markers."""
    conn = make_ledger(tmp_path)
    _swallow(conn, created_at=ANCIENT)
    assert _conditions(tmp_path) == []


def test_an_old_drop_that_happened_again_recently_still_reports(tmp_path):
    conn = make_ledger(tmp_path)
    _swallow(conn, created_at=ANCIENT)
    _swallow(conn, created_at=FRESH)
    assert _conditions(tmp_path) == ["swallowed"]


def test_an_unparseable_payload_still_reports_the_drop(tmp_path):
    """Never let a malformed payload turn a dropped ask back into silence."""
    conn = make_ledger(tmp_path)
    add_event(conn, event_type="engine.approval_swallowed", payload={}, created_at=FRESH)
    assert _conditions(tmp_path) == ["swallowed"]


def test_other_events_are_not_drops(tmp_path):
    conn = make_ledger(tmp_path)
    add_event(conn, event_type="ap.invoice.recorded", payload={"file": "x.pdf"}, created_at=FRESH)
    assert _conditions(tmp_path) == []
