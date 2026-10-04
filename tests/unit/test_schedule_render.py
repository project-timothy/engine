"""The container's crontab is rendered from ``tenant.toml`` (phase 7 row 7.21).

The Mac's schedule is twelve launchd plists. The container's schedule is one
crontab that ``supercronic`` reads, and it is a GENERATED file: the times live
in ``[host.schedule]`` beside every other tenant knob, and ``engine schedule
<tenant>`` renders them. Nothing in the container is hand-edited, so a host
that drifts from its tenant file cannot happen.

Six entries, in this order: the daily loop, the nightly audit, the ledger
push, the dead-man heartbeat, due job retries (row 7.23 declared the cadence
and left the line to this row), and the build lane. A cron expression is the
entry's on switch: empty means the line is not rendered, which is how the
build lane ships off.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from core.engine.config import TenantConfig, load_tenant
from core.engine.schedule import (
    ENTRY_ORDER,
    EVERY_MINUTE,
    ScheduleEntry,
    entries,
    render_crontab,
    template_path,
)

REPO = Path(__file__).resolve().parents[2]


def _cfg(**schedule) -> TenantConfig:
    """A tenant config carrying only what the schedule needs."""
    data = {
        "identity": {"legal_name": "Demo Tenant Inc.", "slug": "demo", "timezone": "America/Lima"},
    }
    if schedule:
        data["host"] = {"schedule": schedule}
    return TenantConfig.model_validate(data)


def _render(cfg: TenantConfig, **kw) -> str:
    return render_crontab(cfg, tenant="demo", repo="/app", log_dir="/data/logs", **kw)


def _job_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln and not ln.startswith("#") and "=" not in ln[:12]]


# ---- the defaults the row names -----------------------------------------------


def test_the_five_default_entries_render_at_the_times_the_row_names():
    """auditor 02:00, engine 08:00, ledger backup 23:00, heartbeat every 30
    minutes, retries every 15 (row 7.23's cadence). The build lane is off."""
    assert _job_lines(_render(_cfg())) == [
        "0 8 * * * /app/scripts/engine-ap-daily.sh >> /data/logs/engine-ap-daily.log 2>&1",
        "0 2 * * * /app/scripts/auditor-nightly.sh >> /data/logs/auditor-nightly.log 2>&1",
        "0 23 * * * /app/scripts/ledger-backup.sh >> /data/logs/ledger-backup.log 2>&1",
        "*/30 * * * * /app/scripts/host-heartbeat.sh >> /data/logs/host-heartbeat.log 2>&1",
        "*/15 * * * * /app/scripts/engine-jobs-resume.sh >> /data/logs/engine-jobs-resume.log 2>&1",
    ]


def test_the_build_lane_is_off_until_a_host_turns_it_on():
    assert "build" not in _render(_cfg())
    text = _render(_cfg(build="0 4 * * *", build_command="/app/host/build-lane.sh"))
    assert text.splitlines()[-1] == (
        "0 4 * * * /app/host/build-lane.sh >> /data/logs/build-lane.log 2>&1"
    )


def test_an_entry_with_an_empty_expression_renders_no_line():
    """The expression IS the on switch: one knob per entry, never a second
    ``enabled`` boolean that can disagree with it."""
    text = _render(_cfg(ledger_backup="", heartbeat="", retries=""))
    assert len(_job_lines(text)) == 2
    assert "ledger-backup.sh" not in text


def test_a_tenant_that_moves_a_time_moves_the_line():
    text = _render(_cfg(engine="30 6 * * *"))
    assert "30 6 * * * /app/scripts/engine-ap-daily.sh" in text
    assert "0 8 * * *" not in text


def test_the_order_is_fixed_so_two_renders_of_one_tenant_are_identical():
    assert _render(_cfg()) == _render(_cfg())
    assert ENTRY_ORDER == ("engine", "auditor", "ledger_backup", "heartbeat", "retries", "build")


# ---- the frame ----------------------------------------------------------------


def test_the_schedule_timezone_is_the_tenant_timezone():
    """supercronic reads schedules in its own timezone unless the crontab
    carries CRON_TZ. ``02:00`` means 02:00 where the business is, so the
    tenant's ``[identity].timezone`` is rendered into the file."""
    assert "CRON_TZ=America/Lima" in _render(_cfg())


def test_the_file_says_it_is_generated_and_names_the_command_that_regenerates_it():
    text = _render(_cfg())
    assert text.startswith("#")
    assert "engine schedule demo" in text
    assert text.endswith("\n")


def test_every_placeholder_in_the_template_is_filled():
    assert "{{" not in _render(_cfg())


def test_the_template_is_the_one_the_row_names():
    assert template_path() == REPO / "host" / "crontab.tmpl"
    assert template_path().is_file()


def test_a_template_naming_an_unknown_placeholder_is_a_hard_error(tmp_path):
    """render-template.py's contract (row 7.25), kept here: a typo fails the
    render, it never leaves ``{{`` in a path a scheduler will execute."""
    bad = tmp_path / "crontab.tmpl"
    bad.write_text("CRON_TZ={{TIMEZONE}}\n{{JOBS}}\n")
    with pytest.raises(KeyError) as exc:
        _render(_cfg(), template=bad)
    assert "TIMEZONE" in str(exc.value)


# ---- the fast crontab the CI cycle uses ---------------------------------------


def test_every_minute_rewrites_the_expressions_and_nothing_else():
    """One cycle in CI cannot wait until 02:00. ``--every-minute`` moves every
    enabled entry to ``* * * * *`` and leaves the commands untouched, so what
    CI proves is the real crontab running the real scripts."""
    normal = entries(_cfg(), repo="/app", log_dir="/data/logs")
    fast = entries(_cfg(), repo="/app", log_dir="/data/logs", every_minute=True)
    assert [e.command for e in fast] == [e.command for e in normal]
    assert {e.cron for e in fast} == {EVERY_MINUTE}


# ---- validation: a bad expression fails at config load, never at 02:00 --------


@pytest.mark.parametrize(
    "expression",
    ["0 8 * *", "every day", "0 8 * * * *", "0 25 * * *", ""],
)
def test_a_cron_expression_that_is_not_five_fields_is_refused_at_config_load(expression):
    if expression == "":
        pytest.skip("empty is the off switch, tested above")
    with pytest.raises(ValidationError) as exc:
        _cfg(engine=expression)
    assert "engine" in str(exc.value)


def test_the_build_lane_cannot_be_scheduled_without_naming_a_command():
    """The other five entries run scripts this repo ships. The build lane is
    the host's own, so turning it on without saying what to run is a config
    error naming the key, not a silent empty cron line."""
    with pytest.raises(ValidationError) as exc:
        _cfg(build="0 4 * * *")
    assert "build_command" in str(exc.value)


# ---- the demo tenant carries the section -------------------------------------


def test_the_demo_tenant_carries_the_schedule_and_renders():
    text = (REPO / "tenants" / "demo" / "tenant.toml").read_text()
    assert "[host.schedule]" in text
    assert (REPO / "tenants" / "_templates" / "tenant.toml.tmpl").read_text().count("[host") == 2
    cfg = load_tenant("demo")
    assert cfg.host.schedule.engine == "0 8 * * *"
    assert cfg.host.schedule.build == ""
    text = render_crontab(cfg, tenant="demo", repo="/app", log_dir="/data/logs")
    assert len(_job_lines(text)) == 5


def test_an_entry_knows_its_own_log_file():
    entry = next(e for e in entries(_cfg(), repo="/app", log_dir="/logs") if e.name == "auditor")
    assert isinstance(entry, ScheduleEntry)
    assert entry.log_path == "/logs/auditor-nightly.log"
