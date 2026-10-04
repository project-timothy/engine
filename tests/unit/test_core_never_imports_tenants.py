"""Product boundary: nothing under core/ or auditor/ imports from tenants/ (row 7.5)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PATTERN = re.compile(r"^\s*(from|import)\s+tenants\b", re.M)


def test_core_and_auditor_never_import_tenants():
    hits = [
        str(p.relative_to(ROOT))
        for pkg in ("core", "auditor")
        for p in (ROOT / pkg).rglob("*.py")
        if PATTERN.search(p.read_text())
    ]
    assert hits == [], f"tenant imports under the product packages: {hits}"
