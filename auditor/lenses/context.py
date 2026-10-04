"""Lens 9 — context: the owner's canonical context system has not drifted.

The owner's agent context lives in one git repo; a generator inside that repo
builds stamped shims (the instruction files every agent surface loads) from
it. The predecessor system died of hand-maintained mirrors plus health checks
that whispered; this lens is the scream. Everything tenant-specific comes from
``[auditor.context]`` in tenant.toml. Five checks, all local (filesystem +
git, no network, so the lens runs under ``--local-only``):

- the repo itself is committed and pushed (mirrors the ledger-backup pulse);
- every configured shim exists, carries a v1 stamp, embeds the repo's current
  HEAD, and hashes clean — a hand-edit is content that dies at the next
  regenerate, CRITICAL by default;
- the focus file has a recent commit (a stale focus is a stale compass) and
  stays under its size cap (every shim loads it into every session);
- retired context locations stay dead (a reappeared copy is the old drift
  coming back);
- the month's first audit deals the owner one INFO review card.

The stamp contract, coordinated by convention with the generator (same spirit
as the ledger-location convention in ledger_reader): line 1 of every shim is
exactly ``<!-- context-shim v1 source=<40-hex HEAD sha> generated=<date>
sha256=<64-hex hash of every byte after the first newline> -->``.
"""

from __future__ import annotations

import errno
import hashlib
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ..findings import Finding
from ..store import AuditorStore
from . import AuditContext

LENS = "context"

STAMP_RE = re.compile(
    r"^<!-- context-shim v1 "
    r"source=(?P<source>[0-9a-f]{40}) "
    r"generated=(?P<generated>\S+) "
    r"sha256=(?P<digest>[0-9a-f]{64}) -->$"
)


def _git(root: Path, *args: str) -> tuple[int, str]:
    # Same shape as the heartbeat lens's private helper (deliberate small
    # duplication, house style).
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, timeout=30
    )
    return result.returncode, result.stdout.strip()


def _hours_between(earlier_iso: str, now: datetime) -> float:
    return (now - datetime.fromisoformat(earlier_iso)).total_seconds() / 3600


def check_repo(ctx: AuditContext) -> list[Finding]:
    repo = Path(ctx.tenant.context_repo).expanduser()
    subject = "context repo"
    code, _ = _git(repo, "rev-parse", "--git-dir")
    if code != 0:
        return [
            Finding(
                lens=LENS,
                subject=subject,
                condition="not-a-repo",
                severity="CRITICAL",
                detail=f"{repo} is missing or not a git repository; the canonical "
                "context has no home and no history",
            )
        ]
    findings: list[Finding] = []
    code, out = _git(repo, "status", "--porcelain")
    if code == 0 and out:
        findings.append(
            Finding(
                lens=LENS,
                subject=subject,
                condition="dirty-worktree",
                severity="WARN",
                detail=f"{len(out.splitlines())} uncommitted change(s) in the working "
                "tree; the wrap-up rule says commit in the same breath as the work",
            )
        )
    # @{upstream} rather than a hardcoded branch ref: the context repo's
    # branch name is the owner's business, not this lens's.
    code, _ = _git(repo, "rev-parse", "--verify", "-q", "@{upstream}")
    if code != 0:
        findings.append(
            Finding(
                lens=LENS,
                subject=subject,
                condition="no-remote-ref",
                severity="CRITICAL",
                detail="no upstream tracking ref; the context repo has never been "
                "pushed off this machine",
            )
        )
        return findings
    code, out = _git(repo, "log", "@{upstream}..HEAD", "--format=%cI")
    unpushed = [line for line in out.splitlines() if line.strip()] if code == 0 else []
    if unpushed:
        age = _hours_between(unpushed[-1], ctx.now)  # git log is newest first
        if age > ctx.tenant.context_unpushed_max_age_hours:
            findings.append(
                Finding(
                    lens=LENS,
                    subject=subject,
                    condition="unpushed-too-long",
                    severity="CRITICAL",
                    detail=f"{len(unpushed)} commit(s) unpushed, oldest {age:.0f}h old "
                    f"(window {ctx.tenant.context_unpushed_max_age_hours}h); the "
                    "canonical context exists only on this machine",
                )
            )
    return findings


def _shim_text(path: Path) -> str:
    # Raw read, factored out so evals can simulate a cloud placeholder
    # hermetically (same seam as ap.jobs._file_bytes).
    return path.read_text(encoding="utf-8")


