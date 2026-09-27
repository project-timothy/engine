"""Janitor: aged landing-folder files are organized, never deleted.

Owner decision 2026-07-09 (cutover day): the landing folder had accumulated
609 raw email attachments since April; unbrowsable, and every non-invoice
stayed at top level forever. The janitor moves settled files older than a
cutoff into ``_archive/YYYY-MM`` (month of file mtime) WITHIN the landing
tree. This amends the original "originals are never moved" rule to "originals
never leave the landing tree, and are never deleted"; the audit artifact
survives, organized. Intake ignores subfolders, so archived files simply stop
being candidates; ledger md5 dedup still recognizes a re-send of an archived
original.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from conftest import minimal_pdf
from core.engine.guard import ProtectedSurfaceError, WriteGuard


def _aged(path: Path, days: int) -> None:
    """Backdate a file's mtime by ``days``."""
    t = time.time() - days * 86400
    os.utime(path, (t, t))


def _make_landing(tmp_path: Path) -> Path:
    landing = tmp_path / "landing"
    landing.mkdir()
    old_pdf = landing / "ancient-invoice.pdf"
    old_pdf.write_bytes(minimal_pdf("old"))
    _aged(old_pdf, 90)
    old_png = landing / "signature-logo.png"
    old_png.write_bytes(b"png junk")
    _aged(old_png, 45)
    fresh = landing / "fresh-invoice.pdf"
    fresh.write_bytes(minimal_pdf("fresh"))  # today; must not move
    sub = landing / "_skipped"
    sub.mkdir()
    (sub / "already-triaged.pdf").write_bytes(minimal_pdf("pdf"))
    _aged(sub / "already-triaged.pdf", 90)  # in a subfolder; janitor never descends
    return landing


def _run_janitor(landing: Path, ledger_dir: Path, *, shadow: bool = False, days: str = "30"):
    from core.engine.runner import run

    return run(
        "demo",
        "ap",
        "janitor",
        shadow=shadow,
        params={"landing_dir": str(landing), "days": days},
        ledger_dir=ledger_dir,
    )


def test_janitor_archives_aged_files_by_mtime_month(tmp_path):
    landing = _make_landing(tmp_path)

    result = _run_janitor(landing, tmp_path / "data")

    assert result.status == "ok"
    archive = landing / "_archive"
    moved = sorted(p.relative_to(archive).as_posix() for p in archive.rglob("*") if p.is_file())
    assert len(moved) == 2
    # month folders derive from each file's own mtime
    assert any(p.endswith("/ancient-invoice.pdf") for p in moved)
    assert any(p.endswith("/signature-logo.png") for p in moved)
    # aged originals left the top level but never the landing tree
    assert not (landing / "ancient-invoice.pdf").exists()
    assert not (landing / "signature-logo.png").exists()
    # fresh files and subfolders are untouched
    assert (landing / "fresh-invoice.pdf").exists()
    assert (landing / "_skipped" / "already-triaged.pdf").exists()


def test_janitor_shadow_is_a_dry_run(tmp_path):
    landing = _make_landing(tmp_path)

    result = _run_janitor(landing, tmp_path / "data", shadow=True)

    assert result.status == "ok"
    assert not (landing / "_archive").exists()
    assert (landing / "ancient-invoice.pdf").exists()
    # the dry-run still reports what it would do
    assert "2" in result.summary


def test_janitor_rerun_archives_nothing_then_noops(tmp_path):
    """Runner noop semantics: the key encodes the current aged set, so the
    run after an archive pass sees a new (empty) set once, archives nothing,
    and every identical run after that is a straight noop."""
    landing = _make_landing(tmp_path)

    _run_janitor(landing, tmp_path / "data")
    second = _run_janitor(landing, tmp_path / "data")
    assert second.status == "ok"
    assert not second.actions  # nothing left to archive
    third = _run_janitor(landing, tmp_path / "data")
    assert third.status == "noop"


def test_janitor_never_overwrites_on_name_collision(tmp_path):
    landing = tmp_path / "landing"
    landing.mkdir()
    f = landing / "invoice.pdf"
    f.write_bytes(minimal_pdf("new content"))
    _aged(f, 60)
    # a same-named, different-content file already sits in that archive month
    month = time.strftime("%Y-%m", time.localtime(time.time() - 60 * 86400))
    pre = landing / "_archive" / month / "invoice.pdf"
    pre.parent.mkdir(parents=True)
    pre.write_bytes(minimal_pdf("earlier different file"))

    result = _run_janitor(landing, tmp_path / "data")

    assert result.status == "ok"
    assert pre.read_bytes() == minimal_pdf("earlier different file")  # untouched
    siblings = sorted(p.name for p in pre.parent.iterdir())
    assert len(siblings) == 2  # the new file arrived under a suffixed name


