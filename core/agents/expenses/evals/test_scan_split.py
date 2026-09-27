"""Combined-scan splitter evals (issue #104, the first-live-drop pattern).

Contract under test:
- A receipt-suffixed PDF with more than one page is a suspected combined
  scan: the grouper proposes page groups, CODE validates coverage and writes
  one child PDF per group into the same drop location, the original moves to
  the drop tree's ``_originals/``, and the children flow through normal
  intake in the same run (one landed event per receipt, never one per scan).
- A multi-page SINGLE receipt (the hotel-folio shape) is left whole: one
  group covering every page records a scan-single verdict and the file
  intakes normally.
- An invalid or failed grouping never splits and never files: the scan is
  held in the drop tree with an anomaly, because filing a 17-receipt scan as
  one line is the exact failure this feature exists to prevent.
- Attribution inherits: children land in the original's folder and carry the
  original's project tag normalized to underscore form.
- Everything is evented (``expense.scan_split`` carries the page map and
  child hashes) and idempotent across re-runs; shadow mode writes nothing.
"""

from __future__ import annotations

import json

from pypdf import PdfReader, PdfWriter

from core.agents.expenses.schema import parse_amount_tag
from core.engine.runner import resolve_ledger_root, run
from core.ledger import Ledger

PERSON = "Pat Owner"


