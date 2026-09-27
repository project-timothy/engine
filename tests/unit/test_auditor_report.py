"""Report renderer: three sections, checklist voice, one-line all-clear."""

from __future__ import annotations

from auditor.report import render_report
from auditor.store import ReconcileResult


def _row(
    subject="inv-1",
    condition="no-evidence",
    lens="status",
    severity="WARN",
    detail="detail line",
    first_seen="2026-07-18T06:00:00+00:00",
    reason="new",
):
    return {
        "subject": subject,
        "condition": condition,
        "lens": lens,
        "severity": severity,
        "detail": detail,
        "first_seen": first_seen,
        "last_seen": first_seen,
        "reason": reason,
    }


def test_all_clear_is_one_line():
    text = render_report(
        ReconcileResult(new=[], open=[], resolved=[]), tenant="t", date="2026-07-21"
    )
    body = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    assert len(body) == 1
    assert "clear" in body[0].lower()


def test_three_sections_in_order():
    result = ReconcileResult(
        new=[_row(subject="loud")],
        open=[
            _row(subject="loud"),
            _row(subject="carrying", first_seen="2026-07-14T06:00:00+00:00"),
        ],
        resolved=[_row(subject="fixed")],
    )
    text = render_report(result, tenant="t", date="2026-07-21")
    i_new = text.index("New since the last report")
    i_open = text.index("Open checklist")
    i_res = text.index("Resolved since the last report")
    assert i_new < i_open < i_res
    assert "loud" in text[i_new:i_open]
    assert "carrying" in text[i_open:i_res]
    assert "fixed" in text[i_res:]


def test_open_items_carry_first_seen_and_severity():
    result = ReconcileResult(
        new=[],
        open=[_row(severity="CRITICAL", first_seen="2026-07-14T06:00:00+00:00")],
        resolved=[],
    )
    text = render_report(result, tenant="t", date="2026-07-21")
    open_section = text[text.index("Open checklist") :]
    assert "CRITICAL" in open_section
    assert "2026-07-14" in open_section
    assert "- [ ]" in open_section  # reads like a to-do list, because it is one


def test_new_section_orders_critical_first():
    result = ReconcileResult(
        new=[
            _row(subject="warnish", severity="WARN"),
            _row(subject="critish", severity="CRITICAL"),
        ],
        open=[],
        resolved=[],
    )
    text = render_report(result, tenant="t", date="2026-07-21")
    section = text[text.index("New since") : text.index("Open checklist")]
    assert section.index("critish") < section.index("warnish")


def test_aging_bump_is_labelled_not_repeated_verbatim():
    result = ReconcileResult(
        new=[_row(reason="aging", severity="CRITICAL", first_seen="2026-07-18T06:00:00+00:00")],
        open=[_row(severity="CRITICAL", first_seen="2026-07-18T06:00:00+00:00")],
        resolved=[],
    )
    text = render_report(result, tenant="t", date="2026-07-21")
    section = text[text.index("New since") : text.index("Open checklist")]
    assert "still open" in section  # the bump says why it is back, not a re-alarm


def test_advisory_section_appears_only_when_present():
    result = ReconcileResult(new=[], open=[], resolved=[])
    without = render_report(result, tenant="t", date="2026-07-21")
    with_adv = render_report(result, tenant="t", date="2026-07-21", advisory="Consider X.")
    assert "Advisory" not in without
    assert "Advisory" in with_adv
    assert "Consider X." in with_adv


def test_snooze_expired_note_renders():
    row = {
        "severity": "WARN",
        "lens": "filing",
        "subject": "thing.pdf",
        "detail": "d",
        "first_seen": "2026-09-04T02:00:00+00:00",
        "reason": "snooze-expired",
    }
    text = render_report(ReconcileResult(new=[row], open=[row]), tenant="t", date="2026-09-04")
    assert "thing.pdf: d (snooze expired)" in text
