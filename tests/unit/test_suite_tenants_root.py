"""The suite tests the checkout it runs in, whatever the host's shell says.

Since the tenant cut (2026-09-22) this Mac exports ENGINE_TENANTS_ROOT for
the owner's interactive runs and every launchd wrapper exports it for the
scheduled sessions. Both point at the private tenant clone, which carries no
``demo`` tenant, so a suite that honoured the variable would fail every eval
that loads ``demo`` (the 06:00 triage runs this suite under that very
environment). The root conftest strips the two tenant-root names for the
whole session; this pins that.
"""

from __future__ import annotations

import os
from pathlib import Path

from auditor import config as auditor_config
from core.engine.config import default_tenants_root

REPO = Path(__file__).resolve().parents[2]


def test_the_tenant_root_names_are_absent_for_the_whole_session():
    assert "ENGINE_TENANTS_ROOT" not in os.environ
    assert "AUDITOR_TENANTS_DIR" not in os.environ


def test_the_engine_and_the_auditor_resolve_this_checkouts_tenants():
    assert default_tenants_root() == REPO / "tenants"
    assert auditor_config.default_tenants_dir() == REPO / "tenants"
    assert (REPO / "tenants" / "demo" / "tenant.toml").is_file()
