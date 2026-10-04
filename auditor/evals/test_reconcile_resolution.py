"""End-to-end reconcile resolution evals (issue #372): a rejected hand-check
card closes lens 15's finding the same night as an approved one (#368), but
the resolved line names the owner's rejection instead of carrying the stale
WARN detail forward untouched. The sweep lane's answer (2026-10-02) closes a
finding the same way, naming the coding the owner chose.
"""

from __future__ import annotations

from datetime import UTC, datetime

from auditor.lenses import LensSpec, reconcile
from auditor.runner import run_audit

from .fixtures import add_approval, add_event, add_invoice, make_ledger

RECONCILE_LENS = [LensSpec(name=reconcile.LENS, check=reconcile.check)]


def _world(tmp_path):
    tenants_dir = tmp_path / "tenants" / "t"
    tenants_dir.mkdir(parents=True, exist_ok=True)
    (tenants_dir / "tenant.toml").write_text('[identity]\nslug = "t"\ntimezone = "UTC"\n')
    conn = make_ledger(tmp_path / "ledger" / "t")
    return conn, {
        "tenants_dir": tmp_path / "tenants",
        "ledger_dir": tmp_path / "ledger",
        "store_dir": tmp_path / "store",
        "report_dir": tmp_path / "reports",
        "lenses": RECONCILE_LENS,
        "drafter": lambda facts: "quiet",
    }


def _unknown(conn, qbo_id, *, date, payee="A Contractor", amount_cents=425000, check_ref="8156"):
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
        created_at=f"{date}T12:00:00+00:00",
    )


def test_a_rejected_card_resolves_with_the_rejection_named(tmp_path):
    conn, world = _world(tmp_path)
    _unknown(conn, "Purchase:313", date="2026-07-15")

    night1 = run_audit("t", now=datetime(2026, 7, 21, 6, 0, tzinfo=UTC), **world)
    # night 1: still unexplained, nothing to say about it yet
    assert "Purchase:313" in night1.report_text
    assert "## Resolved" in night1.report_text
    assert "Purchase:313" not in night1.report_text.split("## Resolved")[1]

    card_id = add_approval(
        conn,
        action_type="ap.record_direct_payment",
        params={"qbo_id": "Purchase:313", "payee": "A Contractor", "amount_cents": 425000},
        status="rejected",
        resolved_at="2026-07-22T09:00:00+00:00",
    )
    conn.commit()

    night2 = run_audit("t", now=datetime(2026, 7, 23, 6, 0, tzinfo=UTC), **world)
    assert f"owner rejected card #{card_id} on 2026-07-22" in night2.report_text
    assert "matched no ledger row" not in (night2.report_text.split("## Resolved")[1])


def test_a_coded_sweep_card_resolves_with_the_coding_named(tmp_path):
    """2026-10-02, end to end: a $25,123.45 check cleared on the statement,
    the sweep's note had already named it so no hand-check card was ever
    parked for it (the engine's own cross-source rule), and the owner
    approved the sweep card with a coding. The next morning's report must
    close the finding and say what he answered, rather than printing the
    stale WARN asking him to record a row he has already dealt with."""
    conn, world = _world(tmp_path)
    _unknown(
        conn, "stmt:d45b40", date="2026-07-15", payee="", amount_cents=2512345, check_ref="8158"
    )

    night1 = run_audit("t", now=datetime(2026, 7, 21, 6, 0, tzinfo=UTC), **world)
    assert "stmt:d45b40" in night1.report_text
    assert "stmt:d45b40" not in night1.report_text.split("## Resolved")[1]

    card_id = add_approval(
        conn,
        action_type="qbo.sweep_parked",
        params={
            "feed_line_id": "33dd9a2361f0885e",
            "amount_cents": 2512345,
            "check_ref": "8158",
            "account": "An Equity Account",
            "source": "sweep",
        },
        status="approved",
        resolved_at="2026-07-22T09:00:00+00:00",
    )
    conn.commit()

    night2 = run_audit("t", now=datetime(2026, 7, 23, 6, 0, tzinfo=UTC), **world)
    resolved_section = night2.report_text.split("## Resolved")[1]
    assert f"owner coded check 8158 to An Equity Account on sweep card #{card_id}" in (
        resolved_section
    )
    assert "matched no ledger row" not in resolved_section


def test_an_approved_cards_path_is_unchanged(tmp_path):
    """#368's own behavior: closing via a RECORDED payable row still carries
    the last WARN detail forward on the resolved line, untouched."""
    conn, world = _world(tmp_path)
    _unknown(conn, "Purchase:400", date="2026-07-15")
    run_audit("t", now=datetime(2026, 7, 21, 6, 0, tzinfo=UTC), **world)

    add_invoice(
        conn,
        invoice_number="DP-Purchase-400",
        status="Paid",
        source_file="Purchase:400",
        amount_cents=425000,
    )
    night2 = run_audit("t", now=datetime(2026, 7, 23, 6, 0, tzinfo=UTC), **world)
    resolved_section = night2.report_text.split("## Resolved")[1]
    assert "Purchase:400" in resolved_section
    assert "matched no ledger row" in resolved_section
    assert "owner rejected" not in resolved_section