def _pdf_bytes(pages: int, width: int = 200) -> bytes:
    import io

    writer = PdfWriter()
    for i in range(pages):
        # distinct dimensions per page so single-page children never carry
        # identical bytes (identical children would rightly dedupe on hash)
        writer.add_blank_page(width=width + 10 * i, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _drop_pdf(tmp_path, name: str, pages: int, project: str | None = "P26_2001", width: int = 200):
    person_dir = tmp_path / "drop" / PERSON
    target = person_dir / project if project else person_dir
    target.mkdir(parents=True, exist_ok=True)
    path = target / name
    path.write_bytes(_pdf_bytes(pages, width))
    return path


def _groups_sidecar(path, groups):
    path.with_name(path.name + ".groups.json").write_text(json.dumps(groups))


def _params(tmp_path, **extra):
    return {
        "drop_dir": str(tmp_path / "drop"),
        "filing_dir": str(tmp_path / "filing"),
        "month": "2026-08",
        "extractor": "fixture",
        "grouper": "fixture",
        **extra,
    }


def _run(job, tmp_path, *, shadow=False, **extra):
    return run(
        "demo",
        "expenses",
        job,
        params=_params(tmp_path, **extra),
        ledger_dir=tmp_path / "ledger",
        shadow=shadow,
    )


def _events(tmp_path, event_type):
    root = resolve_ledger_root("demo", tmp_path / "ledger")
    with Ledger.open(root) as ledger:
        return [e for e in ledger.read_event_log() if e.get("event_type") == event_type]


GROUPS_3 = [
    {"pages": [1], "vendor": "Kala Coffeehouse", "amount": "108.37", "date": "2026-08-08"},
    {"pages": [2], "vendor": "Bucks Tavern", "amount": "29.25", "date": "2026-07-09"},
    {"pages": [3], "vendor": "Depot", "amount": "412.00", "date": "2026-08-01"},
]


def test_three_receipt_scan_splits_and_files_in_the_same_run(tmp_path):
    scan = _drop_pdf(tmp_path, "combined-scan.pdf", pages=3)
    _groups_sidecar(scan, GROUPS_3)

    result = _run("intake", tmp_path)

    assert result.status == "ok"
    (split,) = _events(tmp_path, "expense.scan_split")
    assert split["payload"]["pages"] == 3
    assert len(split["payload"]["children"]) == 3
    # the original is archived out of the drop, never re-intaken
    assert not scan.exists()
    originals = list((tmp_path / "drop" / "_originals").glob("*.pdf"))
    assert len(originals) == 1
    # children flowed through normal intake in the same run: one landed
    # event per RECEIPT, not one per scan
    landed = _events(tmp_path, "expense.receipt_landed")
    assert len(landed) == 3
    assert all(e["payload"]["project"] == "P26_2001" for e in landed)


def test_child_filenames_carry_the_extract_crosscheck_tags(tmp_path):
    scan = _drop_pdf(tmp_path, "combined-scan.pdf", pages=3)
    _groups_sidecar(scan, GROUPS_3)

    _run("intake", tmp_path)

    landed = _events(tmp_path, "expense.receipt_landed")
    amounts = sorted(parse_amount_tag(e["payload"]["file"]) for e in landed)
    assert amounts == [2925, 10837, 41200]


def test_receipt_spanning_two_pages_stays_one_child(tmp_path):
    scan = _drop_pdf(tmp_path, "hotel-and-lunch.pdf", pages=3)
    _groups_sidecar(
        scan,
        [
            {"pages": [1, 2], "vendor": "Grand Hotel", "amount": "512.30", "date": "2026-08-02"},
            {"pages": [3], "vendor": "Cafe", "amount": "18.00", "date": "2026-08-03"},
        ],
    )

    _run("intake", tmp_path)

    landed = _events(tmp_path, "expense.receipt_landed")
    assert len(landed) == 2
    filed_dir = tmp_path / "filing" / "2026-08" / "receipts" / PERSON / "P26_2001"
    by_pages = sorted(len(PdfReader(p).pages) for p in filed_dir.glob("*.pdf"))
    assert by_pages == [1, 2]


def test_single_page_pdf_is_untouched_by_the_splitter(tmp_path):
    _drop_pdf(tmp_path, "lunch.pdf", pages=1)

    _run("intake", tmp_path)

    assert _events(tmp_path, "expense.scan_split") == []
    assert _events(tmp_path, "expense.scan_single") == []
    assert len(_events(tmp_path, "expense.receipt_landed")) == 1


def test_multipage_single_receipt_is_left_whole(tmp_path):
    scan = _drop_pdf(tmp_path, "hotel-folio.pdf", pages=4)
    _groups_sidecar(
        scan,
        [{"pages": [1, 2, 3, 4], "vendor": "Grand Hotel", "amount": "812.44", "date": ""}],
    )

    _run("intake", tmp_path)

    assert _events(tmp_path, "expense.scan_split") == []
    (single,) = _events(tmp_path, "expense.scan_single")
    assert single["payload"]["pages"] == 4
    # intaken whole, in the same run
    (landed,) = _events(tmp_path, "expense.receipt_landed")
    assert landed["payload"]["file"] == "hotel-folio.pdf"
    assert scan.exists()  # intake copies; the drop original stays put


def test_invalid_grouping_holds_the_scan_and_files_nothing(tmp_path):
    scan = _drop_pdf(tmp_path, "combined-scan.pdf", pages=3)
    # page 3 uncovered: the proposal does not account for every page
    _groups_sidecar(scan, GROUPS_3[:2])

    result = _run("intake", tmp_path)

    assert _events(tmp_path, "expense.scan_split") == []
    assert _events(tmp_path, "expense.receipt_landed") == []
    assert scan.exists()
    assert any(a.code == "expenses.scan_split_invalid" for a in result.anomalies)


def test_grouper_failure_holds_the_scan_and_files_nothing(tmp_path):
    _drop_pdf(tmp_path, "combined-scan.pdf", pages=3)  # no sidecar: grouper raises

    result = _run("intake", tmp_path)

    assert _events(tmp_path, "expense.receipt_landed") == []
    assert any(a.code == "expenses.scan_split_failed" for a in result.anomalies)


def test_root_drop_scan_inherits_normalized_filename_tag(tmp_path):
    # The live 8/10 shape: one combined scan at the person-folder root,
    # project only in the filename, hyphen tag form.
    scan = _drop_pdf(tmp_path, "receipts P26-2034.pdf", pages=2, project=None)
    _groups_sidecar(
        scan,
        [
            {"pages": [1], "vendor": "Kala", "amount": "108.37", "date": ""},
            {"pages": [2], "vendor": "Bucks", "amount": "29.25", "date": ""},
        ],
    )

    _run("intake", tmp_path)

    landed = _events(tmp_path, "expense.receipt_landed")
    assert len(landed) == 2
    assert all(e["payload"]["project"] == "P26_2034" for e in landed)


def test_shadow_mode_splits_nothing(tmp_path):
    scan = _drop_pdf(tmp_path, "combined-scan.pdf", pages=3)
    _groups_sidecar(scan, GROUPS_3)

    result = _run("intake", tmp_path, shadow=True)

    assert scan.exists()
    assert not (tmp_path / "drop" / "_originals").exists()
    assert _events(tmp_path, "expense.scan_split") == []
    assert any("would split" in a for a in result.actions)


def test_split_is_idempotent_across_reruns(tmp_path):
    scan = _drop_pdf(tmp_path, "combined-scan.pdf", pages=3)
    _groups_sidecar(scan, GROUPS_3)
    _run("intake", tmp_path)

    _run("intake", tmp_path)

    assert len(_events(tmp_path, "expense.scan_split")) == 1
    assert len(_events(tmp_path, "expense.receipt_landed")) == 3
    assert len(list((tmp_path / "drop" / "_originals").glob("*.pdf"))) == 1


def test_single_verdict_is_not_reanalyzed_on_rerun(tmp_path):
    scan = _drop_pdf(tmp_path, "hotel-folio.pdf", pages=4)
    _groups_sidecar(
        scan,
        [{"pages": [1, 2, 3, 4], "vendor": "Grand Hotel", "amount": "812.44", "date": ""}],
    )
    _run("intake", tmp_path)
    # remove the sidecar: a re-analysis would now fail loudly; the recorded
    # verdict must make the second run skip the grouper entirely
    scan.with_name(scan.name + ".groups.json").unlink()
    _drop_pdf(tmp_path, "new-lunch.pdf", pages=1)  # change the tree so intake re-runs

    result = _run("intake", tmp_path)

    assert not any(a.code == "expenses.scan_split_failed" for a in result.anomalies)
    assert len(_events(tmp_path, "expense.scan_single")) == 1


# ---- honesty audit 2026-09-03: the split's memory and its move ordering -----


def test_redropped_split_original_is_held_never_filed_whole(tmp_path):
    """03-F3 (S2). The verdict memory skipped the grouper for a re-dropped
    archived original but did not hold it, so the intake loop filed the
    17-receipt scan WHOLE as one receipt with "filed 1" and no anomaly: the
    exact outcome the splitter exists to prevent. A sha carrying a
    scan_split verdict is now held with an anomaly, every run it sits
    there, and never lands."""
    import shutil

    scan = _drop_pdf(tmp_path, "combined-scan.pdf", pages=3)
    _groups_sidecar(scan, GROUPS_3)
    _run("intake", tmp_path)
    (archived,) = (tmp_path / "drop" / "_originals").glob("*.pdf")
    shutil.copy2(archived, scan)  # a sync client or a human restores it

    result = _run("intake", tmp_path)

    assert result.status == "ok"
    assert any(a.code == "expenses.split_original_redropped" for a in result.anomalies)
    assert "filed 0" in result.summary
    landed = _events(tmp_path, "expense.receipt_landed")
    assert len(landed) == 3  # the three children only, never the whole scan
    assert all(e["payload"]["file"] != "combined-scan.pdf" for e in landed)
    assert scan.exists()  # held in the drop tree, nothing moved
    assert len(list((tmp_path / "drop" / "_originals").glob("*.pdf"))) == 1


def _crash_after_first_split(tmp_path, monkeypatch):
    """Two scans; the run dies splitting the second, AFTER the first one's
    original moved to _originals/ (the 03-F9 shape). Returns the first scan."""
    from core.agents.expenses import jobs as exp_jobs

    first = _drop_pdf(tmp_path, "a-scan.pdf", pages=3)
    _groups_sidecar(first, GROUPS_3)
    second = _drop_pdf(tmp_path, "b-scan.pdf", pages=3, width=400)  # distinct bytes
    _groups_sidecar(second, GROUPS_3)
    real_split = exp_jobs.split_pdf
    calls = {"n": 0}

    def exploding_split(path, groups):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("pypdf choked on the second scan")
        return real_split(path, groups)

    monkeypatch.setattr(exp_jobs, "split_pdf", exploding_split)
    crashed = _run("intake", tmp_path)
    assert crashed.status == "error"
    assert not first.exists()  # a-scan moved before the run died
    assert _events(tmp_path, "expense.scan_split") == []  # the event never landed
    monkeypatch.setattr(exp_jobs, "split_pdf", real_split)
    return first


def test_archived_original_whose_run_died_is_healed_from_its_job_record(tmp_path, monkeypatch):
    """03-F9 (S3), closed 2026-09-10 (record-then-move). The split pass now
    writes a durable job record BEFORE the original moves, so the run that
    dies after the move leaves the record. The next run turns it into the
    lineage event (page map, child hashes) with an action, not an anomaly,
    and the archived original is never an orphan."""
    _crash_after_first_split(tmp_path, monkeypatch)

    result = _run("intake", tmp_path)

    assert result.status == "ok"
    assert not any(a.code == "expenses.split_lineage_missing" for a in result.anomalies)
    assert any("recorded split lineage for a-scan.pdf" in a for a in result.actions)
    # a-scan healed from the record + b-scan split this run
    split = _events(tmp_path, "expense.scan_split")
    assert sorted(e["payload"]["file"] for e in split) == ["a-scan.pdf", "b-scan.pdf"]
    assert len(_events(tmp_path, "expense.receipt_landed")) == 6


def test_archived_original_with_neither_record_nor_event_is_an_anomaly_not_silence(
    tmp_path, monkeypatch
):
    """The pre-record shape (a move from before job records existed, or a
    hand copy into _originals/) is still never silence: every _originals/
    entry with no record and no event is an anomaly on every run until a
    human clears it, and its children intake as plain receipts."""
    _crash_after_first_split(tmp_path, monkeypatch)
    # Erase the record the dying run left, leaving the pre-2026-09-10 shape.
    from core.engine.runner import resolve_ledger_root
    from core.ledger import Ledger

    with Ledger.open(resolve_ledger_root("demo", tmp_path / "ledger")) as ledger:
        ledger.conn.execute("DELETE FROM job_records")
        ledger.conn.commit()

    result = _run("intake", tmp_path)

    assert result.status == "ok"
    orphan = [a for a in result.anomalies if a.code == "expenses.split_lineage_missing"]
    assert len(orphan) == 1
    assert "a-scan.pdf" in orphan[0].detail
    # b-scan split normally this run; a-scan's children intaken as plain receipts
    assert len(_events(tmp_path, "expense.scan_split")) == 1
    assert len(_events(tmp_path, "expense.receipt_landed")) == 6


def test_half_moved_original_is_held_not_resplit(tmp_path, monkeypatch):
    """03-F9 (S3), the cross-device shape: the copy half of the move landed
    in _originals/ and the unlink failed, so the original sits in BOTH
    places with no verdict. The next run used to split it again into a
    second child set and a second _originals copy. Now the drop copy is
    held and the orphan is flagged; nothing is split twice."""
    import shutil

    from core.agents.expenses import jobs as exp_jobs

    scan = _drop_pdf(tmp_path, "combined-scan.pdf", pages=3)
    _groups_sidecar(scan, GROUPS_3)
    real_move = shutil.move

    def half_move(src, dst):
        shutil.copy2(src, dst)
        raise OSError("unlink failed after the copy")

    monkeypatch.setattr(exp_jobs.shutil, "move", half_move)
    crashed = _run("intake", tmp_path)
    assert crashed.status == "error"
    assert scan.exists()
    assert len(list((tmp_path / "drop" / "_originals").glob("*.pdf"))) == 1

    monkeypatch.setattr(exp_jobs.shutil, "move", real_move)
    result = _run("intake", tmp_path)

    assert result.status == "ok"
    # Since 2026-09-10 the split is on the record before the move, so the
    # drop-tree leftover reads as a recorded split's original: held under
    # the 03-F3 rule (never filed whole), never split twice, and the lineage
    # event is healed from the record.
    assert any(a.code == "expenses.split_original_redropped" for a in result.anomalies)
    assert scan.exists()  # held, never re-split
    (split,) = _events(tmp_path, "expense.scan_split")
    assert split["payload"]["file"] == "combined-scan.pdf"
    children = [p for p in scan.parent.glob("*.pdf") if p != scan]
    assert len(children) == 3  # one child set, not two
    assert len(list((tmp_path / "drop" / "_originals").glob("*.pdf"))) == 1
    assert len(_events(tmp_path, "expense.receipt_landed")) == 3
