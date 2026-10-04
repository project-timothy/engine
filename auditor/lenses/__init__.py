"""Lens registry: each lens recomputes one class of truth and diffs it.

A lens is a pure function from :class:`AuditContext` to findings. Lenses
marked ``external`` reach outside the machine (the mailbox, the accounting
system) and are skipped under ``--local-only``. Registration order is report
order within a night.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import AuditorTenantConfig
    from ..ledger_reader import LedgerReader


@dataclass
class AuditContext:
    tenant: AuditorTenantConfig
    ledger: LedgerReader
    now: datetime  # timezone-aware, UTC
    store_root: Path  # .auditor/<slug>, for lens-private scratch if ever needed
    # where tenants/<slug>/ lives (None = the default tenants dir); lenses that
    # read tenant-side registries (vendors.toml) resolve against it
    tenants_dir: Path | None = None


@dataclass(frozen=True)
class LensSpec:
    name: str
    check: Callable[[AuditContext], list]
    external: bool = False


# Lens modules import AuditContext from this package, so they are imported
# after it is defined; registration order is report order within a night.
# Eighteen lenses are live: the eight design lenses, the context lens added
# 2026-07-22, the host lens added 2026-08-18, po-watch 2026-08-24, vendor-1099
# 2026-08-26, and the six catch-up lenses of 2026-09-04 (materiality,
# check-gaps, reconcile, triage, registry, projects; the checks the retired
# bookkeeper-auditor had that this one lacked). The advisory voice is a
# report section, not a lens.
from . import (  # noqa: E402
    approvals,
    book,
    check_gaps,
    context,
    filing,
    heartbeat,
    host,
    mail,
    materiality,
    po_watch,
    projects,
    qbo,
    reconcile,
    recurrence,
    registry,
    status_coherence,
    timesheets,
    triage_lens,
    vendor_1099,
)

LENSES: list[LensSpec] = [
    LensSpec(name="heartbeat", check=heartbeat.check),
    LensSpec(name="filing", check=filing.check),
    LensSpec(name="mail", check=mail.check, external=True),
    LensSpec(name="book", check=book.check),
    LensSpec(name="status", check=status_coherence.check),
    LensSpec(name="approvals", check=approvals.check),
    LensSpec(name="qbo", check=qbo.check, external=True),
    LensSpec(name="timesheets", check=timesheets.check),
    # Local only: filesystem + git, runs under --local-only.
    LensSpec(name="context", check=context.check),
    # Local only: the machine's own disk, backup, and cloud-eviction health.
    LensSpec(name="host", check=host.check),
    # Local only: archived customer POs vs the canonical PO folder and the
    # register's Open-POs sheet (issue #111, added 2026-08-24).
    LensSpec(name="po-watch", check=po_watch.check),
    # External: the registry's 1099 picture vs QBO's Vendor1099 flags
    # (docs/w9-1099-design.md build 2, added 2026-08-26).
    LensSpec(name="vendor-1099", check=vendor_1099.check, external=True),
    # The 2026-09-04 catch-up, all local (ledger + files); each has an
    # [auditor.<name>] enable switch that defaults to enabled.
    LensSpec(name="materiality", check=materiality.check),
    LensSpec(name="check-gaps", check=check_gaps.check),
    LensSpec(name="reconcile", check=reconcile.check),
    LensSpec(name="triage", check=triage_lens.check),
    LensSpec(name="registry", check=registry.check),
    LensSpec(name="projects", check=projects.check),
    # Lens 19 (2026-09-10): the auditor's own store re-read for repeats, each
    # naming the automation that would retire it. Runs last so the night's
    # other lenses are on the report above it; it reads prior nights only.
    LensSpec(name="recurrence", check=recurrence.check),
]
