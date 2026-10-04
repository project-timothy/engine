"""Lens 19 — recurrence: the system points at its own next automation.

Owner's ask 2026-09-10 ("think about recursive self-improvement"): the
triage sees the same finding three mornings in a row and treats each as
new, and the owner clears the same thing by hand every week. This lens
reads the auditor's OWN store (prior nights' findings, never tonight's)
and names the repeats, each with the automation that would retire it:

- ``long-lived``: a finding still open after ``long_nights`` nights. The
  owner has been looking at it every morning; either it is a standing
  fact that wants an acknowledgment, or a chore that wants a job.
- ``class``: one condition raised on ``class_subjects`` or more distinct
  subjects inside ``class_window_days`` (open or resolved). Six vendors
  missing the same flag, six clearings the ledger cannot explain: a class
  is never an incident, it is a missing feature. How many of them are still
  open is the PREVIOUS report's count and the line says so (see ``AS_OF``):
  this lens runs before the reconcile that closes tonight's, so a present
  tense here can contradict the same report's own resolved list.

Every finding carries a candidate: ``[auditor.recurrence.candidates]`` maps
``"<lens>/<condition>"`` to the sentence naming the automation; an unmapped
repeat says "name one", and the 06:00 triage names it in the note's
Recurring patterns section (the morning brief lifts the first bullet). The
lens counts, the triage names, the owner decides.

The candidate is DATA as well as prose (phase 7 row 7.18): every finding
carries a :class:`~auditor.findings.Candidate` with a stable id, the source
subjects, and the count, and the detail line ends with ``candidate: <text>
[<id>]``. The id is the automation's, not the finding's, so the three kinds
below agree on one id for one repeat and the triage lane can propose it
exactly once.

- ``recurring``: a finding that resolved and came back ``reopen_min`` or
  more times (the store counts every return since 2026-09-10). The disk
  chore is the type specimen: cleared by hand, back the next night, young
  again every morning by ``first_seen`` alone. Bounded by the same window
  as ``class``, because ``reopen_count`` only ever grows: a repeat whose
  last sighting predates the window has stopped repeating, and a pointer
  that cannot stop pointing outlives the automation it asked for.

Severity is INFO throughout: this lens is a pointer, never an alarm, and
the underlying findings already carry their own severity. It never
re-reports its own findings (lens ``recurrence`` is excluded from its own
reads).
"""

from __future__ import annotations

from datetime import timedelta

from ..findings import Candidate, Finding, candidate_id
from ..store import AuditorStore
from . import AuditContext

LENS = "recurrence"
UNNAMED = "name one (the triage writes it under Recurring patterns)"
# Every lens runs BEFORE the run's reconcile pass (``auditor/runner.py``), so
# the ``state`` column read here is the one the previous report committed --
# the store commits only after a report reaches disk (honesty audit 04-F3), so
# that report is exactly what an open count describes. Saying so is the
# difference between a number and a claim: on 2026-09-29 this line said two
# unknown clearings were still open on the same report that announced one of
# them resolved, and on 2026-09-12 it said six were open on the report that
# closed all six. The count is real and useful; the present tense was not.
AS_OF = "on the previous report"


def _nights(first_seen: str, now) -> float:
    from datetime import datetime

    try:
        started = datetime.fromisoformat(first_seen)
    except ValueError:
        return 0.0
    if started.tzinfo is None:
        started = started.replace(tzinfo=now.tzinfo)
    return (now - started).total_seconds() / 86400


def _seen_since(last_seen: str, window_start, now) -> bool:
    """Was this finding's last sighting inside the lens's window? Both repeat
    kinds are bounded by it, so the lens has one horizon rather than two that
    can drift apart. An unparseable timestamp is outside it: this lens is a
    pointer, and a pointer on evidence it cannot read is noise."""
    from datetime import datetime

    try:
        last = datetime.fromisoformat(last_seen)
    except ValueError:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=now.tzinfo)
    return last >= window_start


def candidate_for(
    ctx: AuditContext,
    lens: str,
    condition: str,
    *,
    subjects: tuple[str, ...] = (),
    count: int = 0,
) -> Candidate:
    """The automation for one source condition: the tenant's sentence when
    the table names it (``lens/condition`` first, then ``lens``), else the
    "name one" handoff to the triage."""
    table = ctx.tenant.recurrence_candidates
    named = table.get(f"{lens}/{condition}") or table.get(lens)
    return Candidate(
        id=candidate_id(lens, condition),
        text=str(named) if named else UNNAMED,
        named=bool(named),
        source_lens=lens,
        source_condition=condition,
        subjects=subjects,
        count=count,
    )


