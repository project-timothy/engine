"""Reconcile-unknowns lens evals (2026-09-04): money that left the bank and
matched no ledger row was recorded by the engine as ``ap.reconcile.unknown``
and then never mentioned again (the 2026-05-19 lesson: an unlogged payment,
not a search miss). Each clearing surfaces once, keyed by its bank id."""

from __future__ import annotations

from auditor.findings import Finding
from auditor.lenses import reconcile

from .fixtures import (
    add_approval,
    add_event,
    add_expense_report,
    add_invoice,
    make_context,
    make_ledger,
)


def _unknown(
    conn, qbo_id, *, date, payee="Someone", amount_cents=425000, check_ref="", created_at=None
):
    add_event(
        conn,
        event_type="ap.reconcile.unknown",
        payload={
            "qbo_id": qbo_id,
            "payee": payee,
            "amount_cents": amount_cents,
            "date": date,
            "check_ref": check_ref,
        },
        created_at=created_at or f"{date}T12:00:00+00:00",
    )


def test_recent_unknown_clearing_surfaces_once_by_bank_id(tmp_path):
    conn = make_ledger(tmp_path)
    _unknown(conn, "Purchase:285", date="2026-07-15", payee="A Contractor", check_ref="4051")
    ctx = make_context(tmp_path)
    with ctx.ledger:
        findings = reconcile.check(ctx)
    assert [(f.subject, f.condition, f.severity) for f in findings] == [
        ("Purchase:285", "unknown-clearing", "WARN")
    ]
    assert "$4,250.00 to A Contractor on 2026-07-15 (check 4051)" in findings[0].detail


# ---- issue #370: one hand-check floor, shared with the engine lane --------


def test_a_clearing_below_the_hand_check_floor_never_warns(tmp_path):
    """The direct-payment lane (core/agents/ap/direct_payment.py) refuses to
    card a clearing under [qbo].direct_payment_floor_cents, so a WARN below
    that same line has no answer path (owner decision 2026-09-28, root cause
    2 of the 9/28 triage). Lens 15 reads the identical tenant setting and
    stays quiet on it too — the smallest honest change: skip it outright,
    the same as the lane already does, rather than inventing a softer
    severity for an item nothing can ever close."""
    conn = make_ledger(tmp_path)
    _unknown(conn, "Purchase:1", date="2026-07-15", amount_cents=59_999)  # under the $600 default
    _unknown(conn, "Purchase:2", date="2026-07-15", amount_cents=60_000)  # exactly at the floor
    ctx = make_context(tmp_path)
    with ctx.ledger:
        assert [f.subject for f in reconcile.check(ctx)] == ["Purchase:2"]


def test_the_floor_is_the_tenants_own_setting_not_a_hardcoded_number(tmp_path):
    """A tenant that raised or lowered [qbo].direct_payment_floor_cents sees
    the SAME number here that the direct-payment lane uses -- one setting,
    read twice."""
    conn = make_ledger(tmp_path)
    _unknown(conn, "Purchase:1", date="2026-07-15", amount_cents=100_000)
    ctx = make_context(tmp_path, qbo_direct_payment_floor_cents=0)
    with ctx.ledger:
        assert [f.subject for f in reconcile.check(ctx)] == ["Purchase:1"]
    ctx = make_context(tmp_path, qbo_direct_payment_floor_cents=500_000)
    with ctx.ledger:
        assert reconcile.check(ctx) == []


def test_the_warn_detail_names_the_floor_in_effect(tmp_path):
    conn = make_ledger(tmp_path)
    _unknown(conn, "Purchase:1", date="2026-07-15", amount_cents=1_000_000)
    ctx = make_context(tmp_path)
    with ctx.ledger:
        findings = reconcile.check(ctx)
    assert "$600.00 hand-check floor" in findings[0].detail
    ctx = make_context(tmp_path, qbo_direct_payment_floor_cents=250_000)
    with ctx.ledger:
        findings = reconcile.check(ctx)
    assert "$2,500.00 hand-check floor" in findings[0].detail


