"""W-9 intake routing (docs/w9-1099-design.md build 3, owner go 2026-09-01).

An inbound W-9 is recognized DETERMINISTICALLY (filename tokens, then PDF
text-layer markers) before any model call — the form carries a TIN and the
invariant is absolute: no TIN ever reaches a model, a card, an event, a log,
or this repo. Detection routes the file (``ap.intake.routed.w9``) and parks
ONE card proposing the registry flip; the approved card's execution copies
the archived original into the Vendor W-9s folder under the naming
convention and emits the registry diff as an event — the engine NEVER
writes vendors.toml (the diff-emitting variant, decision log 2026-09-01).

Rejection here means "not a W-9 / do not file" — a final answer — so the
stable content-keyed card is CORRECT under the #143 dedup (contrast issue
#161, where rejection meant "ask again" and needed a fresh key).
"""

from __future__ import annotations

import datetime
import json
import re
import shutil
from pathlib import Path

from core.agents.ap import w9
from core.agents.ap.w9 import _looks_like_w9_name, _looks_like_w9_text, _vendor_slug
from core.engine.runner import resolve_ledger_root, run
from core.ledger import Ledger

# ---- deterministic detection -------------------------------------------------


def test_filename_detection_matches_the_shapes_seen_live():
    assert _looks_like_w9_name("Alpha Parts W-9.pdf")
    assert _looks_like_w9_name("alpha-parts-w9.pdf")
    assert _looks_like_w9_name("fw9.pdf")
    assert _looks_like_w9_name("Vendor_W9_2026.pdf")
    assert _looks_like_w9_name("w-9 Smith, Pat.pdf")
    assert _looks_like_w9_name("W9.jpg")  # image scans count; text layer never needed


def test_filename_detection_never_matches_ordinary_documents():
    assert not _looks_like_w9_name("Invoice_10089_from_Alpha_Parts.pdf")
    assert not _looks_like_w9_name("BMW9000_manual.pdf")  # token boundary
    assert not _looks_like_w9_name("w94_report.pdf")  # digit after the 9
    assert not _looks_like_w9_name("PO-777-123_v1_20260901.pdf")
    assert not _looks_like_w9_name("statement_2026-08.pdf")


def test_text_detection_needs_the_form_markers():
    assert _looks_like_w9_text("Form W-9 (Rev. March 2024) Request for Taxpayer")
    assert _looks_like_w9_text("REQUEST FOR TAXPAYER\nIDENTIFICATION NUMBER AND CERTIFICATION")
    assert not _looks_like_w9_text("Invoice 123 — please remit payment")
    assert not _looks_like_w9_text("")


def test_vendor_slug_matches_the_folder_naming_convention():
    assert _vendor_slug("Alpha Parts") == "Alpha_Parts"
    assert _vendor_slug("Beta Freight, LLC") == "Beta_Freight_LLC"
    assert _vendor_slug("Gamma  &  Sons") == "Gamma_Sons"


# ---- run-level: detect, route, park ------------------------------------------


def _run_intake(tmp_path, landing, w9_folder, **extra_params):
    params = {
        "landing_dir": str(landing),
        "extractor": "fixture",
        "w9_folder": str(w9_folder),
    }
    params.update(extra_params)
    return run("demo", "ap", "intake", params=params, ledger_dir=tmp_path / "data")


def _world(tmp_path):
    landing = tmp_path / "landing"
    landing.mkdir()
    w9_folder = tmp_path / "w9-folder"
    w9_folder.mkdir()
    return landing, w9_folder


def _events(tmp_path, event_type=None):
    root = resolve_ledger_root("demo", tmp_path / "data")
    with Ledger.open(root) as ledger:
        events = ledger.read_event_log()
    if event_type is None:
        return events
    return [e for e in events if e["event_type"] == event_type]


def _cards(tmp_path, action_type):
    root = resolve_ledger_root("demo", tmp_path / "data")
    with Ledger.open(root) as ledger:
        rows = ledger.conn.execute(
            "SELECT id, status, params_json FROM approval_queue WHERE action_type = ?",
            (action_type,),
        ).fetchall()
        return [
            {"id": r["id"], "status": r["status"], "params": json.loads(r["params_json"])}
            for r in rows
        ]


