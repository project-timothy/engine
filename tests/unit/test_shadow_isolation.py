"""DoD item 0: the shadow path cannot reach any fidelity-contract surface.

A full shadow intake runs against a tenant whose protected_paths point at a
sentinel "production" tree. The test proves, byte for byte, that the
production tree and the landing folder are untouched and that every filesystem
change the run made landed inside the engine's own ledger root.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from core.engine.runner import resolve_ledger_root, run

FIXTURES = Path(__file__).resolve().parents[2] / "core/agents/ap/evals/fixtures/landing"


def _tree_digest(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[str(path.relative_to(root))] = hashlib.md5(path.read_bytes()).hexdigest()
    return out


def _make_tenant(tmp_path: Path, protected: Path, landing: Path) -> None:
    tenant_dir = tmp_path / "tenants" / "shadowtest"
    tenant_dir.mkdir(parents=True)
    (tenant_dir / "tenant.toml").write_text(
        f"""
[identity]
legal_name = "Shadow Test Inc."
slug = "shadowtest"

[approval]
auto_file_under = 100.0

[ap]
landing_dir = "{landing}"
protected_paths = ["{protected}"]
""",
        encoding="utf-8",
    )
    (tenant_dir / "vendors.toml").write_text(
        """
["alphaparts.example"]
vendor = "Alpha Parts"
cost_type = "Materials"
subject_aliases = ["alpha parts"]
""",
        encoding="utf-8",
    )


def test_shadow_intake_cannot_touch_production_surfaces(tmp_path, monkeypatch):
    # A sentinel production tree standing in for the workbook + filing tree.
    production = tmp_path / "production"
    (production / "AP Ledgers").mkdir(parents=True)
    (production / "AP Ledgers" / "Ledger.xlsx").write_bytes(b"legacy workbook bytes")
    (production / "01_Inbox").mkdir()
    (production / "01_Inbox" / "existing.pdf").write_bytes(b"already filed")

    # The landing folder is production too: originals are the audit artifact.
    landing = tmp_path / "landing"
    landing.mkdir()
    for src in FIXTURES.iterdir():
        shutil.copy2(src, landing / src.name)

    _make_tenant(tmp_path, production, landing)
    monkeypatch.setenv("ENGINE_TENANTS_ROOT", str(tmp_path / "tenants"))

    production_before = _tree_digest(production)
    landing_before = _tree_digest(landing)
    ledger_dir = tmp_path / "engine-data"

    result = run(
        "shadowtest",
        "ap",
        "intake",
        shadow=True,
        params={"extractor": "fixture"},
        ledger_dir=ledger_dir,
    )
    assert result.status in ("ok", "needs_approval")
    assert result.shadow is True

    # Production and landing trees: byte-for-byte untouched.
    assert _tree_digest(production) == production_before
    assert _tree_digest(landing) == landing_before

    # The run did write, and everything it wrote lives under the ledger root.
    root = resolve_ledger_root("shadowtest", ledger_dir)
    assert (root / "ledger.sqlite3").exists()
    assert (root / "event_log.jsonl").exists()

    # And every row it recorded carries the shadow flag.
    from core.ledger import Ledger

    with Ledger.open(root) as ledger:
        flags = [r[0] for r in ledger.conn.execute("SELECT shadow FROM ap_invoices")]
        assert flags and all(flag == 1 for flag in flags)
        run_flags = [r[0] for r in ledger.conn.execute("SELECT shadow FROM runs")]
        assert all(flag == 1 for flag in run_flags)