def test_archive_move_respects_the_write_guard(tmp_path):
    from core.agents.ap.jobs import _archive_move

    landing = tmp_path / "landing"
    landing.mkdir()
    f = landing / "invoice.pdf"
    f.write_bytes(minimal_pdf("pdf"))
    _aged(f, 60)

    guard = WriteGuard([landing])  # protected, and NO carve-out for _archive
    with pytest.raises(ProtectedSurfaceError):
        _archive_move(f, landing / "_archive" / "2026-05", guard=guard, shadow=False)


def test_janitor_never_archives_files_still_in_flight(tmp_path):
    """A file referenced by an unfiled invoice row or a pending approval is
    work in progress: apply/identify resolve it by its landing name, so the
    janitor must leave it at top level no matter how old it is."""
    from core.agents.ap import store
    from core.engine.runner import resolve_ledger_root
    from core.ledger import Ledger

    landing = tmp_path / "landing"
    landing.mkdir()
    unfiled = landing / "unfiled-invoice.pdf"
    unfiled.write_bytes(minimal_pdf("recorded but not yet filed"))
    _aged(unfiled, 90)
    queued = landing / "awaiting-decision.pdf"
    queued.write_bytes(minimal_pdf("pending approval"))
    _aged(queued, 90)
    plain_old = landing / "settled-junk.png"
    plain_old.write_bytes(b"png")
    _aged(plain_old, 90)

    ledger_dir = tmp_path / "data"
    # A first (dry-run) pass exists to own run id 1: approval_queue.run_id is a
    # real FK. Distinct `days` so its key never collides with the live run's.
    _run_janitor(landing, ledger_dir, shadow=True, days="60")
    root = resolve_ledger_root("demo", ledger_dir)
    with Ledger.open(root) as ledger:
        store.insert_invoice(
            ledger,
            tenant="demo",
            vendor="Acme",
            invoice_number="900",
            amount_cents=1000,
            source_file="unfiled-invoice.pdf",
            source_md5="fake-md5-unfiled",
            shadow=True,
        )  # recorded, never filed: no ap.invoice.filed event
        ledger.enqueue_approval(
            idempotency_key="eval:pending:awaiting-decision",
            run_id=1,
            tenant="demo",
            agent="ap",
            action_type="ap.new_vendor_decision",
            params={"file": "awaiting-decision.pdf", "extracted_vendor": "X"},
        )

    result = _run_janitor(landing, ledger_dir)

    assert result.status == "ok"
    assert unfiled.exists()  # in-flight: stays
    assert queued.exists()  # in-flight: stays
    assert not plain_old.exists()  # settled junk: archived


def test_janitor_archives_deterministic_junk_immediately(tmp_path):
    """Owner feedback, cutover day: ~80% of the landing top level was junk the
    engine had already judged (signature graphics, decks, drawings). Junk by
    deterministic rule (inline-graphic name or non-candidate suffix) archives
    the day it arrives; a real invoice candidate the engine has not settled
    stays put whatever its type."""
    landing = tmp_path / "landing"
    landing.mkdir()
    (landing / "image001.png").write_bytes(b"signature graphic")  # fresh today
    (landing / "vendor-deck.pptx").write_bytes(b"deck")  # fresh today
    (landing / "scan-receipt.pdf").write_bytes(minimal_pdf("unprocessed candidate"))

    result = _run_janitor(landing, tmp_path / "data")

    assert result.status == "ok"
    archived = {p.name for p in (landing / "_archive").rglob("*") if p.is_file()}
    assert archived == {"image001.png", "vendor-deck.pptx"}
    assert (landing / "scan-receipt.pdf").exists()  # unsettled candidate stays


