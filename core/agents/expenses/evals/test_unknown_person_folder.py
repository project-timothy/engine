"""Unknown-person-folder visibility evals (issue #119, live case 2026-08-14).

The shape under reconstruction: a vendor's folder appeared in the drop tree
on 2026-08-13 with two empty project subfolders. The vendor is not in
[expenses].persons (deliberately), and nothing — no WARN, no card, no run
line — could have told the owner the tree held a folder intake would never
look at. Had receipt files landed there, they would sit forever with everyone
assuming they were processed.

Contract under test:
- A top-level drop-tree folder matching no configured person emits a
  warn-level event + anomaly line, once per folder — never a daily nag.
- The folder's appearance ALONE changes the intake run key (else the runner
  replays the prior result and the warn never fires — the live 8/13 shape).
- Receipt-suffixed files inside an unknown folder escalate to a review card;
  the files are NEVER processed (persons stays the authority on treatment).
- Reserved engine folders (underscore-prefixed: _originals) stay invisible.
"""

from __future__ import annotations

from core.engine.runner import resolve_ledger_root, run
from core.ledger import Ledger

PERSON = "Pat Owner"
STRANGER = "Sam Vendor"


def _drop(tmp_path, name: str, body: bytes = b"receipt-bytes", person: str = PERSON):
    target = tmp_path / "drop" / person / "P26_2001"
    target.mkdir(parents=True, exist_ok=True)
    (target / name).write_bytes(body)
    return target / name


def _params(tmp_path, **extra):
    return {
        "drop_dir": str(tmp_path / "drop"),
        "filing_dir": str(tmp_path / "filing"),
        "month": "2026-08",
        "extractor": "fixture",
        **extra,
    }


def _run(tmp_path, **extra):
    return run(
        "demo",
        "expenses",
        "intake",
        params=_params(tmp_path, **extra),
        ledger_dir=tmp_path / "ledger",
    )


def _events(tmp_path, event_type):
    root = resolve_ledger_root("demo", tmp_path / "ledger")
    with Ledger.open(root) as ledger:
        return [e for e in ledger.read_event_log() if e.get("event_type") == event_type]


def _warn_codes(result):
    return [a.code for a in result.anomalies]


def test_unknown_folder_appearance_alone_breaks_the_replay(tmp_path):
    # The live 8/13 shape: nothing else in the tree changes, so an unchanged
    # run key would replay the prior clean result and the folder stays
    # invisible forever.
    _drop(tmp_path, "lunch.pdf")
    first = _run(tmp_path)
    assert "expenses.unknown_person_folder" not in _warn_codes(first)

    (tmp_path / "drop" / STRANGER / "P00_0101").mkdir(parents=True)

    second = _run(tmp_path)

    assert "expenses.unknown_person_folder" in _warn_codes(second)
    (event,) = _events(tmp_path, "expense.unknown_person_folder")
    assert event["payload"]["folder"] == STRANGER


def test_warn_fires_once_per_folder_not_every_run(tmp_path):
    (tmp_path / "drop" / STRANGER).mkdir(parents=True)
    _run(tmp_path)

    # New legitimate activity changes the run key; the already-warned folder
    # must not nag again.
    _drop(tmp_path, "dinner.pdf")

    result = _run(tmp_path)

    assert "expenses.unknown_person_folder" not in _warn_codes(result)
    assert len(_events(tmp_path, "expense.unknown_person_folder")) == 1


def test_receipt_files_in_unknown_folder_card_and_never_process(tmp_path):
    stray = _drop(tmp_path, "hotel folio.pdf", person=STRANGER)

    result = _run(tmp_path)

    assert result.status == "needs_approval"
    cards = [
        a for a in result.approvals_needed if a.action_type == "expenses.unknown_person_folder"
    ]
    (card,) = cards
    assert card.params["folder"] == STRANGER
    assert "hotel folio.pdf" in card.params["files"]
    # Visibility only: nothing filed, no landed event, the file stays put.
    assert _events(tmp_path, "expense.receipt_landed") == []
    assert not (tmp_path / "filing").exists()
    assert stray.is_file()


def test_empty_unknown_folder_warns_without_a_card(tmp_path):
    (tmp_path / "drop" / STRANGER / "P00_0102").mkdir(parents=True)

    result = _run(tmp_path)

    assert result.status == "ok"
    assert "expenses.unknown_person_folder" in _warn_codes(result)
    assert not any(
        a.action_type == "expenses.unknown_person_folder" for a in result.approvals_needed
    )


def test_new_files_in_a_warned_folder_raise_a_fresh_card(tmp_path):
    _drop(tmp_path, "week1.pdf", person=STRANGER)
    first = _run(tmp_path)
    (first_card,) = [
        a for a in first.approvals_needed if a.action_type == "expenses.unknown_person_folder"
    ]

    _drop(tmp_path, "week2.pdf", body=b"other-bytes", person=STRANGER)

    second = _run(tmp_path)

    (second_card,) = [
        a for a in second.approvals_needed if a.action_type == "expenses.unknown_person_folder"
    ]
    assert sorted(second_card.params["files"]) == ["week1.pdf", "week2.pdf"]


def test_reserved_and_configured_folders_stay_silent(tmp_path):
    _drop(tmp_path, "lunch.pdf")
    (tmp_path / "drop" / "_originals").mkdir(parents=True)

    result = _run(tmp_path)

    assert "expenses.unknown_person_folder" not in _warn_codes(result)
    assert _events(tmp_path, "expense.unknown_person_folder") == []