def test_clearing_older_than_the_window_ages_out(tmp_path):
    conn = make_ledger(tmp_path)
    _unknown(conn, "Purchase:1", date="2026-06-01")  # 50 days before NOW
    _unknown(conn, "Purchase:2", date="2026-07-01")
    ctx = make_context(tmp_path)
    with ctx.ledger:
        assert [f.subject for f in reconcile.check(ctx)] == ["Purchase:2"]
    ctx = make_context(tmp_path, reconcile_unknown_window_days=60)
    with ctx.ledger:
        assert [f.subject for f in reconcile.check(ctx)] == ["Purchase:1", "Purchase:2"]


def test_two_events_for_one_clearing_carry_the_newest_amount(tmp_path):
    """Purchase 304 (2026-09-16): the QBO record was trimmed from $5,731.18 to
    $5,689.28 after the first unknown event was written, so the oldest event
    carries a figure that never cleared. The detail line reports the newest
    event; the fingerprint stays subject-only, so the mute already on the item
    survives."""
    conn = make_ledger(tmp_path)
    _unknown(conn, "Purchase:304", date="2026-07-15", payee="A Vendor", amount_cents=573118)
    _unknown(conn, "Purchase:304", date="2026-07-15", payee="A Vendor", amount_cents=568928)
    ctx = make_context(tmp_path)
    with ctx.ledger:
        findings = reconcile.check(ctx)
    assert len(findings) == 1
    assert "$5,689.28" in findings[0].detail
    assert "$5,731.18" not in findings[0].detail
    unchanged = Finding(
        lens="reconcile",
        subject="Purchase:304",
        condition="unknown-clearing",
        severity="WARN",
        detail="the fingerprint ignores the detail line",
    )
    assert findings[0].fingerprint == unchanged.fingerprint


def test_2026_09_17_an_expense_reports_own_purchase_is_not_unknown_money(tmp_path):
    """Purchase 304 — a staffer's September reimbursement, cleared 2026-09-15
    — is the record the ENGINE itself wrote for expense report #2 at ``expenses
    match``. Reconcile had no expense-report tier the night it cleared, so it
    logged ``ap.reconcile.unknown``; the tier landed the next morning (#291,
    issue #281) and correctly re-decides the clearing as already recorded.
    But an event log is append-only and that re-decision writes nothing, so
    the stale unknown stayed the newest word and this lens re-asked the owner
    about his own recorded money every night for the rest of the 30-day
    window — the 7th hand mute of a finding the engine had already answered.

    The lens answers it itself rather than trusting an engine verdict: an
    expense report commits money with no ``ap_invoices`` row, and
    ``qbo_purchase_id`` is the exact id the engine recorded for it. That IS
    the ledger row this clearing matched. Identity, not a heuristic."""
    conn = make_ledger(tmp_path)
    add_expense_report(
        conn, person="A Staffer", month="2026-09", total_cents=568928, qbo_purchase_id="304"
    )
    _unknown(
        conn,
        "Purchase:304",
        date="2026-07-15",
        payee="A Payer LLC",
        amount_cents=573118,
        check_ref="EXP-2",
    )
    _unknown(conn, "Purchase:285", date="2026-07-15", payee="A Contractor", check_ref="4051")
    ctx = make_context(tmp_path)
    with ctx.ledger:
        findings = reconcile.check(ctx)
    assert [f.subject for f in findings] == ["Purchase:285"]