def test_w9_routes_before_the_extractor_and_parks_one_card(tmp_path):
    landing, w9_folder = _world(tmp_path)
    # No .extract.json sidecar: the fixture extractor would route this file
    # as doc_type "unknown" if it were ever called — the absence of that
    # event is the tripwire proving the deterministic gate ran first and no
    # model saw the form.
    (landing / "Alpha Parts W-9.pdf").write_bytes(b"%PDF stub with no sidecar")

    result = _run_intake(tmp_path, landing, w9_folder)
    assert result.status in ("ok", "needs_approval")

    routed = _events(tmp_path, "ap.intake.routed.w9")
    assert len(routed) == 1
    assert routed[0]["payload"]["file"] == "Alpha Parts W-9.pdf"
    assert _events(tmp_path, "ap.intake.routed.unknown") == []  # the extractor never ran

    cards = _cards(tmp_path, w9.W9_CARD)
    assert len(cards) == 1
    assert cards[0]["params"]["vendor"] == "Alpha Parts"  # subject-alias match
    assert cards[0]["params"]["file"] == "Alpha Parts W-9.pdf"

    # Same landing state, same card: a re-run parks nothing new.
    _run_intake(tmp_path, landing, w9_folder)
    assert len(_cards(tmp_path, w9.W9_CARD)) == 1


def test_unknown_vendor_parks_a_card_that_asks_instead_of_guessing(tmp_path):
    landing, w9_folder = _world(tmp_path)
    (landing / "fw9 signed.pdf").write_bytes(b"%PDF anonymous form")
    _run_intake(tmp_path, landing, w9_folder)
    cards = _cards(tmp_path, w9.W9_CARD)
    assert len(cards) == 1
    assert cards[0]["params"]["vendor"] == "unknown"


def test_text_marker_detection_catches_a_w9_with_a_neutral_filename(tmp_path, monkeypatch):
    landing, w9_folder = _world(tmp_path)
    (landing / "scan_0042.pdf").write_bytes(b"%PDF neutral name")
    monkeypatch.setattr(
        w9, "_read_text", lambda path: "Form W-9\nRequest for Taxpayer\nTIN 12-3456789"
    )
    _run_intake(tmp_path, landing, w9_folder)
    assert len(_events(tmp_path, "ap.intake.routed.w9")) == 1


# ---- execution: approved card copies the file and emits the diff -------------


def _approve(tmp_path, card_id, **overrides):
    root = resolve_ledger_root("demo", tmp_path / "data")
    with Ledger.open(root) as ledger:
        ledger.resolve_approval("demo", card_id, "approved", param_overrides=overrides)


def test_approved_card_files_the_form_and_emits_the_registry_diff(tmp_path):
    landing, w9_folder = _world(tmp_path)
    source = landing / "Alpha Parts W-9.pdf"
    source.write_bytes(b"%PDF form bytes")
    _run_intake(tmp_path, landing, w9_folder)
    card = _cards(tmp_path, w9.W9_CARD)[0]

    _approve(tmp_path, card["id"], classification="s-corp")
    _run_intake(tmp_path, landing, w9_folder)

    year = datetime.date.today().year
    dest = w9_folder / f"Alpha_Parts_W9_{year}.pdf"
    assert dest.read_bytes() == b"%PDF form bytes"
    assert source.exists(), "the landing original is the audit artifact, never moved"

    filed = _events(tmp_path, w9.W9_FILED_EVENT)
    assert len(filed) == 1
    payload = filed[0]["payload"]
    assert payload["vendor"] == "Alpha Parts"
    assert payload["classification"] == "s-corp"
    assert "w9 = true" in payload["diff"]
    assert str(dest) in payload["diff"]

    # Idempotent: a third run neither re-copies nor re-emits.
    _run_intake(tmp_path, landing, w9_folder)
    assert len(_events(tmp_path, w9.W9_FILED_EVENT)) == 1


def test_owner_corrects_the_vendor_at_approval(tmp_path):
    landing, w9_folder = _world(tmp_path)
    (landing / "fw9 signed.pdf").write_bytes(b"%PDF anonymous form")
    _run_intake(tmp_path, landing, w9_folder)
    card = _cards(tmp_path, w9.W9_CARD)[0]
    assert card["params"]["vendor"] == "unknown"

    _approve(tmp_path, card["id"], vendor="Beta Freight", classification="individual")
    _run_intake(tmp_path, landing, w9_folder)

    year = datetime.date.today().year
    assert (w9_folder / f"Beta_Freight_W9_{year}.pdf").is_file()
    payload = _events(tmp_path, w9.W9_FILED_EVENT)[0]["payload"]
    assert payload["vendor"] == "Beta Freight"


