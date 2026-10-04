"""The auditor's own tenant-config reader.

Deliberate duplication (docs/auditor-design.md): the auditor reads the same
tenants/<slug>/tenant.toml as the engine but with its OWN parser, so a bug in
the engine's config layer cannot blind the checker. Only the fields the
lenses need are surfaced; everything else stays available in ``raw``.
"""

from __future__ import annotations

import pytest

from auditor.config import AuditorConfigError, load_auditor_tenant

FULL_TOML = """
[identity]
legal_name = "T Corp"
slug = "t"
timezone = "America/New_York"

[ap]
landing_dir = "/tmp/landing"
filing_dir = "/tmp/filing"
workbook_path = "/tmp/book.xlsx"
workbook_columns = [
    {label = "Vendor", field = "vendor"},
    {label = "Amount", field = "amount_cents"},
]

[timesheets]
filing_dir = "/tmp/timesheets"

[qbo]
token_file = "~/.qbo-tokens.json"
since_days = 30

[auditor]
report_dir = "/tmp/reports"
expected_daily_jobs = ["mail/fetch", "ap/intake"]
daily_run_max_age_hours = 26
decision_window_days = 12
filing_grace_days = 3
stale_approval_days = 5
backup_max_age_hours = 30
token_warn_days = 4
token_critical_days = 20
coverage_since = "2026-07-09"

[auditor.context]
repo = "/tmp/ctx-repo"
shims = ["/tmp/shim-a.md", "/tmp/shim-b.md"]
focus_file = "focus.md"
focus_max_age_days = 10
forbidden_paths = ["/tmp/old-copy"]
unpushed_max_age_hours = 72
monthly_review = false
"""


def _write_tenant(tmp_path, slug="t", body=FULL_TOML):
    tdir = tmp_path / slug
    tdir.mkdir(parents=True)
    (tdir / "tenant.toml").write_text(body)
    return tmp_path


def test_full_config_round_trip(tmp_path):
    cfg = load_auditor_tenant("t", tenants_dir=_write_tenant(tmp_path))
    assert cfg.slug == "t"
    assert cfg.timezone == "America/New_York"
    assert cfg.landing_dir == "/tmp/landing"
    assert cfg.workbook_path == "/tmp/book.xlsx"
    assert cfg.workbook_columns == [("Vendor", "vendor"), ("Amount", "amount_cents")]
    assert cfg.timesheets_filing_dir == "/tmp/timesheets"
    assert cfg.qbo_token_file == "~/.qbo-tokens.json"
    assert cfg.report_dir == "/tmp/reports"
    assert cfg.expected_daily_jobs == [("mail", "fetch"), ("ap", "intake")]
    assert cfg.daily_run_max_age_hours == 26
    assert cfg.decision_window_days == 12
    assert cfg.filing_grace_days == 3
    assert cfg.stale_approval_days == 5
    assert cfg.backup_max_age_hours == 30
    assert cfg.token_warn_days == 4
    assert cfg.token_critical_days == 20
    assert cfg.coverage_since == "2026-07-09"
    assert cfg.context_repo == "/tmp/ctx-repo"
    assert cfg.context_shims == ["/tmp/shim-a.md", "/tmp/shim-b.md"]
    assert cfg.context_focus_file == "focus.md"
    assert cfg.context_focus_max_age_days == 10
    assert cfg.context_forbidden_paths == ["/tmp/old-copy"]
    assert cfg.context_unpushed_max_age_hours == 72
    assert cfg.context_monthly_review is False
    assert cfg.raw["identity"]["legal_name"] == "T Corp"


def test_minimal_config_gets_defaults(tmp_path):
    root = _write_tenant(tmp_path, body='[identity]\nslug = "t"\n')
    cfg = load_auditor_tenant("t", tenants_dir=root)
    assert cfg.timezone == "UTC"
    assert cfg.landing_dir == ""
    assert cfg.report_dir == ""
    assert cfg.expected_daily_jobs == []
    assert cfg.daily_run_max_age_hours == 25
    assert cfg.decision_window_days == 14
    assert cfg.filing_grace_days == 2
    assert cfg.stale_approval_days == 7
    assert cfg.backup_max_age_hours == 25
    assert cfg.token_warn_days == 3
    assert cfg.token_critical_days == 30
    assert cfg.coverage_since == ""
    assert cfg.context_repo == ""
    assert cfg.context_shims == []
    assert cfg.context_focus_file == ""
    assert cfg.context_focus_max_age_days == 14
    assert cfg.context_forbidden_paths == []
    assert cfg.context_unpushed_max_age_hours == 48
    assert cfg.context_monthly_review is True