def test_only_a_recorded_report_on_the_purchase_entity_is_excluded(tmp_path):
    """The join is entity + exact id, and nothing weaker. A report that never
    reached the accounting system carries no id and excludes nothing; another
    entity sharing the raw id is a different record; and another tenant's
    report is not this tenant's money."""
    conn = make_ledger(tmp_path)
    add_expense_report(conn, person="A Staffer", month="2026-09", total_cents=10000)
    add_expense_report(
        conn, tenant="other", person="Someone", month="2026-09", qbo_purchase_id="900"
    )
    add_expense_report(conn, person="Another Staffer", month="2026-09", qbo_purchase_id="304")
    _unknown(conn, "Purchase:900", date="2026-07-15")
    _unknown(conn, "BillPayment:304", date="2026-07-15")
    ctx = make_context(tmp_path)
    with ctx.ledger:
        assert [f.subject for f in reconcile.check(ctx)] == ["Purchase:900", "BillPayment:304"]


def _recorded(conn, qbo_id, **kw):
    """A payable row exactly as the hand-check lane writes one on approval:
    the clearing's own id verbatim in ``source_file``, a ``DP-`` number
    synthesized from it, status Paid."""
    number = "DP-" + qbo_id.replace(":", "-")
    kw.setdefault("amount_cents", 425000)
    return add_invoice(conn, invoice_number=number, status="Paid", source_file=qbo_id, **kw)


def test_2026_09_28_a_recorded_hand_check_payment_is_not_unknown_money(tmp_path):
    """Tonight's live shape, and the third time this lens has re-asked about
    money the book already holds.

    The engine's hand-check lane (7.4) closes its own loop: it writes
    ``ap.reconcile.unknown`` once, parks an ``ap.record_direct_payment`` card,
    and on the owner's approval creates the payable row. This lens reads only
    the first of those three. So a clearing the owner ANSWERED and the engine
    RECORDED keeps its WARN until it ages out of the window, and the only exit
    is a hand mute — which is exactly what happened to the six unknown
    clearings that closed in one batch on 2026-09-12, none of them by the
    money being recorded.

    The join is the same shape as the expense-report one above and the same
    lesson ("An improved rule must be able to retract": re-derive from ledger
    state, and a mute is a missing join). The lane writes the clearing's id
    verbatim into ``source_file``, so the auditor reads an id, never a
    filename convention it would have to re-spell — core does not import
    auditor and auditor does not import core.
    """
    conn = make_ledger(tmp_path)
    _recorded(conn, "Purchase:313", vendor="A Contractor", amount_cents=5012345)
    _unknown(
        conn,
        "Purchase:313",
        date="2026-07-15",
        payee="A Contractor",
        amount_cents=5012345,
        check_ref="8156",
    )
    _unknown(conn, "Purchase:320", date="2026-07-15", payee="A Card Account", amount_cents=383_400)
    ctx = make_context(tmp_path)
    with ctx.ledger:
        findings = reconcile.check(ctx)
    assert [f.subject for f in findings] == ["Purchase:320"]


# ---- issue #372: a rejected hand-check card closes the finding too --------


def _rejected(conn, qbo_id, *, resolved_at="2026-07-20T09:00:00+00:00", **extra):
    """An ``ap.record_direct_payment`` card the owner rejected: the engine's
    own event names the card an approval, never a payable row, so the
    approval queue itself is the only place this decision lives."""
    params = {"qbo_id": qbo_id, "payee": "A Contractor", "amount_cents": 425000, **extra}
    return add_approval(
        conn,
        action_type="ap.record_direct_payment",
        params=params,
        status="rejected",
        resolved_at=resolved_at,
    )


def test_a_rejected_card_closes_the_finding_like_a_recorded_one(tmp_path):
    """The owner's rejection is an answer too ("this is not AP"), even
    though nothing was recorded: the lens still closes the finding rather
    than re-warning about money the owner already decided on."""
    conn = make_ledger(tmp_path)
    card_id = _rejected(conn, "Purchase:313")
    _unknown(conn, "Purchase:313", date="2026-07-15", payee="A Contractor", check_ref="8156")
    _unknown(conn, "Purchase:320", date="2026-07-15", payee="A Card Account", amount_cents=120000)
    ctx = make_context(tmp_path)
    with ctx.ledger:
        findings = reconcile.check(ctx)
    assert [f.subject for f in findings] == ["Purchase:320"]
    assert card_id > 0


