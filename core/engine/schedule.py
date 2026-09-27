"""The host's crontab, rendered from ``tenant.toml`` (phase 7 row 7.21).

The Mac's schedule is twelve launchd plists that an installer renders. The
container's schedule is one crontab that ``supercronic`` reads, and it is a
GENERATED file for the same reason the AP workbook is: the times live in
``[host.schedule]`` beside every other tenant knob, ``engine schedule
<tenant>`` renders them, and a host that drifted from its tenant file would be
a lie nobody could see.

Six entries, in a fixed order so two renders of one tenant are byte-identical:

    engine         the daily loop            scripts/engine-ap-daily.sh
    auditor        the nightly audit         scripts/auditor-nightly.sh
    ledger_backup  the ledger push           scripts/ledger-backup.sh
    heartbeat      the off-box dead man      scripts/host-heartbeat.sh
    retries        due job retries           scripts/engine-jobs-resume.sh
    build          the build lane            whatever the host names

Every line runs a SCRIPT, never an inline ``uv`` command, so the contract the
scripts carry (which tenant, which uv, which extras, the freshness guard, the
dead-man ping) lives in exactly one place and the container and the Mac run
the same file.

The frame comes from ``host/crontab.tmpl`` with ``{{PLACEHOLDER}}`` tokens,
the convention row 7.25 established for host templates: a placeholder with no
value is a hard error, never a ``{{`` left in a line a scheduler will execute.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .config import TenantConfig

ENTRY_ORDER = ("engine", "auditor", "ledger_backup", "heartbeat", "retries", "build")

EVERY_MINUTE = "* * * * *"
"""What ``--every-minute`` writes into every enabled entry: the fast crontab
CI and a first install use to watch one whole cycle instead of waiting for
02:00. It rewrites the expressions and nothing else, so what runs is the real
crontab running the real scripts."""

# name -> (script under the repo, log file stem, the comment the line carries)
SHIPPED: dict[str, tuple[str, str, str]] = {
    "engine": (
        "scripts/engine-ap-daily.sh",
        "engine-ap-daily",
        "the daily loop: mail, intake, filing, accounting writes, reconcile, workbook",
    ),
    "auditor": (
        "scripts/auditor-nightly.sh",
        "auditor-nightly",
        "the independent nightly audit (docs/auditor-design.md)",
    ),
    "ledger_backup": (
        "scripts/ledger-backup.sh",
        "ledger-backup",
        "push the ledger repo to its remote (the 24 h off-machine copy)",
    ),
    "heartbeat": (
        "scripts/host-heartbeat.sh",
        "host-heartbeat",
        "off-box dead-man ping: shell and curl only, no engine code",
    ),
    "retries": (
        "scripts/engine-jobs-resume.sh",
        "engine-jobs-resume",
        "execute due job retries (docs/retries.md)",
    ),
}
BUILD_LOG_STEM = "build-lane"
BUILD_COMMENT = "the build lane ([host.schedule].build_command)"

_TOKEN = re.compile(r"\{\{([^{}]*)\}\}")


@dataclass(frozen=True)
class ScheduleEntry:
    """One crontab line: when, what, and where its output lands."""

    name: str
    cron: str
    command: str
    log_path: str
    comment: str

    def line(self) -> str:
        return f"{self.cron} {self.command} >> {self.log_path} 2>&1"


def template_path() -> Path:
    """``host/crontab.tmpl`` in THIS repository."""
    return Path(__file__).resolve().parents[2] / "host" / "crontab.tmpl"


def entries(
    cfg: TenantConfig,
    *,
    repo: str | Path,
    log_dir: str | Path,
    every_minute: bool = False,
) -> list[ScheduleEntry]:
    """The enabled entries, in ``ENTRY_ORDER``.

    ``repo`` is where the code lives on the host (``/app`` in the container),
    ``log_dir`` where each job's output is appended (a folder on the data
    volume, so the logs survive the container).
    """
    repo_path = str(repo).rstrip("/")
    logs = str(log_dir).rstrip("/")
    out: list[ScheduleEntry] = []
    for name in ENTRY_ORDER:
        cron = getattr(cfg.host.schedule, name)
        if not cron:
            continue
        if name == "build":
            command, stem, comment = cfg.host.schedule.build_command, BUILD_LOG_STEM, BUILD_COMMENT
        else:
            script, stem, comment = SHIPPED[name]
            command = f"{repo_path}/{script}"
        out.append(
            ScheduleEntry(
                name=name,
                cron=EVERY_MINUTE if every_minute else cron,
                command=command,
                log_path=f"{logs}/{stem}.log",
                comment=comment,
            )
        )
    return out


def render(text: str, values: dict[str, str]) -> str:
    """Fill ``{{PLACEHOLDER}}`` tokens; an unknown one raises ``KeyError``."""
    missing = sorted({m.group(1) for m in _TOKEN.finditer(text) if m.group(1) not in values})
    if missing:
        raise KeyError(", ".join(f"{{{{{name}}}}}" for name in missing))
    return _TOKEN.sub(lambda m: values[m.group(1)], text)


def render_crontab(
    cfg: TenantConfig,
    *,
    tenant: str,
    repo: str | Path,
    log_dir: str | Path,
    every_minute: bool = False,
    template: str | Path | None = None,
) -> str:
    """The crontab text for this tenant on this host."""
    scheduled = entries(cfg, repo=repo, log_dir=log_dir, every_minute=every_minute)
    jobs = "\n\n".join(f"# {entry.comment}\n{entry.line()}" for entry in scheduled)
    source = Path(template) if template is not None else template_path()
    values = {
        "TENANT": tenant,
        # supercronic schedules in its own timezone unless the crontab names
        # one. "02:00" means 02:00 where the business is, and the tenant file
        # is the only thing that knows where that is.
        "TZ": cfg.identity.timezone,
        "REPO": str(repo).rstrip("/"),
        "LOG_DIR": str(log_dir).rstrip("/"),
        "JOBS": jobs if jobs else "# no entries are scheduled for this tenant",
    }
    out = render(source.read_text(encoding="utf-8"), values)
    return out if out.endswith("\n") else out + "\n"
