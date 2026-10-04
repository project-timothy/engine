"""Unit tests for the tenant config loader and secret resolution."""

from __future__ import annotations

import pytest

from core.engine.config import (
    TenantNotFoundError,
    default_tenants_root,
    load_tenant,
)

TENANTS = default_tenants_root()


def test_loads_demo_tenant():
    cfg = load_tenant("demo", tenants_root=TENANTS)
    assert cfg.identity.legal_name == "Demo Tenant Inc."
    assert cfg.identity.slug == "demo"
    assert cfg.approval.currency == "USD"


def test_loads_second_tenant_with_distinct_config(tmp_path):
    """A second tenant beside the demo, differing only in its fiscal
    calendar, loads its own value: the loader reads config rather than
    falling back to a baked-in default."""
    demo = TENANTS / "demo" / "tenant.toml"
    other = tmp_path / "other"
    other.mkdir()
    text = demo.read_text().replace('slug = "demo"', 'slug = "other"')
    (other / "tenant.toml").write_text(text.replace("year_start_month = 7", "year_start_month = 1"))
    assert load_tenant("demo", tenants_root=TENANTS).fiscal.year_start_month == 7
    cfg = load_tenant("other", tenants_root=tmp_path)
    assert cfg.identity.slug == "other"
    assert cfg.fiscal.year_start_month == 1


def test_missing_tenant_raises():
    with pytest.raises(TenantNotFoundError):
        load_tenant("no-such-tenant", tenants_root=TENANTS)


def test_resolve_secret_reads_env(monkeypatch):
    cfg = load_tenant("demo", tenants_root=TENANTS)
    monkeypatch.setenv("DEMO_QBO_CLIENT_ID", "abc-123")
    assert cfg.resolve_secret("qbo_client_id") == "abc-123"


def test_resolve_secret_unset_env_raises(monkeypatch):
    cfg = load_tenant("demo", tenants_root=TENANTS)
    monkeypatch.delenv("DEMO_QBO_CLIENT_ID", raising=False)
    with pytest.raises(KeyError):
        cfg.resolve_secret("qbo_client_id")


def test_resolve_undeclared_secret_raises():
    cfg = load_tenant("demo", tenants_root=TENANTS)
    with pytest.raises(KeyError):
        cfg.resolve_secret("not_a_declared_secret")