def _cloud_only(configured: str) -> Finding:
    return Finding(
        lens=LENS,
        subject=f"shim {configured}",
        condition="cloud-only",
        severity="WARN",
        detail="the file is an unhydrated cloud placeholder (EDEADLK "
        "on read); its stamp cannot be verified from a headless "
        'session — pin it "Always Keep on This Device" so the sync '
        "client keeps local content",
    )


def _drift_findings(text: str, head: str, subject: str) -> list[Finding]:
    """What a shim the lens DID read says about itself: no stamp, a body that
    disagrees with its stamped hash, or a stamp from an older source commit."""
    first, _, body = text.partition("\n")
    match = STAMP_RE.match(first.strip())
    if match is None:
        return [
            Finding(
                lens=LENS,
                subject=subject,
                condition="no-stamp",
                severity="WARN",
                detail="line 1 carries no parseable v1 stamp; either the generator "
                "never wrote this file or something stripped the stamp",
            )
        ]
    findings: list[Finding] = []
    if hashlib.sha256(body.encode("utf-8")).hexdigest() != match["digest"]:
        findings.append(
            Finding(
                lens=LENS,
                subject=subject,
                condition="hand-edited",
                severity="CRITICAL",
                detail="the body no longer matches the stamped hash; edits made "
                "here die at the next regenerate — fold them into the source "
                "repo and rebuild",
            )
        )
    if head and match["source"] != head:
        findings.append(
            Finding(
                lens=LENS,
                subject=subject,
                condition="stale-source",
                severity="WARN",
                detail=f"generated from {match['source'][:8]} (on "
                f"{match['generated']}) but the repo HEAD is {head[:8]}; "
                "regenerate the shims",
            )
        )
    return findings


def shim_verification_state(path, *, read, head: str = "") -> str:
    """Three answers, not two: ``verified`` (read, stamp clean), ``drifted``
    (read, and the content disagrees with its stamp), ``unverifiable`` (the
    read could not happen). A placeholder teaches the lens nothing about the
    file, which is a different fact from learning something bad. Only EDEADLK
    maps to unverifiable (#106); any other read error propagates."""
    try:
        text = read(path)
    except OSError as exc:
        if exc.errno != errno.EDEADLK:
            raise
        return "unverifiable"
    return "drifted" if _drift_findings(text, head, "") else "verified"


def unverifiable_findings(shims, *, sync_roots) -> list[Finding]:
    """Unreadable shims, reported as what they are: a coverage gap about WHERE
    they sit. Every unreadable shim under one configured sync root becomes one
    INFO finding naming the root and the count; one outside every configured
    root keeps today's per-file WARN. No root configured = per-file for all,
    exactly as before: the lens never infers a sync mount from a path."""
    roots = sorted(sync_roots, key=lambda r: len(str(Path(r).expanduser())), reverse=True)
    grouped: dict[str, list[str]] = {}
    findings: list[Finding] = []
    for configured in shims:
        path = Path(configured).expanduser()
        root = next((r for r in roots if path.is_relative_to(Path(r).expanduser())), None)
        if root is None:
            findings.append(_cloud_only(configured))
        else:
            grouped.setdefault(root, []).append(configured)
    for root, members in grouped.items():
        findings.append(
            Finding(
                lens=LENS,
                subject=f"synced tree {root}",
                condition="unverifiable",
                severity="INFO",
                detail=f"{len(members)} shim(s) under this synced tree could not be "
                "read tonight (cloud placeholder, EDEADLK), so their stamps went "
                "unverified: a coverage gap, not drift. Keep the shims on local "
                "disk, or accept the gap: " + ", ".join(members),
            )
        )
    return findings


def check_shims(ctx: AuditContext) -> list[Finding]:
    repo = Path(ctx.tenant.context_repo).expanduser()
    _, head = _git(repo, "rev-parse", "HEAD")  # '' if broken; check_repo already screams
    roots = ctx.tenant.context_sync_roots
    findings: list[Finding] = []
    unreadable: list[str] = []
    for configured in ctx.tenant.context_shims:
        path = Path(configured).expanduser()
        # Subject is the configured path string: basenames collide (several
        # shims are all named CLAUDE.md) and the config string keeps the
        # fingerprint stable across moves of home.
        subject = f"shim {configured}"
        if not path.is_file():
            findings.append(
                Finding(
                    lens=LENS,
                    subject=subject,
                    condition="missing",
                    severity="CRITICAL",
                    detail="the generated file does not exist; agents on this surface load nothing",
                )
            )
            continue
        try:
            text = _shim_text(path)
        except OSError as exc:
            # A shim on a cloud-sync mount can be evicted to a dataless
            # placeholder; reading one from a headless session fails with
            # EDEADLK and does not hydrate it (docs/lessons.md, "A cloud
            # placeholder is not here yet"). Only EDEADLK
            # folds into a finding; any other read error stays a loud crash.
            if exc.errno != errno.EDEADLK:
                raise
            unreadable.append(configured)
            if not roots:  # today's per-file WARN, in today's position
                findings.append(_cloud_only(configured))
            continue
        findings.extend(_drift_findings(text, head, subject))
    if roots and unreadable:
        findings.extend(unverifiable_findings(unreadable, sync_roots=roots))
    return findings


