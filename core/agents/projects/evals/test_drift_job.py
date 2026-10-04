"""projects/drift end to end (#325): the live report shape, cards, answers.

The report the tenant's sync writes has section headings and a "Stale" section
whose bullets mention folders that exist; those must never be read as missing.
"""

from __future__ import annotations

import pytest

from core.engine.runner import resolve_ledger_root, run
from core.ledger import Ledger

REPORT = """# Project Registry Drift 2026-10-01

**Drift items:** 3

## Missing from the accounting system (needs_qbo_project=true)

(none)

## Missing project folders (present in the registry)

- P00_0002 Project Two

## Low-confidence matches (manual review needed)

- P00_0003: appears in only one source. Review before adding to registry.

## Stale (folder exists, no activity 90+ days)

- P00_0004 Old Project: folder exists, no activity 120 days.

## Newly detected since last sync

- P00_0003 New Thing: new since last sync (in 1 source(s)).
"""


@pytest.fixture
def world(tmp_path):
    report = tmp_path / "project-registry-drift-2026-10-01.md"
    report.write_text(REPORT)
    return {"tmp": tmp_path, "report": report, "ledger_dir": tmp_path / "data"}


def _drift(world, **params):
    base = {"report": str(world["report"]), "drift_cards": "true"}
    return run(
        "demo", "projects", "drift", params={**base, **params}, ledger_dir=world["ledger_dir"]
    )


def _cards(world):
    root = resolve_ledger_root("demo", world["ledger_dir"])
    with Ledger.open(root) as ledger:
        rows = ledger.conn.execute(
            "SELECT id, status, params_json FROM approval_queue WHERE action_type = "
            "'projects.registry_drift' ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


def _decide(world, card_id, status):
    root = resolve_ledger_root("demo", world["ledger_dir"])
    with Ledger.open(root) as ledger:
        ledger.conn.execute("UPDATE approval_queue SET status = ? WHERE id = ?", (status, card_id))
        ledger.conn.commit()


def test_off_by_default_is_a_noop(world):
    out = run(
        "demo",
        "projects",
        "drift",
        params={"report": str(world["report"])},
        ledger_dir=world["ledger_dir"],
    )
    assert out.status == "ok" and "off" in out.summary and _cards(world) == []


def test_each_item_parks_one_card_and_stale_is_reported_not_acted(world):
    out = _drift(world)
    assert out.status == "needs_approval"
    kinds = sorted(__import__("json").loads(c["params_json"])["kind"] for c in _cards(world))
    assert kinds == ["folder-missing", "new-since-sync", "one-source"]
    (stale,) = [a for a in out.anomalies if a.code == "projects.drift_unclassified"]
    assert "P00_0004" in stale.detail
    _drift(world)  # the same report again: no second card
    assert len(_cards(world)) == 3


def test_an_approved_decision_is_carried_out_once_and_a_rejection_is_forever(world):
    _drift(world)
    cards = {__import__("json").loads(c["params_json"])["kind"]: c["id"] for c in _cards(world)}
    _decide(world, cards["one-source"], "approved")
    _decide(world, cards["new-since-sync"], "rejected")
    out = _drift(world)
    assert any("add P00_0003 to the project registry" in a for a in out.actions)
    again = _drift(world, report=str(world["report"]))
    assert not any("add P00_0003" in a for a in again.actions)  # carried out once
    later = world["tmp"] / "project-registry-drift-2026-10-08.md"
    later.write_text(REPORT)
    _drift(world, report=str(later))
    assert len(_cards(world)) == 3  # a decided item never cards again


def test_an_approved_folder_card_creates_only_the_folder(world):
    root = world["tmp"] / "projects"
    _drift(world)
    cards = {__import__("json").loads(c["params_json"])["kind"]: c["id"] for c in _cards(world)}
    _decide(world, cards["folder-missing"], "approved")
    out = run(
        "demo",
        "projects",
        "drift",
        params={"report": str(world["report"]), "drift_cards": "true", "folder_root": str(root)},
        ledger_dir=world["ledger_dir"],
    )
    assert (root / "P00_0002 Project Two").is_dir()
    assert [p.name for p in root.iterdir()] == ["P00_0002 Project Two"]
    assert any("folder created" in a for a in out.actions)


def test_a_shadow_run_parks_nothing(world):
    out = run(
        "demo",
        "projects",
        "drift",
        params={"report": str(world["report"]), "drift_cards": "true"},
        ledger_dir=world["ledger_dir"],
        shadow=True,
    )
    assert out.status == "ok" and "would park 3" in out.summary
    assert _cards(world) == []