def test_rejected_card_files_nothing_and_stays_final(tmp_path):
    """Rejection means "not a W-9 / do not file" — a final answer, so the
    stable content key correctly re-parks nothing (the #143 memory, used
    right; contrast issue #161 where rejection meant re-ask)."""
    landing, w9_folder = _world(tmp_path)
    (landing / "Alpha Parts W-9.pdf").write_bytes(b"%PDF form bytes")
    _run_intake(tmp_path, landing, w9_folder)
    card = _cards(tmp_path, w9.W9_CARD)[0]

    root = resolve_ledger_root("demo", tmp_path / "data")
    with Ledger.open(root) as ledger:
        ledger.resolve_approval("demo", card["id"], "rejected")

    _run_intake(tmp_path, landing, w9_folder)
    assert list(w9_folder.iterdir()) == []
    assert _events(tmp_path, w9.W9_FILED_EVENT) == []
    assert len(_cards(tmp_path, w9.W9_CARD)) == 1  # still just the rejected card


# ---- the TIN invariant -------------------------------------------------------


def test_no_tin_shaped_content_ever_leaves_the_document(tmp_path, monkeypatch):
    """The form's text (which contains the TIN) may be read in memory for the
    marker check, but nothing TIN-shaped reaches a card, an event, or a run
    summary. The fake TIN below must appear NOWHERE in the ledger."""
    landing, w9_folder = _world(tmp_path)
    (landing / "scan_0042.pdf").write_bytes(b"%PDF neutral name")
    fake_tin = "98-7654321"
    monkeypatch.setattr(
        w9, "_read_text", lambda path: f"Form W-9 Request for Taxpayer\nEIN {fake_tin}"
    )
    _run_intake(tmp_path, landing, w9_folder)
    card = _cards(tmp_path, w9.W9_CARD)[0]
    _approve(tmp_path, card["id"], vendor="Alpha Parts", classification="c-corp")
    _run_intake(tmp_path, landing, w9_folder)

    for event in _events(tmp_path):
        assert fake_tin not in json.dumps(event)
    for parked in _cards(tmp_path, w9.W9_CARD):
        assert fake_tin not in json.dumps(parked["params"])


def _file_and_approve(tmp_path, landing, w9_folder, name, body):
    (landing / name).write_bytes(body)
    _run_intake(tmp_path, landing, w9_folder)
    pending = [c for c in _cards(tmp_path, w9.W9_CARD) if c["status"] == "pending"]
    _approve(tmp_path, pending[-1]["id"], classification="s-corp")
    return _run_intake(tmp_path, landing, w9_folder)


def test_three_distinct_forms_for_one_vendor_file_as_three_files(tmp_path):
    """Honesty audit 2026-09-03, 02-F5 (S2). The suffix was one deep and the
    copy overwrote: a third distinct form clobbered `_2` silently. Now the
    filing rule loops and nothing is ever overwritten."""
    landing, w9_folder = _world(tmp_path)
    year = datetime.date.today().year
    bodies = [b"%PDF form one", b"%PDF form two", b"%PDF form three"]
    for i, body in enumerate(bodies, start=1):
        _file_and_approve(tmp_path, landing, w9_folder, f"Alpha Parts W-9 v{i}.pdf", body)

    names = sorted(p.name for p in w9_folder.iterdir())
    assert names == [
        f"Alpha_Parts_W9_{year}.pdf",
        f"Alpha_Parts_W9_{year}_2.pdf",
        f"Alpha_Parts_W9_{year}_3.pdf",
    ]
    assert sorted(p.read_bytes() for p in w9_folder.iterdir()) == sorted(bodies)
    assert len(_events(tmp_path, w9.W9_FILED_EVENT)) == 3