def check_focus(ctx: AuditContext) -> list[Finding]:
    rel = ctx.tenant.context_focus_file
    if not rel:
        return []
    repo = Path(ctx.tenant.context_repo).expanduser()
    subject = f"focus {rel}"
    code, out = _git(repo, "log", "-1", "--format=%cI", "--", rel)
    if code != 0 or not out:
        return [
            Finding(
                lens=LENS,
                subject=subject,
                condition="never-committed",
                severity="WARN",
                detail="no commit touches this file; the current-state surface is "
                "untracked or missing",
            )
        ]
    findings = _focus_size(ctx, repo / rel, subject)
    age_days = (ctx.now - datetime.fromisoformat(out)).total_seconds() / 86400
    if age_days <= ctx.tenant.context_focus_max_age_days:
        return findings
    return [
        Finding(
            lens=LENS,
            subject=subject,
            condition="gone-stale",
            severity="WARN",
            detail=f"last commit touching it is {age_days:.0f} day(s) old (window "
            f"{ctx.tenant.context_focus_max_age_days}d); the stated focus no longer "
            "tracks reality",
        ),
        *findings,
    ]


def _focus_size(ctx: AuditContext, path: Path, subject: str) -> list[Finding]:
    """focus.md is one page; it reached 156K before anything measured it."""
    cap = ctx.tenant.context_focus_max_bytes
    try:
        size = path.stat().st_size
    except OSError:
        return []  # never-committed / missing is the other check's finding
    if size <= cap:
        return []
    return [
        Finding(
            lens=LENS,
            subject=subject,
            condition="over-size",
            severity="WARN",
            detail=f"{size:,} bytes against a {cap:,}-byte cap; every shim loads it "
            "into every session. Distill closed bullets to the log and move "
            "standing facts to domains/",
        )
    ]


def check_fossils(ctx: AuditContext) -> list[Finding]:
    return [
        Finding(
            lens=LENS,
            subject=f"fossil {configured}",
            condition="reappeared",
            severity="WARN",
            detail="a retired context location exists again; something is writing to "
            "a dead copy instead of the repo",
        )
        for configured in ctx.tenant.context_forbidden_paths
        if Path(configured).expanduser().exists()
    ]


def check_monthly_review(ctx: AuditContext) -> list[Finding]:
    """First audit of the month deals one INFO review card. 'First run of the
    month, whenever it happens' rather than a literal day-of-month gate: if
    the auditor is down on the 1st, the card still fires on the 3rd, and only
    once. The month lives in the subject, so each month is a fresh fingerprint
    instead of a noisy re-announcement of last month's item."""
    if not ctx.tenant.context_monthly_review:
        return []
    try:
        zone = ZoneInfo(ctx.tenant.timezone)
    except Exception:
        zone = UTC
    month = ctx.now.astimezone(zone).strftime("%Y-%m")
    with AuditorStore.open(ctx.store_root) as store:
        rows = store.conn.execute(
            "SELECT started_at FROM auditor_runs WHERE tenant = ? AND started_at != ? "
            "ORDER BY id DESC LIMIT 1",
            (ctx.tenant.slug, ctx.now.isoformat()),
        ).fetchall()
    if rows:
        previous_month = (
            datetime.fromisoformat(rows[0]["started_at"]).astimezone(zone).strftime("%Y-%m")
        )
        if previous_month == month:
            return []  # not the month's first audit
    return [
        Finding(
            lens=LENS,
            subject=f"context review {month}",
            condition="monthly-review-due",
            severity="INFO",
            detail="first audit of the month: walk the context repo — prune stale "
            "facts, refresh the focus file, regenerate every shim, re-paste the "
            "chat-surface block",
        )
    ]


def check(ctx: AuditContext) -> list[Finding]:
    if not ctx.tenant.context_repo:
        return []  # tenant has no context system to audit
    return [
        *check_repo(ctx),
        *check_shims(ctx),
        *check_focus(ctx),
        *check_fossils(ctx),
        *check_monthly_review(ctx),
    ]
