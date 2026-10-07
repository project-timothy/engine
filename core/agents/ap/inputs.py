"""Tenant inputs every AP job module reads: the vendor registry and the
tenant-local day.

They live here, not in ``jobs``, so a job module split out of ``jobs`` (the
QuickBooks push jobs, public issue #2) reads them without importing ``jobs``
back. ``jobs`` imports both under its old private names, so a patch of
``jobs._retry_day`` still lands on the jobs that live there.
"""

from __future__ import annotations

from pathlib import Path

from ...engine.contracts import JobContext
from .registry import VendorRegistry, load_vendor_registry


def vendors(ctx: JobContext) -> VendorRegistry:
    from ...engine.config import tenant_dir

    override = ctx.params.get("vendors_toml")
    path = Path(override) if override else tenant_dir(ctx.tenant_slug) / "vendors.toml"
    if not path.exists():
        return VendorRegistry()
    return load_vendor_registry(path)


def retry_day(ctx: JobContext) -> str:
    """The tenant-local date. An intake-key input only while a retryable
    flag names a landing candidate, so a transport failure is retried once
    a day instead of replayed forever (the expenses ``_verify_day`` rule)."""
    from ...engine.clock import local_today

    return local_today(ctx.tenant.identity.timezone)
