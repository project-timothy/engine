"""Provenance lens (#356 shadow stage): who mailed each recorded invoice.

Intake records a sender verdict per invoice and places no hold. This lens is
where the verdicts surface, INFO only, so two weeks of real mail show how
many holds enforcement would place and why before anything is enforced.
"""

from __future__ import annotations

from auditor.lenses import provenance

from .fixtures import add_event, make_context, make_ledger

RECENT = "2026-07-18T06:00:00+00:00"  # 3 days before fixture NOW
STALE = "2026-07-01T06:00:00+00:00"  # 20 days before fixture NOW


def _verdict(conn, verdict, *, reason="", vendor="Alpha Parts", sender=None, at=RECENT):
    add_event(
        conn,
        event_type="ap.provenance.recorded",
        payload={
            "file": f"{vendor}-{verdict}.pdf",
            "vendor": vendor,
            "verdict": verdict,
            "reason": reason,
            "sender": sender,
            "sender_domain": sender.rsplit("@", 1)[-1] if sender else None,
        },
        created_at=at,
    )


def _findings(tmp_path):
    ctx = make_context(tmp_path)
    with ctx.ledger:
        return provenance.check(ctx)


def test_vouched_and_matching_mail_is_quiet(tmp_path):
    conn = make_ledger(tmp_path)
    _verdict(conn, "match", sender="ap@alphaparts.example")
    _verdict(conn, "owner_drop")
    _verdict(conn, "internal_forward", sender="owner@tenant.example")
    assert _findings(tmp_path) == []


def test_a_mismatch_surfaces_as_info_naming_the_sender(tmp_path):
    conn = make_ledger(tmp_path)
    _verdict(conn, "mismatch", reason="domain", sender="billing@alphaparts-billing.example")
    (finding,) = _findings(tmp_path)
    assert finding.severity == "INFO"
    assert finding.condition == "sender-mismatch"
    assert "alphaparts-billing.example" in finding.detail
    assert "Alpha Parts" in finding.subject


def test_a_platform_mailer_surfaces_as_unbound(tmp_path):
    conn = make_ledger(tmp_path)
    _verdict(conn, "platform", sender="quickpay@notify.invoiceplatform.example")
    (finding,) = _findings(tmp_path)
    assert (finding.severity, finding.condition) == ("INFO", "sender-platform")


def test_old_verdicts_age_out(tmp_path):
    conn = make_ledger(tmp_path)
    _verdict(conn, "mismatch", reason="domain", sender="x@elsewhere.example", at=STALE)
    assert _findings(tmp_path) == []
