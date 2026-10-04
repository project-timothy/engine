"""``[qbo].payment_records`` is a tenant setting (phase 7 row 7.2, issue
#211): off by default, declared explicitly by every shipped tenant, and
flipped on for the first live tenant by row 7.3 (issue #212) once the statement file
became the clearing signal. The owner's rule for the QuickBooks lane: the
flag exists so a tenant may leave payment records off, and it is not a
construction fence. Before 7.3 this file pinned every tenant off; that was
the dark-mode contract, retired the day 7.3 merged."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from core.engine.config import QboSettings, load_tenant

REPO = Path(__file__).resolve().parents[2]
TENANTS = sorted(p.name for p in (REPO / "tenants").iterdir() if (p / "tenant.toml").exists())


def test_the_default_is_off():
    assert QboSettings().payment_records is False


@pytest.mark.parametrize("slug", TENANTS)
def test_every_shipped_tenant_declares_the_flag_explicitly(slug):
    """A tenant never inherits the default silently: the line is in the file,
    so a reader of tenant.toml sees the choice."""
    text = (REPO / "tenants" / slug / "tenant.toml").read_text()
    assert re.search(r"^payment_records\s*=\s*(true|false)\s*$", text, re.M), (
        f"{slug}: [qbo].payment_records must be declared, not defaulted"
    )


def test_the_demo_does_not_record_payments():
    """The synthetic tenant stays off, so the off path keeps its evals (the
    on side is pinned in the tenant repository that turned it on)."""
    assert load_tenant("demo").qbo.payment_records is False
