"""Execution: the `apply` job files recorded invoices (no approval gate).

A successfully-extracted invoice auto-files: `apply` COPIES the landing original
into the filing tree (never a move: the original is the audit artifact). It is
shadow-aware (shadow = dry-run, writes nothing), guard-safe (a filing_dir inside
a protected surface is refused even live), and idempotent (a file is never
copied twice). 2026-06-29: the file-invoice approval gate was dropped, since
junk is kept out upstream and filing is not a money move.
"""

from __future__ import annotations

import hashlib

import pytest

from core.agents.ap.jobs import _file_invoice_copy, _filed_dest_name
from core.engine.guard import ProtectedSurfaceError, WriteGuard

_PARAMS = {
    "vendor": "Acme",
    "invoice_number": "777",
    "amount_cents": 50000,
    "invoice_date": "2026-07-01",
    "file": "Invoice_777.pdf",
}


def test_filed_dest_name_is_clean_and_single_segment():
    assert _filed_dest_name(_PARAMS) == "Acme - Inv 777 - $500.00.pdf"
    # a vendor with a slash must not become an accidental subfolder
    slashed = {**_PARAMS, "vendor": "Titan / Conveyors"}
    assert "/" not in _filed_dest_name(slashed)


def test_file_invoice_copy_copies_and_preserves_the_original(tmp_path):
    landing = tmp_path / "landing"
    landing.mkdir()
    src = landing / "Invoice_777.pdf"
    src.write_bytes(b"%PDF real invoice")
    filing = tmp_path / "Engine_Filed"

    dest, copied = _file_invoice_copy(
        _PARAMS, landing_dir=landing, filing_dir=filing, guard=WriteGuard([]), shadow=False
    )

    assert copied is True
    # Month folder by invoice date, mirroring the legacy 02_Invoices/YYYY-MM
    # layout (owner decision, 2026-07-09); a missing date files to _undated.
    assert dest == filing / "2026-07" / "Acme - Inv 777 - $500.00.pdf"
    assert dest.read_bytes() == b"%PDF real invoice"  # content intact
    assert src.exists()  # COPY, not move: original preserved


def test_missing_invoice_date_files_to_undated(tmp_path):
    landing = tmp_path / "landing"
    landing.mkdir()
    (landing / "Invoice_777.pdf").write_bytes(b"%PDF")
    filing = tmp_path / "Engine_Filed"
    undated = {**_PARAMS, "invoice_date": ""}

    dest, copied = _file_invoice_copy(
        undated, landing_dir=landing, filing_dir=filing, guard=WriteGuard([]), shadow=False
    )

    assert copied is True
    assert dest == filing / "_undated" / "Acme - Inv 777 - $500.00.pdf"


def test_file_invoice_copy_shadow_writes_nothing(tmp_path):
    landing = tmp_path / "landing"
    landing.mkdir()
    (landing / "Invoice_777.pdf").write_bytes(b"%PDF")
    filing = tmp_path / "Engine_Filed"

    dest, copied = _file_invoice_copy(
        _PARAMS, landing_dir=landing, filing_dir=filing, guard=WriteGuard([]), shadow=True
    )

    assert copied is False
    assert not dest.exists()  # dry-run computes the destination but writes nothing


def test_file_invoice_copy_refuses_a_protected_filing_dir(tmp_path):
    landing = tmp_path / "landing"
    landing.mkdir()
    (landing / "Invoice_777.pdf").write_bytes(b"%PDF")
    protected = tmp_path / "Financials"

    with pytest.raises(ProtectedSurfaceError):
        _file_invoice_copy(
            _PARAMS,
            landing_dir=landing,
            filing_dir=protected,
            guard=WriteGuard([protected]),
            shadow=False,
        )


def test_apply_job_files_recorded_invoices_then_is_idempotent(tmp_path):
    from core.agents.ap import store
    from core.engine.runner import resolve_ledger_root, run
    from core.ledger import Ledger

    landing = tmp_path / "landing"
    landing.mkdir()
    data = b"%PDF real invoice"
    (landing / "Invoice_777.pdf").write_bytes(data)
    md5 = hashlib.md5(data).hexdigest()
    filing = tmp_path / "Engine_Filed"
    ledger_dir = tmp_path / "data"
    params = {"landing_dir": str(landing), "filing_dir": str(filing)}

    # A recorded invoice, no approval: that is all apply needs now.
    root = resolve_ledger_root("demo", ledger_dir)
    with Ledger.open(root) as ledger:
        store.insert_invoice(
            ledger,
            tenant="demo",
            vendor="Acme",
            invoice_number="777",
            amount_cents=50000,
            invoice_date="2026-07-01",
            source_file="Invoice_777.pdf",
            source_md5=md5,
            shadow=True,
        )

    run("demo", "ap", "apply", shadow=False, params=params, ledger_dir=ledger_dir)

    dest = filing / "2026-07" / "Acme - Inv 777 - $500.00.pdf"
    assert dest.read_bytes() == data  # filed copy
    assert (landing / "Invoice_777.pdf").exists()  # original preserved
    with Ledger.open(root) as ledger:
        filed = [e for e in ledger.read_event_log() if e["event_type"] == "ap.invoice.filed"]
    assert len(filed) == 1 and filed[0]["payload"]["md5"] == md5

    # Re-run: same recorded set -> no-op, never a second filing.
    run("demo", "ap", "apply", shadow=False, params=params, ledger_dir=ledger_dir)
    with Ledger.open(root) as ledger:
        filed = [e for e in ledger.read_event_log() if e["event_type"] == "ap.invoice.filed"]
    assert len(filed) == 1