def test_a_pending_or_approved_card_never_closes_the_finding(tmp_path):
    """Only a REJECTED status answers the finding this way; a card still
    pending, or one the owner approved (closed instead through the recorded
    ``ap_invoices`` row, #368), leaves the WARN exactly where it was."""
    conn = make_ledger(tmp_path)
    _rejected(conn, "Purchase:1")
    conn.execute("UPDATE approval_queue SET status='pending' WHERE tenant='t'")
    _rejected(conn, "Purchase:2")
    conn.execute(
        "UPDATE approval_queue SET status='approved' WHERE params_json LIKE '%Purchase:2%'"
    )
    conn.commit()
    _unknown(conn, "Purchase:1", date="2026-07-15")
    _unknown(conn, "Purchase:2", date="2026-07-15")
    ctx = make_context(tmp_path)
    with ctx.ledger:
        assert [f.subject for f in reconcile.check(ctx)] == ["Purchase:1", "Purchase:2"]


def test_resolve_notes_names_the_rejected_card_and_the_date(tmp_path):
    conn = make_ledger(tmp_path)
    card_id = _rejected(conn, "Purchase:313", resolved_at="2026-07-20T09:00:00+00:00")
    _unknown(conn, "Purchase:313", date="2026-07-15", payee="A Contractor", check_ref="8156")
    ctx = make_context(tmp_path)
    with ctx.ledger:
        notes = reconcile.resolve_notes(ctx)
    fp = Finding(
        lens="reconcile",
        subject="Purchase:313",
        condition="unknown-clearing",
        severity="WARN",
        detail="",
    ).fingerprint
    assert notes == {fp: f"owner rejected card #{card_id} on 2026-07-20"}


def test_resolve_notes_is_silent_once_the_clearing_is_recorded_or_never_rejected(tmp_path):
    conn = make_ledger(tmp_path)
    _rejected(conn, "Purchase:1")
    conn.execute("UPDATE approval_queue SET status='pending' WHERE tenant='t'")
    conn.commit()
    ctx = make_context(tmp_path)
    with ctx.ledger:
        assert reconcile.resolve_notes(ctx) == {}


def test_an_unrecorded_clearing_still_warns(tmp_path):
    """The converse fence. The join must close a finding the ledger answers
    and nothing else: with no payable row behind it, the clearing is still
    unexplained money and the WARN is the whole point of the lens."""
    conn = make_ledger(tmp_path)
    _unknown(conn, "Purchase:313", date="2026-07-15", payee="A Contractor", check_ref="8156")
    ctx = make_context(tmp_path)
    with ctx.ledger:
        assert [f.subject for f in reconcile.check(ctx)] == ["Purchase:313"]


def test_only_the_hand_check_lanes_own_row_excludes_a_clearing(tmp_path):
    """Identity, not resemblance — every clause load-bearing.

    A recorded row for a DIFFERENT clearing is not this one's answer; an
    ordinary invoice whose ``source_file`` is a filename never closes
    anything; another tenant's row is not this tenant's money; and a row
    carrying the id without the lane's own ``DP-`` number did not come from
    this lane at all.
    """
    conn = make_ledger(tmp_path)
    _recorded(conn, "Purchase:999")  # a different clearing
    _recorded(conn, "Purchase:280", tenant="other")  # another tenant's books
    add_invoice(  # an ordinary invoice, filename in source_file
        conn, invoice_number="INV-77", source_file="scan-2026-07-15.pdf"
    )
    add_invoice(  # the id, but not a row this lane wrote
        conn, invoice_number="INV-78", source_file="Purchase:285"
    )
    for qbo_id in ("Purchase:999", "Purchase:280", "Purchase:285", "Purchase:313"):
        _unknown(conn, qbo_id, date="2026-07-15")
    ctx = make_context(tmp_path)
    with ctx.ledger:
        subjects = [f.subject for f in reconcile.check(ctx)]
    assert subjects == ["Purchase:280", "Purchase:285", "Purchase:313"]


