"""The shipped tree carries no real business.

Only the synthetic ``demo`` tenant and the ``_templates`` ``engine init``
renders from may live under ``tenants/``. A real tenant lives in its own
repository and is found through ``ENGINE_TENANTS_ROOT``; a folder of one
checked out, copied or restored here would ship its vendor list, paths and
host wiring with the next push. This is the CI boundary check of extraction
gate 3.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SHIPPED = {"demo", "_templates"}


def test_only_the_demo_and_the_templates_live_under_tenants():
    others = [
        p.name
        for p in (REPO / "tenants").iterdir()
        if p.is_dir() and p.name not in SHIPPED and any(p.rglob("*.toml"))
    ]
    assert others == [], f"a tenant folder other than {sorted(SHIPPED)} is in the tree: {others}"


def test_the_license_and_the_contribution_guide_ship():
    text = (REPO / "LICENSE").read_text(encoding="utf-8")
    assert "GNU AFFERO GENERAL PUBLIC LICENSE" in text
    assert "SECTION 7" in text
    assert (REPO / "CONTRIBUTING.md").is_file()