def test_month_template_routes_into_tenant_layout(tmp_path):
    """Owner decision 2026-07-10: filing goes into the tenant's real tree
    (02_Invoices/YYYY-MM/_approved), so the month layout is tenant config."""
    landing = tmp_path / "landing"
    landing.mkdir()
    (landing / "Invoice_777.pdf").write_bytes(b"%PDF")
    filing = tmp_path / "02_Invoices"

    dest, copied = _file_invoice_copy(
        _PARAMS,
        landing_dir=landing,
        filing_dir=filing,
        guard=WriteGuard([]),
        shadow=False,
        month_template="{month}/_approved",
    )

    assert copied is True
    assert dest == filing / "2026-07" / "_approved" / "Acme - Inv 777 - $500.00.pdf"


def test_existing_identical_file_is_adopted_not_recopied(tmp_path):
    """Filing into the shared tree must never clobber history: a same-named,
    same-content file already there (old-stack era) is adopted as the filed
    artifact; a different-content collision gets a suffixed name."""
    landing = tmp_path / "landing"
    landing.mkdir()
    src = landing / "Invoice_777.pdf"
    src.write_bytes(b"%PDF same content")
    filing = tmp_path / "02_Invoices"
    pre = filing / "2026-07" / "Acme - Inv 777 - $500.00.pdf"
    pre.parent.mkdir(parents=True)
    pre.write_bytes(b"%PDF same content")

    dest, copied = _file_invoice_copy(
        _PARAMS, landing_dir=landing, filing_dir=filing, guard=WriteGuard([]), shadow=False
    )
    assert dest == pre and copied is True  # adopted in place
    assert len(list(pre.parent.iterdir())) == 1  # no duplicate copy

    # different content: never overwritten, suffixed instead
    pre.write_bytes(b"%PDF DIFFERENT history file")
    src.write_bytes(b"%PDF new revision")
    dest2, copied2 = _file_invoice_copy(
        _PARAMS, landing_dir=landing, filing_dir=filing, guard=WriteGuard([]), shadow=False
    )
    assert copied2 is True
    assert dest2.name == "Acme - Inv 777 - $500.00 (2).pdf"
    assert pre.read_bytes() == b"%PDF DIFFERENT history file"


def test_cloud_only_destination_defers_the_row_files_the_rest_and_refires(tmp_path):
    """Honesty audit 2026-09-03, 02-F6 (S2). A same-named file already in the
    filing tree that is a cloud-only placeholder used to compare None == None
    against a cloud-only original and be ADOPTED as the filed artifact, with
    an ap.invoice.filed event for content never read. Now: cannot verify,
    deferred with an anomaly, the other rows still file, and the run
    re-fires once the placeholder materializes."""
    import errno
    from unittest.mock import patch

    from core.agents.ap import jobs as ap_jobs
    from core.agents.ap import store
    from core.engine.runner import resolve_ledger_root, run
    from core.ledger import Ledger

    landing = tmp_path / "landing"
    landing.mkdir()
    filing = tmp_path / "Engine_Filed"
    ledger_dir = tmp_path / "data"
    params = {"landing_dir": str(landing), "filing_dir": str(filing)}
    root = resolve_ledger_root("demo", ledger_dir)
    with Ledger.open(root) as ledger:
        for number, body in (("777", b"%PDF seven"), ("888", b"%PDF eight")):
            (landing / f"Invoice_{number}.pdf").write_bytes(body)
            store.insert_invoice(
                ledger,
                tenant="demo",
                vendor="Acme",
                invoice_number=number,
                amount_cents=50000,
                invoice_date="2026-07-01",
                source_file=f"Invoice_{number}.pdf",
                source_md5=hashlib.md5(body).hexdigest(),
                shadow=True,
            )
    # A legacy same-named file sits at 777's destination as a placeholder.
    placeholder = filing / "2026-07" / "Acme - Inv 777 - $500.00.pdf"
    placeholder.parent.mkdir(parents=True)
    placeholder.write_bytes(b"stand-in for a dataless placeholder")
    real = ap_jobs._file_bytes

    def _evicted(path):
        if path == placeholder:
            raise OSError(errno.EDEADLK, "Resource deadlock avoided", str(path))
        return real(path)

    with patch.object(ap_jobs, "_file_bytes", _evicted):
        result = run("demo", "ap", "apply", params=params, ledger_dir=ledger_dir)

    assert result.status == "ok"
    (anomaly,) = [a for a in result.anomalies if a.code == "ap.cloud_only_deferred"]
    assert "777" in anomaly.detail and "destination" in anomaly.detail
    assert "1 deferred" in result.summary
    assert (filing / "2026-07" / "Acme - Inv 888 - $500.00.pdf").read_bytes() == b"%PDF eight"
    assert placeholder.read_bytes() == b"stand-in for a dataless placeholder"  # untouched
    assert not (filing / "2026-07" / "Acme - Inv 777 - $500.00 (2).pdf").exists()
    with Ledger.open(root) as ledger:
        filed = [e for e in ledger.read_event_log() if e["event_type"] == "ap.invoice.filed"]
    assert [e["payload"]["invoice_number"] for e in filed] == ["888"]  # nothing claimed for 777

    # The placeholder materializes (different content): a fresh run, not a replay.
    again = run("demo", "ap", "apply", params=params, ledger_dir=ledger_dir)

    assert again.status == "ok"
    assert (filing / "2026-07" / "Acme - Inv 777 - $500.00 (2).pdf").read_bytes() == b"%PDF seven"
    with Ledger.open(root) as ledger:
        filed = [e for e in ledger.read_event_log() if e["event_type"] == "ap.invoice.filed"]
    assert sorted(e["payload"]["invoice_number"] for e in filed) == ["777", "888"]