# ---- 2026-10-02: the sweep lane answers a clearing too --------------------


def _swept(
    conn,
    check_ref,
    *,
    amount_cents=2512345,
    account="An Equity Account",
    status="approved",
    resolved_at="2026-07-20T09:00:00+00:00",
    tenant="t",
    **extra,
):
    """A ``qbo.sweep_parked`` card exactly as the sweep lane parks one: the
    bank feed's own facts, keyed on ``feed_line_id``, with NO ``qbo_id`` —
    the sweep reads a note, never the reconcile's clearing ids."""
    params = {
        "feed_line_id": "33dd9a2361f0885e",
        "date": "2026-07-15",
        "amount": f"${amount_cents / 100:,.2f}",
        "amount_cents": amount_cents,
        "direction": "out",
        "bank_text": "BUSINESS CHECKING",
        "check_ref": check_ref,
        "account": account,
        "source": "sweep",
        **extra,
    }
    return add_approval(
        conn,
        tenant=tenant,
        action_type="qbo.sweep_parked",
        params=params,
        status=status,
        resolved_at=resolved_at,
    )


def test_2026_10_02_a_clearing_the_sweep_card_answered_is_not_unknown_money(tmp_path):
    """Tonight's live shape, and the fourth time this lens has re-asked about
    money the owner already answered.

    One physical check reaches the owner through more than one lane. The
    engine knows this and acts on it: ``_carded_checks`` in
    ``core/agents/ap/jobs.py`` holds every ``(normalized check, cents)`` pair
    already in front of the owner from EITHER card source, and the hand-check
    lane refuses to park a second card for a check the sweep already carries
    — "one question, one answer". So when a check clears on the statement and
    the sweep's note already named it, no ``ap.record_direct_payment`` card is
    ever created for it, and the owner answers it on the sweep card instead.

    All three of this lens's existing exits are keyed on the clearing's own
    ``qbo_id``, and a sweep card carries none — its identity is the bank
    feed's ``feed_line_id``. So the one clearing the owner answered WITH A
    CODING is the only one that keeps its WARN, while the ones he merely
    dismissed close through the rejected-card exit. Exactly inverted, and the
    only way out was a hand mute.

    The join is the engine's own cross-source identity, read the auditor's
    way: the check number plus the amount to the cent. Both sides are the
    bank's facts about one instrument, so this is identity and not
    resemblance — the same argument lens 14 makes when it counts a bank
    sighting as an observation.
    """
    conn = make_ledger(tmp_path)
    _swept(conn, "8158", amount_cents=2512345)
    _unknown(
        conn, "stmt:d45b40", date="2026-07-15", payee="", amount_cents=2512345, check_ref="8158"
    )
    # the same amount, a different check, and nobody has answered this one
    _unknown(
        conn, "stmt:71ab6c", date="2026-07-15", payee="", amount_cents=2512345, check_ref="8159"
    )
    ctx = make_context(tmp_path)
    with ctx.ledger:
        assert [f.subject for f in reconcile.check(ctx)] == ["stmt:71ab6c"]


def test_only_a_decided_sweep_card_for_the_same_instrument_closes_a_clearing(tmp_path):
    """The converse fence, every clause load-bearing.

    A card still PENDING is an open question, not an answer, so the WARN
    stands exactly as it does for a pending hand-check card. The check number
    alone is not enough and the amount alone is not enough: the pair is the
    instrument. A clearing with no check reference joins nothing at all
    rather than falling back to amount-matching, which would close a finding
    on a coincidence. And another tenant's card is not this tenant's money.
    """
    conn = make_ledger(tmp_path)
    _swept(conn, "2001", status="pending", resolved_at=None)  # asked, not answered
    _swept(conn, "2002", amount_cents=999)  # same check, a different amount
    _swept(conn, "2004", amount_cents=2512345, tenant="other")  # another tenant's books
    _swept(conn, "", amount_cents=2512345)  # a feed row with no check number
    for check_ref in ("2001", "2002", "2003", "2004"):
        _unknown(
            conn,
            f"stmt:{check_ref}",
            date="2026-07-15",
            amount_cents=2512345,
            check_ref=check_ref,
        )
    _unknown(conn, "stmt:none", date="2026-07-15", amount_cents=2512345, check_ref="")
    ctx = make_context(tmp_path)
    with ctx.ledger:
        subjects = [f.subject for f in reconcile.check(ctx)]
    assert subjects == ["stmt:2001", "stmt:2002", "stmt:2003", "stmt:2004", "stmt:none"]