def test_janitor_archives_ledger_settled_files_immediately(tmp_path):
    """A recorded AND filed invoice is settled: its raw landing original
    archives the same day (the clean filed copy is the human artifact)."""
    from core.engine.runner import run

    landing = tmp_path / "landing"
    landing.mkdir()
    (landing / "Invoice_777.pdf").write_bytes(minimal_pdf("real invoice"))  # fresh
    (landing / "Invoice_777.pdf.extract.json").write_text(
        '{"doc_type": "invoice", "vendor_name": "Alpha Parts", "invoice_number": "777",'
        ' "amount": "10.00", "invoice_date": "2026-07-01", "confidence": 0.95}',
        encoding="utf-8",
    )
    ledger_dir = tmp_path / "data"
    filing = tmp_path / "filing"
    run(
        "demo",
        "ap",
        "intake",
        shadow=False,
        params={"landing_dir": str(landing), "extractor": "fixture"},
        ledger_dir=ledger_dir,
    )
    run(
        "demo",
        "ap",
        "apply",
        shadow=False,
        params={"landing_dir": str(landing), "filing_dir": str(filing)},
        ledger_dir=ledger_dir,
    )

    result = _run_janitor(landing, ledger_dir)

    assert result.status == "ok"
    assert not (landing / "Invoice_777.pdf").exists()  # settled: archived
    archived = [p.name for p in (landing / "_archive").rglob("*.pdf")]
    assert archived == ["Invoice_777.pdf"]
    # the sidecar is fixture metadata, never touched
    assert (landing / "Invoice_777.pdf.extract.json").exists()
    # and the filed clean copy is untouched
    assert list((filing / "2026-07").glob("*.pdf"))


def test_janitor_guard_refusal_is_a_job_error_not_a_green_run(tmp_path, monkeypatch):
    """Honesty audit 2026-09-03, 02-F8 (S4): ProtectedSurfaceError is a
    PermissionError, so the archive move's OSError net swallowed a guard
    refusal into a per-file `ap.janitor_move_failed` and the run reported
    `ok ... archived 0` every day. A guard refusal never clears on a retry;
    it is a configuration fault and the run says so."""
    from core.engine import runner

    landing = _make_landing(tmp_path)
    # The tenant's guard now protects the landing tree with NO _archive carve-out.
    monkeypatch.setattr(runner, "WriteGuard", lambda roots, allowed=None: WriteGuard([landing]))

    result = _run_janitor(landing, tmp_path / "data")

    assert result.status == "error"
    assert any("ProtectedSurfaceError" in a.detail for a in result.anomalies)
    assert not any(a.code == "ap.janitor_move_failed" for a in result.anomalies)
    assert (landing / "ancient-invoice.pdf").exists()  # nothing moved
    assert not (landing / "_archive").exists()


def test_janitor_archive_whose_run_died_is_healed_from_its_job_record(tmp_path, monkeypatch):
    """03-F9 (S3), closed 2026-09-10 (record-then-move). The janitor writes
    a durable job record naming the destination BEFORE the move, so a run
    that dies after the move leaves the record; the next run emits the
    ap.landing.archived event from it (an action), and the archived file
    is on the mail log exactly once."""
    import shutil

    from core.agents.ap import jobs as ap_jobs
    from core.engine.runner import resolve_ledger_root
    from core.ledger import Ledger

    landing = _make_landing(tmp_path)
    real_move = shutil.move

    def move_then_die(src, dst):
        real_move(src, dst)
        raise RuntimeError("died after the move")

    monkeypatch.setattr(ap_jobs.shutil, "move", move_then_die)
    crashed = _run_janitor(landing, tmp_path)
    assert crashed.status == "error"
    assert not (landing / "ancient-invoice.pdf").exists()  # moved before the run died
    root = resolve_ledger_root("demo", tmp_path)
    with Ledger.open(root) as ledger:
        assert [
            e for e in ledger.read_event_log() if e["event_type"] == "ap.landing.archived"
        ] == []
        (rec,) = ledger.job_records(tenant="demo", record_type=ap_jobs.JANITOR_RECORD)
    assert rec["payload"]["file"] == "ancient-invoice.pdf"

    monkeypatch.setattr(ap_jobs.shutil, "move", real_move)
    healed = _run_janitor(landing, tmp_path)

    assert healed.status == "ok"
    assert any(
        "recorded the archive of ancient-invoice.pdf from its job record" in a
        for a in healed.actions
    )
    with Ledger.open(root) as ledger:
        archived = [e for e in ledger.read_event_log() if e["event_type"] == "ap.landing.archived"]
    names = sorted(e["payload"]["file"] for e in archived)
    assert names.count("ancient-invoice.pdf") == 1
    assert "signature-logo.png" in names  # the second aged file archived normally this run
