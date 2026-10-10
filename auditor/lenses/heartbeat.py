"""Lens 1 — heartbeat: is the machinery alive?

Six independent pulses, each recomputed from ground truth, never from logs
the engine writes for humans:

- the engine fired within the daily window: at least one fresh non-shadow
  row in the ``runs`` table. Per-job STALENESS is deliberately not checked:
  the engine's idempotency replays a job whose context is unchanged (same
  files = same key = no new row), so on a quiet day most stages leave no
  fresh timestamp — that is health, not silence. First seen live 2026-07-20:
  timesheets intake had replayed for a week and the naive check called it
  dead. A stage that dies without even an error row is caught by the
  OUTCOME lenses instead (unhandled arrivals surface in filing coverage);
- every expected daily job has run at least once ever (a stage missing
  entirely is a wiring error) and its latest run did not exit 'error';
- the ledger repo has no unpushed commits older than the backup window
  (its own ``git log`` against the remote-tracking ref);
- the QBO token file rotated recently (Intuit rotates the refresh token on
  every successful exchange, so a stale mtime means the engine has stopped
  talking to the accounting system);
- the auditor's own previous run finished clean (a report that failed to
  generate is itself a CRITICAL condition the next night — silence is never
  the signal);
- no scheduled run was refused or degraded by the entry-script preflight
  guard (issue #108): the guard writes JSON markers under
  ``<store base>/preflight/`` when a runtime checkout is stale, dirty, or
  off-main, and this lens is the delivery surface that makes a refusal loud.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path

from ..findings import Finding
from ..store import AuditorStore
from . import AuditContext

LENS = "heartbeat"

# A marker older than this has had its nights on the report; letting it age
# out is what allows the checklist reconciliation to resolve the item once
# refusals stop happening.
PREFLIGHT_LOOKBACK_HOURS = 48


def _hours_between(earlier_iso: str, now: datetime) -> float:
    earlier = datetime.fromisoformat(earlier_iso)
    return (now - earlier).total_seconds() / 3600


def check_engine_liveness(ctx: AuditContext) -> list[Finding]:
    """Did the engine fire at all inside the window? A healthy day always
    writes at least one fresh row (the intake stage re-keys on its daily
    window even when nothing arrived)."""
    if not ctx.tenant.expected_daily_jobs:
        return []  # tenant has no daily run to expect
    max_age = ctx.tenant.daily_run_max_age_hours
    rows = ctx.ledger.query(
        "SELECT created_at FROM runs WHERE tenant = ? AND shadow = 0 ORDER BY id DESC LIMIT 1",
        (ctx.tenant.slug,),
    )
    subject = "engine daily run"
    if not rows:
        return [
            Finding(
                lens=LENS,
                subject=subject,
                condition="never-ran",
                severity="CRITICAL",
                detail="the runs table holds no non-shadow run at all",
            )
        ]
    age = _hours_between(rows[0]["created_at"], ctx.now)
    if age <= max_age:
        return []
    return [
        Finding(
            lens=LENS,
            subject=subject,
            condition="stale",
            severity="CRITICAL",
            detail=f"the newest run of anything is {age:.0f}h old (window {max_age}h); "
            "the daily run did not fire",
        )
    ]


def _never_live(ctx: AuditContext, agent: str, job: str, subject: str) -> Finding:
    """An expected stage with no live run. Say what the ledger holds: a dry run
    (shadow row) is a run, so "no run recorded at all" is only true of a stage
    with no row of any kind. A stage whose first dry run is younger than one
    daily window has not had a scheduled fire since it was built (it merges
    after the morning fire and the audit runs before the next), so that is a
    fact awaiting its first fire, not a page. One window is the lens's own
    ``daily_run_max_age_hours``; past it the stage is the wiring error this
    check exists for. A stage with no row of any kind is never graced: nothing
    recorded cannot be told from a stage never wired."""
    shadow = ctx.ledger.query(
        "SELECT COUNT(*) AS n, MIN(created_at) AS first FROM runs "
        "WHERE tenant = ? AND agent = ? AND job = ? AND shadow = 1",
        (ctx.tenant.slug, agent, job),
    )[0]
    if not shadow["n"]:
        return Finding(
            lens=LENS,
            subject=subject,
            condition="never-ran",
            severity="CRITICAL",
            detail="no run recorded at all for this daily stage; the stage is "
            "expected in config but has never executed",
        )
    age = _hours_between(shadow["first"], ctx.now)
    seen = (
        f"only {shadow['n']} shadow (dry) run(s) recorded, the first {age:.0f}h ago; "
        "no live run yet"
    )
    if age <= ctx.tenant.daily_run_max_age_hours:
        return Finding(
            lens=LENS,
            subject=subject,
            condition="awaiting-first-run",
            severity="INFO",
            detail=f"{seen}; inside one daily window, so the first scheduled fire has "
            "not come round yet",
        )
    return Finding(
        lens=LENS,
        subject=subject,
        condition="never-ran",
        severity="CRITICAL",
        detail=f"{seen}; the stage is expected in config and a full daily window has "
        "passed without it going live",
    )


def check_daily_runs(ctx: AuditContext) -> list[Finding]:
    """Per-stage wiring and error checks. No staleness here — replay
    semantics make a stale timestamp healthy (see module docstring)."""
    findings: list[Finding] = []
    for agent, job in ctx.tenant.expected_daily_jobs:
        rows = ctx.ledger.query(
            "SELECT status, created_at FROM runs "
            "WHERE tenant = ? AND agent = ? AND job = ? AND shadow = 0 "
            "ORDER BY id DESC LIMIT 1",
            (ctx.tenant.slug, agent, job),
        )
        subject = f"daily run {agent}/{job}"
        if not rows:
            findings.append(_never_live(ctx, agent, job, subject))
        elif rows[0]["status"] == "error":
            findings.append(
                Finding(
                    lens=LENS,
                    subject=subject,
                    condition="errored",
                    severity="CRITICAL",
                    detail=f"the most recent run ({rows[0]['created_at'][:16]}) exited with "
                    "status 'error'",
                )
            )
    return findings


def _git(root: Path, *args: str) -> tuple[int, str]:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, timeout=30
    )
    return result.returncode, result.stdout.strip()


def check_ledger_backup(ctx: AuditContext) -> list[Finding]:
    root = ctx.ledger.root
    subject = "ledger backup"
    code, _ = _git(root, "rev-parse", "--git-dir")
    if code != 0:
        return [
            Finding(
                lens=LENS,
                subject=subject,
                condition="not-a-repo",
                severity="CRITICAL",
                detail=f"the ledger at {root} is not a git repository; nothing is backed up",
            )
        ]
    code, _ = _git(root, "rev-parse", "--verify", "-q", "refs/remotes/origin/main")
    if code != 0:
        return [
            Finding(
                lens=LENS,
                subject=subject,
                condition="no-remote-ref",
                severity="CRITICAL",
                detail="the ledger repo has no origin/main tracking ref; it has never "
                "been pushed off this machine",
            )
        ]
    code, out = _git(root, "log", "refs/remotes/origin/main..HEAD", "--format=%cI")
    if code != 0:
        return [
            Finding(
                lens=LENS,
                subject=subject,
                condition="log-failed",
                severity="CRITICAL",
                detail="could not compare the ledger against its remote-tracking ref",
            )
        ]
    unpushed = [line for line in out.splitlines() if line.strip()]
    if not unpushed:
        return []
    oldest = unpushed[-1]  # git log is newest first
    age = _hours_between(oldest, ctx.now)
    if age <= ctx.tenant.backup_max_age_hours:
        return []  # normal intraday lag; the nightly push has not come round yet
    return [
        Finding(
            lens=LENS,
            subject=subject,
            condition="unpushed-too-long",
            severity="CRITICAL",
            detail=f"{len(unpushed)} ledger commit(s) unpushed, oldest {age:.0f}h old "
            f"(window {ctx.tenant.backup_max_age_hours}h); the nightly backup is not keeping up",
        )
    ]


def check_qbo_token(ctx: AuditContext) -> list[Finding]:
    configured = ctx.tenant.qbo_token_file
    if not configured:
        return []  # tenant has no accounting-system connection to check
    path = Path(configured).expanduser()
    subject = "qbo token"
    if not path.exists():
        return [
            Finding(
                lens=LENS,
                subject=subject,
                condition="missing",
                severity="CRITICAL",
                detail=f"token file {path} does not exist; the engine cannot reach "
                "the accounting system",
            )
        ]
    age_days = (ctx.now.timestamp() - path.stat().st_mtime) / 86400
    if age_days >= ctx.tenant.token_critical_days:
        severity = "CRITICAL"
    elif age_days >= ctx.tenant.token_warn_days:
        severity = "WARN"
    else:
        return []
    return [
        Finding(
            lens=LENS,
            subject=subject,
            condition="stale",
            severity=severity,
            detail=f"token file last rotated {age_days:.0f} day(s) ago; the engine "
            "has stopped exchanging tokens with the accounting system "
            "(left long enough, this forces a human re-consent)",
        )
    ]


def check_previous_audit(ctx: AuditContext) -> list[Finding]:
    with AuditorStore.open(ctx.store_root) as store:
        rows = store.conn.execute(
            "SELECT started_at, status, error FROM auditor_runs WHERE tenant = ? "
            "AND started_at != ? ORDER BY id DESC LIMIT 1",
            (ctx.tenant.slug, ctx.now.isoformat()),
        ).fetchall()
    if not rows:
        return []  # first night ever; nothing to compare against
    previous = dict(rows[0])
    if previous["status"] == "ok":
        return []
    what = (
        "crashed before finishing"
        if previous["status"] == "running"
        else f"failed: {previous['error'] or 'unknown error'}"
    )
    return [
        Finding(
            lens=LENS,
            subject="previous audit",
            condition="did-not-finish-clean",
            severity="CRITICAL",
            detail=f"the audit started {previous['started_at'][:16]} {what}; "
            "that night's report cannot be trusted to exist",
        )
    ]


def _parse_marker_ts(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def check_preflight_refusals(ctx: AuditContext) -> list[Finding]:
    """Surface scheduled-run preflight markers (scripts/run-preflight.sh).

    The guard writes to ``<store base>/preflight/`` — one level above the
    per-tenant store dir, because a stale runtime checkout is a machine
    condition, not a tenant condition. A refused auditor run cannot report
    itself the same night; this check makes it loud on the next run that
    does happen.
    """
    marker_dir = ctx.store_root.parent / "preflight"
    if not marker_dir.is_dir():
        return []

    findings: list[Finding] = []
    fresh: dict[tuple[str, str], list[dict]] = {}
    for path in sorted(marker_dir.glob("*.json")):
        try:
            marker = json.loads(path.read_text())
            age_hours = (ctx.now - _parse_marker_ts(marker["ts"])).total_seconds() / 3600
            job = str(marker["job"])
            kind = str(marker["kind"])
        except Exception:
            mtime_age = (ctx.now.timestamp() - path.stat().st_mtime) / 3600
            if mtime_age <= PREFLIGHT_LOOKBACK_HOURS:
                findings.append(
                    Finding(
                        lens=LENS,
                        subject=f"preflight marker {path.name}",
                        condition="marker-unreadable",
                        severity="WARN",
                        detail="a preflight marker exists but cannot be parsed; "
                        "a scheduled run may have been refused without a readable reason",
                    )
                )
            continue
        if age_hours <= PREFLIGHT_LOOKBACK_HOURS:
            fresh.setdefault((job, kind), []).append(marker)

    for (job, kind), markers in sorted(fresh.items()):
        newest = max(markers, key=lambda m: m["ts"])
        refused = kind == "refusal"
        count = len(markers)
        counted = f"{count} {kind}(s) in {PREFLIGHT_LOOKBACK_HOURS}h" if count > 1 else kind
        findings.append(
            Finding(
                lens=LENS,
                subject=f"scheduled run {job}",
                condition="preflight-refused" if refused else "preflight-warning",
                severity="CRITICAL" if refused else "WARN",
                detail=f"{counted}: {newest.get('reason', 'unknown')} — "
                f"{newest.get('detail', '')} (branch {newest.get('branch', '?')}, "
                f"last {newest['ts']})" + ("; the job did NOT run" if refused else ""),
            )
        )
    return findings


def check(ctx: AuditContext) -> list[Finding]:
    return [
        *check_engine_liveness(ctx),
        *check_daily_runs(ctx),
        *check_ledger_backup(ctx),
        *check_qbo_token(ctx),
        *check_previous_audit(ctx),
        *check_preflight_refusals(ctx),
    ]