def test_a_sweep_cards_check_reference_is_compared_the_engines_way(tmp_path):
    """``norm_check_ref``'s contract, duplicated here because the auditor
    never imports core: "Check 8158" and "8158" are one instrument. A tenant
    writing the note by hand spells it either way."""
    conn = make_ledger(tmp_path)
    _swept(conn, "Check 8158", amount_cents=2512345)
    _unknown(conn, "stmt:d45b40", date="2026-07-15", amount_cents=2512345, check_ref="8158")
    ctx = make_context(tmp_path)
    with ctx.ledger:
        assert reconcile.check(ctx) == []


def test_resolve_notes_names_the_sweep_cards_answer(tmp_path):
    """An approved sweep card carries the coding the owner chose, and that is
    the one fact worth putting on the resolved line: the finding did not stop
    mattering, he answered it. A rejected sweep row says "not the engine's
    business", the same answer the hand-check lane's rejection gives."""
    conn = make_ledger(tmp_path)
    coded = _swept(conn, "8158", amount_cents=2512345, account="An Equity Account")
    _unknown(conn, "stmt:d45b40", date="2026-07-15", amount_cents=2512345, check_ref="8158")
    dismissed = _swept(
        conn,
        "1060",
        amount_cents=700_000,
        account="",
        status="rejected",
        resolved_at="2026-07-19T09:00:00+00:00",
        feed_line_id="ef17697b2a70151b",
    )
    _unknown(conn, "stmt:71ab6c", date="2026-07-15", amount_cents=700_000, check_ref="1060")
    ctx = make_context(tmp_path)
    with ctx.ledger:
        notes = reconcile.resolve_notes(ctx)

    def fingerprint(subject):
        return Finding(
            lens="reconcile",
            subject=subject,
            condition="unknown-clearing",
            severity="WARN",
            detail="",
        ).fingerprint

    assert notes[fingerprint("stmt:d45b40")] == (
        f"owner coded check 8158 to An Equity Account on sweep card #{coded}, 2026-07-20"
    )
    assert notes[fingerprint("stmt:71ab6c")] == (
        f"owner rejected sweep card #{dismissed} on 2026-07-19"
    )


def test_payload_without_a_date_falls_back_to_the_event_time(tmp_path):
    conn = make_ledger(tmp_path)
    add_event(
        conn,
        event_type="ap.reconcile.unknown",
        payload={"qbo_id": "Purchase:9", "payee": "", "amount_cents": 100_000},
        created_at="2026-07-20T12:00:00+00:00",
    )
    ctx = make_context(tmp_path)
    with ctx.ledger:
        findings = reconcile.check(ctx)
    assert [f.subject for f in findings] == ["Purchase:9"]
    assert "(no payee)" in findings[0].detail


def test_disabled_and_eventless_ledgers_are_quiet(tmp_path):
    conn = make_ledger(tmp_path)
    _unknown(conn, "Purchase:1", date="2026-07-15")
    ctx = make_context(tmp_path, reconcile_enabled=False)
    with ctx.ledger:
        assert reconcile.check(ctx) == []
    import sqlite3

    (tmp_path / "empty").mkdir()
    sqlite3.connect(tmp_path / "empty" / "ledger.sqlite3").close()
    ctx = make_context(tmp_path / "empty")
    with ctx.ledger:
        assert reconcile.check(ctx) == []