def _says(candidate: Candidate) -> str:
    """The prose tail: the sentence the report has always carried, plus the
    id the proposal lane keys on."""
    return f"candidate: {candidate.text} [{candidate.id}]"


def _prior_findings(ctx: AuditContext) -> list[dict]:
    """Every finding the store knows for this tenant, except this lens's own."""
    with AuditorStore.open(ctx.store_root) as store:
        rows = store.conn.execute(
            "SELECT lens, subject, condition, severity, state, first_seen, last_seen, "
            "resolved_at, reopen_count "
            "FROM findings WHERE tenant = ? AND lens != ? ORDER BY first_seen, lens, subject",
            (ctx.tenant.slug, LENS),
        ).fetchall()
    return [dict(r) for r in rows]


def check(ctx: AuditContext) -> list[Finding]:
    if not ctx.tenant.recurrence_enabled:
        return []
    rows = _prior_findings(ctx)
    if not rows:
        return []
    findings: list[Finding] = []
    # The lens's one horizon, read by both repeat kinds below.
    window_start = ctx.now - timedelta(days=ctx.tenant.recurrence_class_window_days)

    # 1. long-lived: open findings older than the threshold, oldest first.
    long_nights = ctx.tenant.recurrence_long_nights
    for row in rows:
        if row["state"] != "open":
            continue
        nights = _nights(row["first_seen"], ctx.now)
        if nights < long_nights:
            continue
        candidate = candidate_for(
            ctx,
            row["lens"],
            row["condition"],
            subjects=(row["subject"],),
            count=int(nights),
        )
        findings.append(
            Finding(
                lens=LENS,
                subject=f"{row['lens']}: {row['subject']}",
                condition="long-lived",
                severity="INFO",
                detail=f"open {int(nights)} nights (since {row['first_seen'][:10]}, "
                f"{row['severity']} {row['condition']}); a fact that wants an "
                f"acknowledgment or a chore that wants a job; {_says(candidate)}",
                candidate=candidate,
            )
        )

    # 2. class: one condition across many subjects inside the window.
    by_class: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        if not _seen_since(row["last_seen"], window_start, ctx.now):
            continue
        by_class.setdefault((row["lens"], row["condition"]), []).append(row)
    for (lens, condition), members in sorted(by_class.items()):
        subjects = sorted({m["subject"] for m in members})
        if len(subjects) < ctx.tenant.recurrence_class_subjects:
            continue
        open_n = sum(1 for m in members if m["state"] == "open")
        shown = "; ".join(subjects[:4]) + (
            f"; +{len(subjects) - 4} more" if len(subjects) > 4 else ""
        )
        candidate = candidate_for(
            ctx, lens, condition, subjects=tuple(subjects), count=len(subjects)
        )
        findings.append(
            Finding(
                lens=LENS,
                subject=f"{lens} {condition}",
                condition="class",
                severity="INFO",
                detail=f"{len(subjects)} subjects in {ctx.tenant.recurrence_class_window_days} "
                f"days ({open_n} still open {AS_OF}): {shown}; a class is a missing feature, "
                f"not an incident; {_says(candidate)}",
                candidate=candidate,
            )
        )

    # 3. recurring: resolved and back again, reopen_min times or more.
    reopen_min = ctx.tenant.recurrence_reopen_min
    for row in rows:
        returns = int(row.get("reopen_count") or 0)
        if returns < reopen_min:
            continue
        # The count is cumulative and never resets, so recency is what makes it
        # a pointer rather than a monument: a chore that has gone quiet for the
        # window ages off the checklist the way every other finding does. An
        # open row is always inside the window (the store refreshes last_seen on
        # every sighting), so this bounds only the resolved tail.
        if not _seen_since(row["last_seen"], window_start, ctx.now):
            continue
        state = "open again" if row["state"] == "open" else "resolved for now"
        candidate = candidate_for(
            ctx,
            row["lens"],
            row["condition"],
            subjects=(row["subject"],),
            count=returns,
        )
        findings.append(
            Finding(
                lens=LENS,
                subject=f"{row['lens']}: {row['subject']}",
                condition="recurring",
                severity="INFO",
                detail=f"cleared and back {returns} times ({state}; {row['severity']} "
                f"{row['condition']}); a chore done by hand on repeat wants a job; "
                f"{_says(candidate)}",
                candidate=candidate,
            )
        )
    return findings
