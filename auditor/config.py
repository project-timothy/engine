"""The auditor's own tenant-config reader.

Deliberate duplication (docs/auditor-design.md, principle 2): the auditor
reads the same ``tenants/<slug>/tenant.toml`` the engine reads — the file is
ground truth configuration, not engine code — but parses it with its own
small reader so a bug in the engine's config layer cannot blind the checker.
Only the fields the lenses need are surfaced; the full parse stays in
``raw`` for anything lens-specific.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


class AuditorConfigError(ValueError):
    pass


@dataclass(frozen=True)
class AuditorTenantConfig:
    slug: str
    timezone: str
    # engine surfaces the lenses recompute against
    landing_dir: str
    filing_dir: str
    workbook_path: str
    workbook_columns: list[tuple[str, str]]  # (label, field)
    timesheets_filing_dir: str
    qbo_token_file: str
    qbo_since_days: int
    # the auditor's own section
    report_dir: str
    expected_daily_jobs: list[tuple[str, str]]  # (agent, job)
    daily_run_max_age_hours: int
    decision_window_days: int
    filing_grace_days: int  # arrivals younger than this are still in flight
    stale_approval_days: int
    backup_max_age_hours: int
    token_warn_days: int
    token_critical_days: int
    coverage_since: str  # ISO date; files older than this pre-date the engine
    # the owner's context system ([auditor.context]); repo == "" disables the lens
    context_repo: str
    context_shims: list[str]
    context_focus_file: str
    context_focus_max_age_days: int
    context_focus_max_bytes: int
    context_forbidden_paths: list[str]
    context_unpushed_max_age_hours: int
    context_monthly_review: bool
    # the machine itself ([auditor.host]); absent section disables the lens
    host_enabled: bool = False
    host_data_path: str = "/"
    host_disk_warn_free_gb: int = 40
    host_disk_critical_free_gb: int = 20
    host_timemachine_max_age_hours: int = 36
    host_expected_volumes: list[str] = field(default_factory=list)
    host_eviction_watch_paths: list[str] = field(default_factory=list)
    # customer-PO routing watch ([auditor.po_watch]); absent section disables
    po_watch_enabled: bool = False
    po_archive_dir: str = ""
    po_home_dir: str = ""
    po_register_xlsx: str = ""
    po_register_sheet: str = "Open POs"
    # registry-vs-QBO 1099 flag sync ([auditor.vendor_1099]); absent section disables
    vendor_1099_enabled: bool = False
    # The 2026-09-04 catch-up lenses (docs/auditor-design.md lenses 13-18):
    # each section carries an enable switch that DEFAULTS TO ENABLED (an
    # absent section is the default configuration, not an opt-out), plus
    # its thresholds. Lenses that need a path stay out of scope while it
    # is unset.
    materiality_enabled: bool = True
    materiality_floor_cents: int = 500_000  # the design's $5,000 floor (revisit Nov 2026)
    materiality_multiple: float = 3.0
    materiality_window_days: int = 365
    check_gaps_enabled: bool = True
    check_gaps_window_days: int = 90
    check_gaps_min_observed: int = 3
    check_gaps_series: list[str] = field(default_factory=list)  # e.g. ["1xxx"]; [] = all
    reconcile_enabled: bool = True
    reconcile_unknown_window_days: int = 30
    triage_enabled: bool = True
    triage_notes_dir: str = ""  # explicit; unset = the marker check is out of scope
    triage_ack_max_age_days: int = 90
    registry_enabled: bool = True
    registry_path: str = ""  # the project registry TOML; unset = out of scope
    registry_max_age_hours: int = 168
    projects_enabled: bool = True
    projects_nicknames: dict = field(default_factory=dict)  # "rig 0101" -> "P00_0101"
    projects_overhead_tokens: list[str] = field(default_factory=list)
    # Lens 19, recurrence (2026-09-10): the auditor's own store re-read for
    # repeats; candidates map "lens/condition" (or "lens") to the automation.
    recurrence_enabled: bool = True
    recurrence_long_nights: int = 7
    recurrence_class_subjects: int = 3
    recurrence_class_window_days: int = 60
    recurrence_reopen_min: int = 3  # resolved-and-back this many times = recurring
    recurrence_candidates: dict = field(default_factory=dict)
    raw: dict = field(repr=False, default_factory=dict)


def default_tenants_dir() -> Path:
    env = os.environ.get("AUDITOR_TENANTS_DIR")
    if env:
        return Path(env)
    # Since the tenant cut (docs/decisions/2026-09-22-the-tenant-lives-in-its-
    # own-repository.md) the schedule names the tenant location once, for the
    # engine and the auditor alike. The name is re-read here, never imported:
    # the auditor imports nothing from core (auditor.evals.independence_lint).
    env = os.environ.get("ENGINE_TENANTS_ROOT")
    if env:
        return Path(env)
    # auditor/config.py -> parents[1] is the repo root; tenants/ sits beside auditor/.
    return Path(__file__).resolve().parents[1] / "tenants"


def _parse_daily_jobs(entries: list) -> list[tuple[str, str]]:
    jobs: list[tuple[str, str]] = []
    for entry in entries:
        text = str(entry)
        if text.count("/") != 1:
            raise AuditorConfigError(
                f"[auditor].expected_daily_jobs entries are 'agent/job'; got {text!r}"
            )
        agent, job = text.split("/")
        if not agent or not job:
            raise AuditorConfigError(
                f"[auditor].expected_daily_jobs entries are 'agent/job'; got {text!r}"
            )
        jobs.append((agent, job))
    return jobs


def load_auditor_tenant(slug: str, *, tenants_dir: str | Path | None = None) -> AuditorTenantConfig:
    root = Path(tenants_dir) if tenants_dir else default_tenants_dir()
    path = root / slug / "tenant.toml"
    if not path.exists():
        raise AuditorConfigError(f"no tenant {slug!r}: {path} does not exist")
    with path.open("rb") as handle:
        raw = tomllib.load(handle)

    identity = raw.get("identity", {})
    ap = raw.get("ap", {})
    timesheets = raw.get("timesheets", {})
    qbo = raw.get("qbo", {})
    auditor = raw.get("auditor", {})
    context = auditor.get("context", {})
    host = auditor.get("host", {})
    po_watch = auditor.get("po_watch", {})
    vendor_1099 = auditor.get("vendor_1099", {})
    materiality = auditor.get("materiality", {})
    check_gaps = auditor.get("check_gaps", {})
    reconcile = auditor.get("reconcile", {})
    triage = auditor.get("triage", {})
    registry = auditor.get("registry", {})
    projects = auditor.get("projects", {})
    recurrence = auditor.get("recurrence", {})

    columns = [
        (str(c.get("label", "")), str(c.get("field", ""))) for c in ap.get("workbook_columns", [])
    ]
    return AuditorTenantConfig(
        slug=slug,
        timezone=str(identity.get("timezone", "UTC")),
        landing_dir=str(ap.get("landing_dir", "")),
        filing_dir=str(ap.get("filing_dir", "")),
        workbook_path=str(ap.get("workbook_path", "")),
        workbook_columns=columns,
        timesheets_filing_dir=str(timesheets.get("filing_dir", "")),
        qbo_token_file=str(qbo.get("token_file", "")),
        qbo_since_days=int(qbo.get("since_days", 30)),
        report_dir=str(auditor.get("report_dir", "")),
        expected_daily_jobs=_parse_daily_jobs(auditor.get("expected_daily_jobs", [])),
        daily_run_max_age_hours=int(auditor.get("daily_run_max_age_hours", 25)),
        decision_window_days=int(auditor.get("decision_window_days", 14)),
        filing_grace_days=int(auditor.get("filing_grace_days", 2)),
        stale_approval_days=int(auditor.get("stale_approval_days", 7)),
        backup_max_age_hours=int(auditor.get("backup_max_age_hours", 25)),
        token_warn_days=int(auditor.get("token_warn_days", 3)),
        token_critical_days=int(auditor.get("token_critical_days", 30)),
        coverage_since=str(auditor.get("coverage_since", "")),
        context_repo=str(context.get("repo", "")),
        context_shims=[str(p) for p in context.get("shims", [])],
        context_focus_file=str(context.get("focus_file", "")),
        context_focus_max_age_days=int(context.get("focus_max_age_days", 14)),
        context_focus_max_bytes=int(context.get("focus_max_bytes", 25_000)),
        context_forbidden_paths=[str(p) for p in context.get("forbidden_paths", [])],
        context_unpushed_max_age_hours=int(context.get("unpushed_max_age_hours", 48)),
        context_monthly_review=bool(context.get("monthly_review", True)),
        host_enabled=bool(host) and bool(host.get("enabled", True)),
        host_data_path=str(host.get("data_path", "/")),
        host_disk_warn_free_gb=int(host.get("disk_warn_free_gb", 40)),
        host_disk_critical_free_gb=int(host.get("disk_critical_free_gb", 20)),
        host_timemachine_max_age_hours=int(host.get("timemachine_max_age_hours", 36)),
        host_expected_volumes=[str(p) for p in host.get("expected_volumes", [])],
        host_eviction_watch_paths=[str(p) for p in host.get("eviction_watch_paths", [])],
        po_watch_enabled=bool(po_watch) and bool(po_watch.get("enabled", True)),
        po_archive_dir=str(po_watch.get("archive_dir", "")),
        po_home_dir=str(po_watch.get("po_home_dir", "")),
        po_register_xlsx=str(po_watch.get("register_xlsx", "")),
        po_register_sheet=str(po_watch.get("register_sheet", "Open POs")),
        vendor_1099_enabled=bool(vendor_1099) and bool(vendor_1099.get("enabled", True)),
        materiality_enabled=bool(materiality.get("enabled", True)),
        materiality_floor_cents=int(round(float(materiality.get("floor_dollars", 5000)) * 100)),
        materiality_multiple=float(materiality.get("multiple", 3.0)),
        materiality_window_days=int(materiality.get("window_days", 365)),
        check_gaps_enabled=bool(check_gaps.get("enabled", True)),
        check_gaps_window_days=int(check_gaps.get("window_days", 90)),
        check_gaps_min_observed=int(check_gaps.get("min_observed", 3)),
        check_gaps_series=[str(s) for s in check_gaps.get("series", [])],
        reconcile_enabled=bool(reconcile.get("enabled", True)),
        reconcile_unknown_window_days=int(reconcile.get("unknown_window_days", 30)),
        triage_enabled=bool(triage.get("enabled", True)),
        triage_notes_dir=str(triage.get("notes_dir", "")),
        triage_ack_max_age_days=int(triage.get("ack_max_age_days", 90)),
        registry_enabled=bool(registry.get("enabled", True)),
        registry_path=str(registry.get("path", "")),
        registry_max_age_hours=int(registry.get("max_age_hours", 168)),
        projects_enabled=bool(projects.get("enabled", True)),
        projects_nicknames={
            str(k): str(v) for k, v in (projects.get("nicknames", {}) or {}).items()
        },
        projects_overhead_tokens=[str(t) for t in projects.get("overhead_tokens", [])],
        recurrence_enabled=bool(recurrence.get("enabled", True)),
        recurrence_long_nights=int(recurrence.get("long_nights", 7)),
        recurrence_class_subjects=int(recurrence.get("class_subjects", 3)),
        recurrence_class_window_days=int(recurrence.get("class_window_days", 60)),
        recurrence_reopen_min=int(recurrence.get("reopen_min", 3)),
        recurrence_candidates={
            str(k): str(v) for k, v in (recurrence.get("candidates", {}) or {}).items()
        },
        raw=raw,
    )
