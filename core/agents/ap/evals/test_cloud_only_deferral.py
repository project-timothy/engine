"""Cloud-only placeholder files defer instead of killing the intake run.

2026-07-09 cutover-night incident: the first headless (launchd) run of the
live engine died with ``OSError(EDEADLK, "Resource deadlock avoided")``. The
landing folder lives on a cloud-sync drive with on-demand files; a
placeholder with no local content fails ``read()`` under launchd, and ``_intake_key`` md5s every
candidate up front, so one such file produced a zero-work run minutes after
old Otto was retired. Old Otto's fix for the same failure (73590dd) was never
ported. See docs/lessons.md, "A cloud placeholder is not here yet".

The contract these evals enforce:

- ``_md5`` maps EDEADLK (and only EDEADLK) to ``None``.
- One cloud-only candidate defers, visibly; every readable candidate is still
  processed in the same run.
- Deferral is self-healing, never a silent drop: once the file materializes,
  the intake key changes and the next run records the invoice.
"""

from __future__ import annotations

import errno
from pathlib import Path
from unittest.mock import patch

import pytest

from conftest import minimal_pdf
from core.agents.ap import jobs as ap_jobs
from core.agents.ap.jobs import _md5


def _raise_edeadlk_for(name: str):
    """A ``_file_bytes`` stand-in: EDEADLK for one file, real bytes otherwise."""
    real = ap_jobs._file_bytes

    def reader(path: Path) -> bytes:
        if path.name == name:
            raise OSError(errno.EDEADLK, "Resource deadlock avoided", str(path))
        return real(path)

    return reader


# ---------- _md5 contract ----------------------------------------------------


def test_md5_maps_cloud_only_edeadlk_to_none(tmp_path):
    f = tmp_path / "placeholder.pdf"
    f.write_bytes(minimal_pdf("stub"))
    with patch.object(ap_jobs, "_file_bytes", _raise_edeadlk_for("placeholder.pdf")):
        assert _md5(f) is None


def test_md5_propagates_other_oserrors(tmp_path):
    f = tmp_path / "denied.pdf"
    f.write_bytes(minimal_pdf("stub"))

    def reader(path: Path) -> bytes:
        raise OSError(errno.EACCES, "Permission denied", str(path))

    with patch.object(ap_jobs, "_file_bytes", reader):
        with pytest.raises(OSError) as exc:
            _md5(f)
        assert exc.value.errno == errno.EACCES


# ---------- intake behavior --------------------------------------------------


def _make_landing(tmp_path: Path) -> Path:
    landing = tmp_path / "landing"
    landing.mkdir()
    (landing / "Invoice_A.pdf").write_bytes(minimal_pdf("readable invoice"))
    (landing / "Invoice_A.pdf.extract.json").write_text(
        '{"doc_type": "invoice", "vendor_name": "Alpha Parts", "invoice_number": "A-1",'
        ' "amount": "10.00", "invoice_date": "2026-07-01", "confidence": 0.95}',
        encoding="utf-8",
    )
    (landing / "Invoice_B.pdf").write_bytes(minimal_pdf("cloud-only invoice"))
    (landing / "Invoice_B.pdf.extract.json").write_text(
        '{"doc_type": "invoice", "vendor_name": "Beta Freight", "invoice_number": "B-2",'
        ' "amount": "20.00", "invoice_date": "2026-07-02", "confidence": 0.95}',
        encoding="utf-8",
    )
    return landing


def _run_intake(landing: Path, ledger_dir: Path):
    from core.engine.runner import run

    return run(
        "demo",
        "ap",
        "intake",
        shadow=True,
        params={"landing_dir": str(landing), "extractor": "fixture"},
        ledger_dir=ledger_dir,
    )


def _events(ledger_dir: Path) -> list[dict]:
    from core.engine.runner import resolve_ledger_root
    from core.ledger import Ledger

    root = resolve_ledger_root("demo", ledger_dir)
    with Ledger.open(root) as ledger:
        return ledger.read_event_log()


def test_intake_defers_cloud_only_file_and_processes_the_rest(tmp_path):
    landing = _make_landing(tmp_path)

    with patch.object(ap_jobs, "_file_bytes", _raise_edeadlk_for("Invoice_B.pdf")):
        result = _run_intake(landing, tmp_path / "data")

    # The run survives: not an error, and the readable invoice was recorded.
    assert result.status == "ok"
    events = _events(tmp_path / "data")
    recorded = {e["payload"]["file"] for e in events if e["event_type"] == "ap.invoice.recorded"}
    assert "Invoice_A.pdf" in recorded
    assert "Invoice_B.pdf" not in recorded

    # The deferral is visible three ways: count, event, anomaly.
    assert "DEFERRED 1" in result.summary
    deferred = [e for e in events if e["event_type"] == "ap.intake.deferred"]
    assert [e["payload"]["file"] for e in deferred] == ["Invoice_B.pdf"]
    assert any(a.code == "ap.cloud_only_deferred" for a in result.anomalies)


def test_deferred_file_is_processed_after_it_materializes(tmp_path):
    """The self-heal: materialization changes the intake key and the next run
    records the deferred invoice. This is the assertion that deferral is not a
    silent drop (the failure mode that loses a real payable)."""
    landing = _make_landing(tmp_path)

    with patch.object(ap_jobs, "_file_bytes", _raise_edeadlk_for("Invoice_B.pdf")):
        first = _run_intake(landing, tmp_path / "data")
    assert first.status == "ok"

    # File Provider materializes the placeholder (patch gone: reads succeed).
    second = _run_intake(landing, tmp_path / "data")

    # Same folder contents, but the key must differ: a real run, not a noop.
    assert second.status == "ok"
    recorded = {
        e["payload"]["file"]
        for e in _events(tmp_path / "data")
        if e["event_type"] == "ap.invoice.recorded"
    }
    assert "Invoice_B.pdf" in recorded