def test_evicted_destination_defers_instead_of_filing_a_duplicate(tmp_path, monkeypatch):
    """02-F5, second shape: a cloud-only file already at the destination used
    to hash None, read as "different", and land a `_2` copy of an identical
    form. Now: cannot verify, deferred with an anomaly, no event, retry."""
    import errno

    from core.agents.ap import jobs as ap_jobs

    landing, w9_folder = _world(tmp_path)
    year = datetime.date.today().year
    dest = w9_folder / f"Alpha_Parts_W9_{year}.pdf"
    dest.write_bytes(b"placeholder stand-in")
    real = ap_jobs._file_bytes

    def _evicted(path):
        if path == dest:
            raise OSError(errno.EDEADLK, "Resource deadlock avoided", str(path))
        return real(path)

    monkeypatch.setattr(ap_jobs, "_file_bytes", _evicted)
    result = _file_and_approve(tmp_path, landing, w9_folder, "Alpha Parts W-9.pdf", b"%PDF form")

    assert any(a.code == "ap.w9_copy_deferred" for a in result.anomalies)
    assert [p.name for p in w9_folder.iterdir()] == [dest.name]  # no _2 on a guess
    assert _events(tmp_path, w9.W9_FILED_EVENT) == []


# ---- honesty audit 2026-09-03: the lane tells the truth about itself -------


def _run_intake_no_folder(tmp_path, monkeypatch, landing):
    """Intake with ``[w9].folder`` unset and no ``--param w9_folder``:
    detection still runs, filing is off.

    The unset config is built here rather than borrowed from the demo
    tenant: since row 7.19 the demo is rendered from the archetype template
    and DOES carry a W-9 folder, so the condition this eval is about has to
    be constructed. A copy of the demo with the one key blanked keeps every
    other value identical, and the same slug keeps the ledger and the card
    from the first run in scope.
    """
    root = tmp_path / "tenants-no-w9"
    (root / "demo").mkdir(parents=True, exist_ok=True)
    demo_dir = Path(__file__).resolve().parents[4] / "tenants" / "demo"
    text = (demo_dir / "tenant.toml").read_text(encoding="utf-8")
    blanked, count = re.subn(r'(?m)^folder = ".*"$', 'folder = ""', text)
    assert count == 1, "[w9].folder is the only folder key in the demo config"
    (root / "demo" / "tenant.toml").write_text(blanked, encoding="utf-8")
    shutil.copy2(demo_dir / "vendors.toml", root / "demo" / "vendors.toml")
    monkeypatch.setenv("ENGINE_TENANTS_ROOT", str(root))
    try:
        return run(
            "demo",
            "ap",
            "intake",
            params={"landing_dir": str(landing), "extractor": "fixture"},
            ledger_dir=tmp_path / "data",
        )
    finally:
        monkeypatch.delenv("ENGINE_TENANTS_ROOT", raising=False)


def test_unset_folder_routes_and_names_the_form_but_parks_no_card(tmp_path, monkeypatch):
    """02-F4 (S2): with the folder unset the lane used to detect, route, and
    park a card it could never execute. The TIN invariant outranks the
    config, so detection still runs and the form is still routed away from
    the extractor; the folder gates FILING: no card (a card that can never
    execute is the lie), and an anomaly says what to configure."""
    from core.agents.ap import extraction

    landing, _ = _world(tmp_path)
    (landing / "Alpha Parts W-9.pdf").write_bytes(b"%PDF stub with no sidecar")
    seen: list[str] = []
    real_extract = extraction.FixtureExtractor.extract
    monkeypatch.setattr(
        extraction.FixtureExtractor,
        "extract",
        lambda self, path: (seen.append(path.name), real_extract(self, path))[1],
    )

    result = _run_intake_no_folder(tmp_path, monkeypatch, landing)

    assert result.status != "error", result.summary
    assert seen == [], "the extractor model saw a detected W-9"
    routed = _events(tmp_path, "ap.intake.routed.w9")
    assert [e["payload"]["file"] for e in routed] == ["Alpha Parts W-9.pdf"]
    assert "ROUTED 1" in result.summary
    assert _cards(tmp_path, w9.W9_CARD) == []
    unset = [a for a in result.anomalies if a.code == "ap.w9_folder_unset"]
    assert len(unset) == 1
    assert "Alpha Parts W-9.pdf" in unset[0].detail
    assert "configure [w9].folder" in unset[0].detail


def test_approved_card_with_the_folder_unset_records_an_anomaly_not_silence(tmp_path, monkeypatch):
    """02-F4, second shape: a card approved while the folder was set, then
    the folder unset (a portability-drill tenant, a config regression). The
    approved card used to sit forever with no event, no anomaly, no action.
    Now every executed run names it."""
    landing, w9_folder = _world(tmp_path)
    (landing / "Alpha Parts W-9.pdf").write_bytes(b"%PDF form bytes")
    _run_intake(tmp_path, landing, w9_folder)
    card = _cards(tmp_path, w9.W9_CARD)[0]
    _approve(tmp_path, card["id"], classification="s-corp")

    result = _run_intake_no_folder(tmp_path, monkeypatch, landing)

    unset = [a for a in result.anomalies if a.code == "ap.w9_folder_unset"]
    named = [a for a in unset if f"card {card['id']}" in a.detail]
    assert len(named) == 1
    assert "Alpha Parts W-9.pdf" in named[0].detail
    assert _events(tmp_path, w9.W9_FILED_EVENT) == []
    assert list(w9_folder.iterdir()) == []


