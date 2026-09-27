"""The auditor finds the tenant where the engine finds it.

Since the tenant cut (docs/decisions/2026-09-22-the-tenant-lives-in-its-own-
repository.md) ``tenants/<slug>/`` lives outside the engine checkout, and the
launchd environment names the location once, as ``ENGINE_TENANTS_ROOT``. The
auditor imports nothing from ``core`` (the independence lint), so it re-reads
that one variable itself rather than importing the constant; its own
``AUDITOR_TENANTS_DIR`` still wins when set, and the repo-relative default
still holds when neither is set.
"""

from __future__ import annotations

from pathlib import Path

from auditor import config


def test_engine_tenants_root_is_honoured_when_the_auditor_has_no_own_setting(monkeypatch, tmp_path):
    monkeypatch.delenv("AUDITOR_TENANTS_DIR", raising=False)
    monkeypatch.setenv("ENGINE_TENANTS_ROOT", str(tmp_path / "tenants"))
    assert config.default_tenants_dir() == tmp_path / "tenants"


def test_the_auditors_own_setting_still_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("AUDITOR_TENANTS_DIR", str(tmp_path / "auditor-tenants"))
    monkeypatch.setenv("ENGINE_TENANTS_ROOT", str(tmp_path / "engine-tenants"))
    assert config.default_tenants_dir() == tmp_path / "auditor-tenants"


def test_neither_set_means_the_checkout_beside_auditor(monkeypatch):
    monkeypatch.delenv("AUDITOR_TENANTS_DIR", raising=False)
    monkeypatch.delenv("ENGINE_TENANTS_ROOT", raising=False)
    expected = Path(config.__file__).resolve().parents[1] / "tenants"
    assert config.default_tenants_dir() == expected


def test_load_auditor_tenant_reads_through_engine_tenants_root(monkeypatch, tmp_path):
    root = tmp_path / "elsewhere" / "tenants"
    (root / "moved").mkdir(parents=True)
    (root / "moved" / "tenant.toml").write_text(
        '[identity]\nslug = "moved"\ntimezone = "America/Chicago"\n'
    )
    monkeypatch.delenv("AUDITOR_TENANTS_DIR", raising=False)
    monkeypatch.setenv("ENGINE_TENANTS_ROOT", str(root))
    cfg = config.load_auditor_tenant("moved")
    assert cfg.slug == "moved"
    assert cfg.timezone == "America/Chicago"