def test_unknown_tenant_raises(tmp_path):
    with pytest.raises(AuditorConfigError):
        load_auditor_tenant("nope", tenants_dir=tmp_path)


def test_malformed_daily_job_raises(tmp_path):
    root = _write_tenant(
        tmp_path, body='[auditor]\nexpected_daily_jobs = ["not-agent-slash-job"]\n'
    )
    with pytest.raises(AuditorConfigError):
        load_auditor_tenant("t", tenants_dir=root)


CATCHUP_TOML = """
[identity]
slug = "t"

[qbo]
direct_payment_floor_cents = 200000

[auditor]
report_dir = "/tmp/reports"

[auditor.materiality]
floor_dollars = 2500
multiple = 2.5
window_days = 180

[auditor.check_gaps]
window_days = 60
min_observed = 2
series = ["1xxx"]

[auditor.reconcile]
unknown_window_days = 45

[auditor.triage]
notes_dir = "/tmp/reports/triage"
ack_max_age_days = 120

[auditor.registry]
path = "/tmp/registry.toml"
max_age_hours = 200

[auditor.projects]
enabled = false
overhead_tokens = ["g&a"]

[auditor.projects.nicknames]
"widget 0101" = "P00_0101"
"""


def test_catchup_lens_sections_round_trip(tmp_path):
    """The 2026-09-04 catch-up lenses: every section carries an enable switch
    that defaults to enabled, and its thresholds."""
    cfg = load_auditor_tenant("t", tenants_dir=_write_tenant(tmp_path, body=CATCHUP_TOML))
    # issue #370: one named setting, read by the lane and the auditor alike.
    assert cfg.qbo_direct_payment_floor_cents == 200000
    assert cfg.materiality_enabled is True
    assert cfg.materiality_floor_cents == 250000
    assert cfg.materiality_multiple == 2.5
    assert cfg.materiality_window_days == 180
    assert cfg.check_gaps_enabled is True
    assert cfg.check_gaps_window_days == 60
    assert cfg.check_gaps_min_observed == 2
    assert cfg.check_gaps_series == ["1xxx"]
    assert cfg.reconcile_enabled is True
    assert cfg.reconcile_unknown_window_days == 45
    assert cfg.triage_enabled is True
    assert cfg.triage_notes_dir == "/tmp/reports/triage"
    assert cfg.triage_ack_max_age_days == 120
    assert cfg.registry_enabled is True
    assert cfg.registry_path == "/tmp/registry.toml"
    assert cfg.registry_max_age_hours == 200
    assert cfg.projects_enabled is False
    assert cfg.projects_nicknames == {"widget 0101": "P00_0101"}
    assert cfg.projects_overhead_tokens == ["g&a"]


def test_catchup_lens_sections_default_to_enabled_with_thresholds(tmp_path):
    root = _write_tenant(tmp_path, body='[identity]\nslug = "t"\n')
    cfg = load_auditor_tenant("t", tenants_dir=root)
    # default matches core/engine/config.py's QboSettings.direct_payment_floor_cents
    # exactly: one number, defined once in words, twice in code (never imported).
    assert cfg.qbo_direct_payment_floor_cents == 60_000
    assert cfg.materiality_enabled is True
    assert cfg.materiality_floor_cents == 500000
    assert cfg.materiality_multiple == 3.0
    assert cfg.materiality_window_days == 365
    assert cfg.check_gaps_enabled is True
    assert cfg.check_gaps_window_days == 90
    assert cfg.check_gaps_min_observed == 3
    assert cfg.check_gaps_series == []
    assert cfg.reconcile_enabled is True
    assert cfg.reconcile_unknown_window_days == 30
    assert cfg.triage_enabled is True
    assert cfg.triage_notes_dir == ""
    assert cfg.triage_ack_max_age_days == 90
    assert cfg.registry_enabled is True
    assert cfg.registry_path == ""
    assert cfg.registry_max_age_hours == 168
    assert cfg.projects_enabled is True
    assert cfg.projects_nicknames == {}
    assert cfg.projects_overhead_tokens == []