def test_unreadable_text_layer_never_reaches_the_extractor(tmp_path, monkeypatch):
    """02-F7 (S3): a PDF whose text layer raises (encrypted, malformed) used
    to read as "not a W-9" and go to the extractor model with its TIN. The
    TIN invariant forbids that: a failed detection is not a negative one.
    Now the file parks in the review shape with no model call at all."""
    from core.agents.ap import extraction

    landing, w9_folder = _world(tmp_path)
    (landing / "scan_0042.pdf").write_bytes(b"%PDF neutral name, encrypted")

    def _raises(path):
        raise ValueError("PDF is encrypted")

    monkeypatch.setattr(extraction, "read_document_text", _raises)
    seen: list[str] = []
    real_extract = extraction.FixtureExtractor.extract
    monkeypatch.setattr(
        extraction.FixtureExtractor,
        "extract",
        lambda self, path: (seen.append(path.name), real_extract(self, path))[1],
    )

    result = _run_intake(tmp_path, landing, w9_folder)

    assert result.status != "error", result.summary
    assert seen == [], "the extractor model saw a file whose W-9 detection failed"
    assert any(a.code == "ap.w9_text_unreadable" for a in result.anomalies)
    assert _events(tmp_path, "ap.intake.routed.w9") == []  # not claimed as a W-9 either
    review = _events(tmp_path, "ap.intake.needs_ocr")
    assert [e["payload"]["file"] for e in review] == ["scan_0042.pdf"]
    cards = _cards(tmp_path, "ap.review_needs_ocr")
    assert [c["params"]["file"] for c in cards] == ["scan_0042.pdf"]
    assert "NEEDS_OCR 1" in result.summary


def test_guard_refusal_on_the_w9_folder_is_a_job_error_not_a_retry(tmp_path, monkeypatch):
    """02-F8 (S4): ProtectedSurfaceError is a PermissionError, so the copy's
    OSError net swallowed a write-guard refusal into `ap.w9_copy_failed ...
    will retry next run`. A guard refusal never clears: it is a job error."""
    from core.engine import runner
    from core.engine.guard import WriteGuard

    landing, w9_folder = _world(tmp_path)
    (landing / "Alpha Parts W-9.pdf").write_bytes(b"%PDF form bytes")
    _run_intake(tmp_path, landing, w9_folder)
    card = _cards(tmp_path, w9.W9_CARD)[0]
    _approve(tmp_path, card["id"], classification="s-corp")

    # The tenant's guard now protects the W-9 folder with no carve-out.
    monkeypatch.setattr(runner, "WriteGuard", lambda roots, allowed=None: WriteGuard([w9_folder]))
    result = _run_intake(tmp_path, landing, w9_folder)

    assert result.status == "error"
    assert any("ProtectedSurfaceError" in a.detail for a in result.anomalies)
    assert not any(a.code == "ap.w9_copy_failed" for a in result.anomalies)
    assert _events(tmp_path, w9.W9_FILED_EVENT) == []
    assert list(w9_folder.iterdir()) == []


def test_filing_an_approved_w9_scores_a_complete_handoff(tmp_path):
    """02-F9 (S4): the handoff rubric compared counts to events, and a W-9
    execution event joins events without a count, so every run that filed a
    form scored handoff 0.0 while the handoff was complete."""
    landing, w9_folder = _world(tmp_path)
    (landing / "Alpha Parts W-9.pdf").write_bytes(b"%PDF form bytes")
    first = _run_intake(tmp_path, landing, w9_folder)
    assert first.rubric.handoff_quality == 1.0
    card = _cards(tmp_path, w9.W9_CARD)[0]
    _approve(tmp_path, card["id"], classification="s-corp")

    result = _run_intake(tmp_path, landing, w9_folder)

    assert len(_events(tmp_path, w9.W9_FILED_EVENT)) == 1
    assert result.rubric.handoff_quality == 1.0
