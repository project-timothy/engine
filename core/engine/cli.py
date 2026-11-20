"""Command-line entry point: ``engine run <tenant> <agent> <job> [--shadow]``
and the owner commands beside it (``init``, ``queue``, ``status``, ...),
plus ``engine jobs resume <tenant> [--now]`` (due retries, row 7.23).

Thin wrapper over :func:`core.engine.runner.run`. Prints a concise human
summary by default, or the full structured ``RunResult`` as JSON with
``--json``. Exit codes: 0 for ok/noop/needs_approval (all normal outcomes,
the last one parks work in the approval queue), 1 for a job error, 2 for a
usage or configuration error or a refused write (another process holds the
ledger lock: ``queue approve|reject`` and ``status`` take the runner's lock
before they commit, so their commit never sweeps a running job's rows).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from ..ledger.ledger import LedgerRestoreError
from ..llm import runner_cli
from .config import TenantNotFoundError
from .kit import SHAPES
from .registry import (
    UnknownAgentError,
    UnknownJobError,
    list_agents,
    load_agent_jobs,
)
from .runner import (
    LOCK_ANOMALY,
    RETRY_ATTEMPT,
    LedgerLocked,
    ledger_write_lock,
    resume_due,
    run,
)
from .version import VersionAction, describe


def _parse_params(pairs: list[str]) -> dict:
    params: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise ValueError(f"--param expects K=V, got {pair!r}")
        key, value = pair.split("=", 1)
        params[key] = value
    return params


def _cmd_agents() -> int:
    agents = list_agents()
    if not agents:
        print("no agents discovered under core/agents/")
        return 0
    for agent in agents:
        jobs = ", ".join(sorted(load_agent_jobs(agent)))
        print(f"{agent}: {jobs}")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    try:
        params = _parse_params(args.param)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        result = run(
            args.tenant,
            args.agent,
            args.job,
            shadow=args.shadow,
            params=params,
            ledger_dir=args.ledger_dir,
        )
    except (
        TenantNotFoundError,
        UnknownAgentError,
        UnknownJobError,
        ValueError,
        LedgerRestoreError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(result.model_dump_json(indent=2))
    else:
        shadow = " [shadow]" if result.shadow else ""
        print(f"{result.agent}/{result.job} @ {result.tenant}{shadow}: {result.status}")
        if result.summary:
            print(f"  {result.summary}")
        if result.actions:
            print(f"  actions: {len(result.actions)}")
        if result.approvals_needed:
            print(f"  approvals queued: {len(result.approvals_needed)}")
        if result.anomalies:
            print(f"  anomalies: {len(result.anomalies)}")
        if result.commit:
            print(f"  commit: {result.commit[:12]}")

    return 1 if result.status == "error" else 0


def _cmd_jobs_resume(args: argparse.Namespace) -> int:
    """Execute the tenant's due retries (docs/retries.md). A scheduler runs
    this every 15 minutes (row 7.21 wires the cadence); nothing due is one
    line and exit 0. ``--now`` forces every scheduled retry due: the
    owner's tool for "run it again now". Exit 1 when a retry ended in
    error (it is a job error like any other)."""
    try:
        results = resume_due(args.tenant, force=args.now, ledger_dir=args.ledger_dir)
    except (TenantNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print("[" + ",\n".join(r.model_dump_json(indent=2) for r in results) + "]")
    elif not results:
        print(f"no retries due for {args.tenant}")
    else:
        for result in results:
            attempt = next((a.detail for a in result.anomalies if a.code == RETRY_ATTEMPT), "")
            shadow = " [shadow]" if result.shadow else ""
            line = f"{result.agent}/{result.job} @ {result.tenant}{shadow}: {result.status}"
            print(f"{line} ({attempt})" if attempt else line)
            if result.summary:
                print(f"  {result.summary}")
            for anomaly in result.anomalies:
                if anomaly.code.startswith("engine.retry.") and anomaly.code != RETRY_ATTEMPT:
                    print(f"  {anomaly.code}: {anomaly.detail}")
    return 1 if any(r.status == "error" for r in results) else 0


def _cmd_runner_run(args: argparse.Namespace) -> int:
    """One agentic session through :mod:`core.llm.runner` (row 7.17). The
    exit codes are the headless wrappers' vocabulary: 0 FINISHED, 75 SKIPPED
    (a precondition; nothing is wrong), 1 FAILED, 2 a usage or configuration
    error. Nothing here opens the ledger: a runner session is not a job."""
    from ..llm.policy import LlmPolicyError
    from ..llm.runner import SkillFormatError
    from ..llm.runner_cli import LaneError, run_lane

    try:
        return run_lane(
            args.tenant,
            args.skill,
            params=args.param,
            adapter=args.adapter,
            as_json=args.json,
        )
    except (
        LaneError,
        SkillFormatError,
        LlmPolicyError,
        TenantNotFoundError,
        FileNotFoundError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _refuse_locked(exc: LedgerLocked) -> int:
    """The runner's structured refusal, in CLI clothing: nothing was written."""
    print(f"error: refused ({LOCK_ANOMALY}): {exc}", file=sys.stderr)
    return 2


# Card params the queue writes itself on a human-only card (#356) or a
# door-witnessed decision (#465); an override naming one is refused, so
# nobody types their way past the gate.
GATE_PARAMS = frozenset(
    {
        "human_only",
        "decided_via",
        "witness",
        "witness_at",
        "witness_kind",
        "witness_resource",
        "vouched_by",
        "vouchers",
    }
)


def _operator_at_terminal() -> bool:
    """A person is at a terminal: stdin is a TTY. Headless lanes and agent
    shells run with stdin not a terminal, so this is false for every one of
    them. It stops the honest lanes and accidents; a shell that fakes a TTY
    is the sandbox's job (#353/#359), and the auditor still sees the card."""
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _human_only_refusal(row: dict, verb: str, overrides: dict) -> str | None:
    """Why this decision may not proceed, or None. Confirms with the person
    at the terminal by having them type the card number back."""
    if not _operator_at_terminal():
        return (
            f"approval #{row['id']} ({row['action_type']}) is a human-only card: "
            f"a person decides it at a terminal (`engine queue {verb} ... --id {row['id']}`)"
        )
    typed = input(
        f"#{row['id']} {row['action_type']} is human-only. Type {row['id']} to {verb} it: "
    )
    if typed.strip() != str(row["id"]):
        return f"approval #{row['id']}: confirmation did not match; nothing decided"
    overrides["decided_via"] = "terminal"
    return None


ROUTE_COMMANDS = ("waiting", "delegate", "revoke", "handoff", "on-it")


def _cmd_route(args: argparse.Namespace, root: Path) -> int:  # noqa: C901  # one branch per verb
    """The route commands (#436), only for a tenant with authority.toml:
    ``waiting`` reads; ``delegate``, ``revoke``, ``handoff`` and ``on-it``
    are a person's acts at a terminal."""
    import json
    from dataclasses import replace
    from datetime import date

    from ..authority.routing import authority_for, due_date
    from ..ledger import Ledger
    from . import clock
    from .authority_gate import load_delegations, load_policy, route_of, waiting
    from .kit import KitError
    from .runner import run

    try:
        authority = load_policy(args.tenant)
    except (KitError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if authority is None:
        print(
            f"error: {args.tenant} has no authority.toml; cards there wait for one decision "
            "and nothing routes",
            file=sys.stderr,
        )
        return 2
    policy = authority.policy
    who = policy.people.get(args.as_principal)
    now = clock.utc_now()
    today = now.date()
    if args.queue_command == "waiting":
        with Ledger.open(root) as ledger:
            items = waiting(policy, ledger, args.tenant, today=today, now=now.isoformat())
            notes = ledger.conn.execute(
                "SELECT payload_json FROM events WHERE tenant = ? AND event_type = "
                "'route.reminder' ORDER BY id",
                (args.tenant,),
            ).fetchall()
        latest = {}
        for r in notes:
            payload = json.loads(r["payload_json"])
            if payload["to"] == args.as_principal:
                latest[payload["card"]] = payload["text"]
        mine = [i for i in items if args.as_principal in i["waiting_on"]]
        if not mine:
            print(f"nothing is waiting on {args.as_principal}")
        for i in mine:
            print(f"#{i['card']} {i['action_type']}: {i['step']}, by {i['due']}")
            if i["card"] in latest:
                print(f"  {latest[i['card']]}")
        return 0
    if who is None:
        print(f"error: {args.as_principal!r} is not a person in authority.toml", file=sys.stderr)
        return 2
    if not _operator_at_terminal():
        print(
            f"error: {args.queue_command} is a person's act: run it at a terminal",
            file=sys.stderr,
        )
        return 2
    if args.queue_command in ("delegate", "revoke"):
        params = {"by": args.as_principal, "today": today.isoformat()}
        if args.queue_command == "delegate":
            params |= {"to": args.to, "role": args.role, "until": args.until}
        else:
            params["delegation"] = args.delegation
        result = run(
            args.tenant, "routing", args.queue_command, params=params, ledger_dir=args.ledger_dir
        )
        print(result.summary)
        return 0 if result.status == "ok" else 2
    try:
        with ledger_write_lock(root), Ledger.open(root) as ledger:
            pending = ledger.list_approvals(args.tenant, status="pending")
            row = next((r for r in pending if r["id"] == args.id), None)
            if row is None:
                raise ValueError(f"no pending approval #{args.id} for {args.tenant}")
            _rule, route = route_of(policy, row, now.isoformat())
            delegations = load_delegations(ledger, args.tenant)
            if authority_for(policy, args.as_principal, route.step, delegations, today) is None:
                raise ValueError(f"#{args.id} is not waiting on {args.as_principal}")
            if args.queue_command == "handoff":
                if authority_for(policy, args.to, route.step, delegations, today) is None:
                    raise ValueError(f"{args.to} cannot decide this step, so cannot take it")
                route = replace(route, handoff_to=args.to)
                said = f"handed to {args.to}"
            else:
                by = date.fromisoformat(args.by)
                if by <= today or (by - today).days > 30:
                    raise ValueError("on it by a date in the next 30 days")
                route = replace(route, on_it_until=by.isoformat(), on_it_set=today.isoformat())
                said = f"{args.as_principal} is on it by {by.isoformat()} (the clock waits)"
            ledger.update_pending_params(args.tenant, args.id, route.to_params())
            ledger.commit(
                agent="queue",
                job=args.queue_command,
                idempotency_key=f"approval-{args.id}-{args.queue_command}-{now.isoformat()}",
                summary=f"approval #{args.id}: {said}",
            )
    except LedgerLocked as exc:
        return _refuse_locked(exc)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"approval #{args.id}: {said}; due {due_date(route, row['params'], policy.routing)}")
    return 0


def _cmd_tracker(args: argparse.Namespace) -> int:
    """A token for one person's phone tracker (core/mileage): shown once, kept
    only as its hash in the tenant's trackers.toml."""
    from ..mileage.trackers import add_tracker
    from .config import tenant_dir

    token = add_tracker(tenant_dir(args.tenant), args.person)
    print(f"Tracker for {args.person}. In the Traccar Client app on their phone:")
    print(f"  Device identifier: {token}")
    print(
        f"  Server URL: https://{args.domain}/track"
        if args.domain
        else "  Server URL: https://<the box>/track"
    )
    print("The box keeps miles and times only; this identifier is shown once.")
    return 0


def _cmd_receipt_link(args: argparse.Namespace) -> int:
    """A person's standing home-screen receipt link (core/tools/receipts.py,
    chunk 10): a new one, shown once, or ``--revoke`` to retire theirs."""
    from ..tools.receipts import HOME_PATH, add_home_link, drop_home_link
    from .config import tenant_dir

    folder = tenant_dir(args.tenant)
    if args.revoke:
        n = drop_home_link(folder, args.person)
        print(f"{args.person}: {n} receipt link{'' if n == 1 else 's'} retired")
        return 0
    key = add_home_link(folder, args.person)
    where = f"https://{args.domain}" if args.domain else "https://<the box>"
    print(f"Receipt page for {args.person} (shown once; their older one stops working):")
    print(f"  {where}{HOME_PATH}{key}")
    return 0


def _cmd_people(args: argparse.Namespace) -> int:
    """New people from one list, one line each (core/onboarding/roster.py,
    chunk 11): ``Ivy Chen = missionary in Thailand, ivy@example.org,
    looked after by Ann Lee``. ``-`` reads the list from stdin."""
    from ..onboarding import OnboardingError
    from ..onboarding.roster import add_people, said_back

    if args.list == "-":
        said = sys.stdin.read()
    else:
        try:
            said = Path(args.list).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    try:
        added = add_people(args.tenant, said, check=args.check)
    except OnboardingError as exc:
        print(f"Nobody was added. {exc}", file=sys.stderr)
        return 1
    for line in said_back(added, check=args.check):
        print(line)
    return 0


def _cmd_calendar_link(args: argparse.Namespace) -> int:
    """Retire a person's calendar feed link (core/tools/calendar_feed.py,
    chunk 11): a lost phone, or someone who left. A new one comes from
    their own setup's calendar step."""
    from ..tools.calendar_feed import drop_calendar_link
    from .config import tenant_dir

    n = drop_calendar_link(tenant_dir(args.tenant), args.person)
    print(f"{args.person}: {n} calendar link{'' if n == 1 else 's'} retired")
    return 0


def _cmd_notices(args: argparse.Namespace) -> int:
    """The notices the routing lane recorded in the last week, one JSON line
    each, for ``doorkeeper send-notices`` (which sends each once). With
    ``--packs``, each month's board pack a notice names is written there
    first, as the PDF the doorkeeper attaches."""
    import json
    from datetime import UTC, date, datetime, timedelta

    from ..agents.routing.jobs import notices_since
    from ..ledger import Ledger
    from .runner import resolve_ledger_root

    since = (date.today() - timedelta(days=args.days)).isoformat()
    with Ledger.open(resolve_ledger_root(args.tenant, args.ledger_dir)) as ledger:
        notices = notices_since(ledger.read_event_log(), since)
    if args.packs:
        _write_packs(args, notices)
    # A missionary who finished (or stopped at) her own setup questions, to
    # whoever sent her the link (core/onboarding/interview.py, board c49).
    from ..onboarding import interview

    book = interview.InterviewBook(resolve_ledger_root(interview.INTERVIEWS, args.ledger_dir))
    started = datetime.combine(date.fromisoformat(since), datetime.min.time(), UTC)
    notices = [*notices, *interview.notices(book, started.timestamp())]
    for notice in notices:
        print(json.dumps(notice, sort_keys=True))
    return 0


def _cmd_queue(args: argparse.Namespace) -> int:
    from ..ledger import Ledger
    from .runner import resolve_ledger_root

    root = resolve_ledger_root(args.tenant, args.ledger_dir)
    if args.queue_command in ROUTE_COMMANDS:
        return _cmd_route(args, root)
    if args.queue_command == "list":
        with Ledger.open(root) as ledger:
            rows = ledger.list_approvals(args.tenant, status=args.status)
        if not rows:
            print(f"approval queue for {args.tenant}: empty")
            return 0
        for r in rows:
            params = ", ".join(f"{k}={v}" for k, v in r["params"].items())
            print(f"#{r['id']} [{r['status']}] {r['action_type']} ({params})")
        return 0
    # approve / reject record the decision; the owning job's next run
    # executes it. They write and commit the ledger, so they hold the run
    # lock (#136). An agent may declare approval-time checks (APPROVAL_CHECKS
    # in its jobs.py) for cards whose approval must name a fact; a refused
    # approval is a clean error and the card stays pending.
    decision = "approved" if args.queue_command == "approve" else "rejected"
    # authority.toml, when the tenant has one, decides who may decide (#435);
    # without it this command is exactly what it was. The decision itself is
    # the one path the box's door shares (core/engine/queue_decide.py).
    from .authority_gate import DECISION_PARAMS, load_policy
    from .kit import KitError
    from .queue_decide import decide_in_ledger

    try:
        authority = load_policy(args.tenant)
    except (KitError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if authority is None and args.as_principal:
        print(
            f"error: {args.tenant} has no authority.toml, so --as names no one; decide without it",
            file=sys.stderr,
        )
        return 2
    gated = GATE_PARAMS | DECISION_PARAMS if authority is not None else GATE_PARAMS
    try:
        with ledger_write_lock(root), Ledger.open(root) as ledger:
            try:
                overrides = _parse_params(getattr(args, "param", []) or [])
                gate_fields = sorted(gated & set(overrides))
                if gate_fields:
                    raise ValueError(
                        f"approval #{args.id}: {', '.join(gate_fields)} is set by the queue, "
                        "never by --param"
                    )
                outcome = decide_in_ledger(
                    ledger,
                    args.tenant,
                    args.id,
                    decision,
                    authority=authority,
                    principal=args.as_principal or "",
                    overrides=overrides,
                    at_terminal=_operator_at_terminal(),
                    confirm_human_only=lambda row, o: _human_only_refusal(
                        row, args.queue_command, o
                    ),
                )
            except (LookupError, ValueError) as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
    except LedgerLocked as exc:
        return _refuse_locked(exc)
    if outcome.vote is not None:
        print(
            f"approval #{args.id}: {args.as_principal}'s yes is recorded; "
            f"now {outcome.vote.step}, waiting on {outcome.waiting}"
        )
        return 0
    overrides = outcome.overrides
    noted = f" ({', '.join(f'{k}={v}' for k, v in overrides.items())})" if overrides else ""
    print(f"approval #{args.id} {decision} ({outcome.action_type}){noted}")
    return 0


def _cmd_shadow_diff(args: argparse.Namespace) -> int:
    from datetime import UTC, datetime

    from ..agents.ap.shadow_diff import render_markdown, run_shadow_diff
    from ..ledger import Ledger
    from .config import load_tenant
    from .guard import WriteGuard
    from .runner import resolve_ledger_root

    if args.agent != "ap":
        print(f"error: shadow-diff supports agent 'ap' (got {args.agent!r})", file=sys.stderr)
        return 2
    tenant = load_tenant(args.tenant)
    legacy = args.legacy_xlsx or tenant.ap.legacy_ledger_xlsx
    if not legacy:
        print(
            "error: no legacy ledger; set [ap].legacy_ledger_xlsx or pass --legacy-xlsx",
            file=sys.stderr,
        )
        return 2
    root = resolve_ledger_root(args.tenant, args.ledger_dir)
    guard = WriteGuard(tenant.ap.protected_paths, allowed=tenant.ap.allowed_paths)
    with Ledger.open(root) as ledger:
        out = (
            None
            if args.no_save
            else root / "shadow-reports" / f"parity-{datetime.now(UTC).date().isoformat()}.md"
        )
        report, written = run_shadow_diff(
            tenant_slug=args.tenant,
            ledger=ledger,
            legacy_xlsx=legacy,
            guard=guard,
            since=args.since or "",
            out_path=out,
        )
    print(render_markdown(report, tenant=args.tenant, since=args.since or ""))
    if written:
        print(f"(saved to {written})")
    return 0 if report.is_parity else 1


def _cmd_sweep(args: argparse.Namespace) -> int:
    from datetime import UTC, datetime

    from ..agents.ap.sweep import render_markdown, run_sweep
    from ..ledger import Ledger
    from .config import load_tenant
    from .guard import WriteGuard
    from .runner import resolve_ledger_root

    if args.agent != "ap":
        print(f"error: sweep supports agent 'ap' (got {args.agent!r})", file=sys.stderr)
        return 2
    tenant = load_tenant(args.tenant)
    landing = args.landing_dir or tenant.ap.landing_dir
    if not landing:
        print(
            "error: no landing directory; set [ap].landing_dir or pass --landing-dir",
            file=sys.stderr,
        )
        return 2
    root = resolve_ledger_root(args.tenant, args.ledger_dir)
    guard = WriteGuard(tenant.ap.protected_paths, allowed=tenant.ap.allowed_paths)
    with Ledger.open(root) as ledger:
        out = (
            None
            if args.no_save
            else root / "shadow-reports" / f"sweep-{datetime.now(UTC).date().isoformat()}.md"
        )
        report, written = run_sweep(
            tenant_slug=args.tenant,
            ledger=ledger,
            landing_dir=landing,
            guard=guard,
            out_path=out,
        )
    print(render_markdown(report, tenant=args.tenant))
    if written:
        print(f"(saved to {written})")
    return 0 if report.is_clean else 1


def _cmd_status(args: argparse.Namespace) -> int:
    from ..agents.ap.status import ALL_STATUSES
    from .runner import resolve_ledger_root

    if args.scheduled:
        status_to = "Scheduled"
    elif args.paid:
        status_to = "Paid"
    else:
        status_to = args.set
    if status_to not in ALL_STATUSES:
        print(
            f"error: unknown status {status_to!r}; known: {', '.join(sorted(ALL_STATUSES))}",
            file=sys.stderr,
        )
        return 2

    root = resolve_ledger_root(args.tenant, args.ledger_dir)
    try:
        with ledger_write_lock(root):
            return _status_locked(args, root, status_to)
    except LedgerLocked as exc:
        return _refuse_locked(exc)


def _status_locked(args: argparse.Namespace, root: Path, status_to: str) -> int:
    from ..agents.ap import store
    from ..agents.ap.status import InvalidTransition
    from ..ledger import Ledger

    with Ledger.open(root) as ledger:
        rows = store.invoices_by_number(ledger, args.tenant, args.ref, vendor=args.vendor)
        if not rows:
            print(f"error: no invoice {args.ref!r} for tenant {args.tenant!r}", file=sys.stderr)
            return 2
        if len(rows) > 1:
            vendors = ", ".join(sorted(r["vendor"] for r in rows))
            print(
                f"error: invoice {args.ref!r} is ambiguous across vendors ({vendors}); "
                f"add --vendor",
                file=sys.stderr,
            )
            return 2
        row = rows[0]
        was = row["status"]
        check_ref = str(getattr(args, "check", "") or "")
        pay_date = str(getattr(args, "date", "") or "")
        if was == status_to and (check_ref or pay_date):
            # The row already wears this status; the owner is adding the
            # instrument (W2: the check number a Scheduled row was paid with,
            # which nothing else records before the money clears).
            store.record_payment_details(
                ledger, invoice_id=row["id"], payment_date=pay_date, check_ref=check_ref
            )
            ledger.commit(
                agent="ap",
                job="status",
                idempotency_key=f"ap-status-{row['id']}-{status_to}-{check_ref}-{pay_date}",
                summary=f"payment details {row['vendor']} / {row['invoice_number']}",
            )
            print(
                f"{row['vendor']} / {row['invoice_number']}: already {was}; recorded "
                + " ".join(
                    part
                    for part in (
                        f"check {check_ref}" if check_ref else "",
                        f"date {pay_date}" if pay_date else "",
                    )
                    if part
                )
            )
            return 0
        try:
            applied = store.update_status(
                ledger, invoice_id=row["id"], status_to=status_to, actor="owner"
            )
        except InvalidTransition as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if applied and (check_ref or pay_date):
            store.record_payment_details(
                ledger, invoice_id=row["id"], payment_date=pay_date, check_ref=check_ref
            )
        if not applied:
            # update_status returns False when this exact transition is already
            # recorded (the history key is from->to), so the status did NOT
            # change. Report that honestly instead of printing a false success.
            print(
                f"{row['vendor']} / {row['invoice_number']}: no change; "
                f"transition {was} -> {status_to} was already recorded",
                file=sys.stderr,
            )
            return 2
        ledger.commit(
            agent="ap",
            job="status",
            idempotency_key=f"ap-status-{row['id']}-{status_to}",
            summary=f"status {row['vendor']} / {row['invoice_number']}: {was} -> {status_to}",
        )
    print(f"{row['vendor']} / {row['invoice_number']}: {was} -> {status_to}")
    return 0


def _cmd_status_page(args: argparse.Namespace) -> int:
    """``engine status-page <tenant>``: render the read-only page (row 7.24).

    A reader, so it takes no run lock and writes no ledger row; the only
    file it touches is the page itself. The name is ``status-page`` because
    ``engine status`` is the owner's invoice write-back and keeps it
    (docs/decisions/2026-09-16-the-status-page-is-a-reader.md).
    """
    from .config import load_tenant
    from .runner import resolve_ledger_root
    from .status import (
        StatusPageError,
        auditor_report_dir,
        default_out_path,
        render_status,
        write_status,
    )

    try:
        tenant = load_tenant(args.tenant)
    except (TenantNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    report_dir = args.report_dir if args.report_dir is not None else auditor_report_dir(args.tenant)
    try:
        page = render_status(
            tenant,
            ledger_root=resolve_ledger_root(args.tenant, args.ledger_dir),
            report_dir=report_dir,
        )
    except StatusPageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.stdout:
        print(page)
        return 0
    out = Path(args.out) if args.out else default_out_path(tenant)
    if out is None:
        print(
            f"error: tenant {args.tenant!r} configures no [close].report_dir; "
            "name the file with --out PATH (or print it with --stdout)",
            file=sys.stderr,
        )
        return 2
    print(write_status(page, out))
    return 0


def _cmd_close_preflight(args: argparse.Namespace) -> int:
    """Run the close preflight through the runner (so the ceremony is on the
    ledger record) and exit by the legacy close script's contract: 0 all OK, 1 WARN
    present, 2 BLOCK present. The verdict rides in the run summary."""
    params = {}
    if args.month:
        params["month"] = args.month
    if args.statement_balance is not None:
        params["statement_balance"] = args.statement_balance
    try:
        result = run(args.tenant, "close", "preflight", params=params, ledger_dir=args.ledger_dir)
    except (
        TenantNotFoundError,
        UnknownAgentError,
        UnknownJobError,
        ValueError,
        LedgerRestoreError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(result.summary)
    for action in result.actions:
        print(f"  {action}")
    if result.status == "error":
        return 2
    code = 0
    if "verdict BLOCK" in result.summary:
        code = 2
    elif "verdict WARN" in result.summary:
        code = 1
    if args.packet and code != 2:
        packet = run(args.tenant, "close", "packet", params=params, ledger_dir=args.ledger_dir)
        if packet.status == "error":
            # The preflight verdict is not the exit code when the packet leg
            # failed: a workbook that did not render is a BLOCK on the close.
            print(f"error: {packet.summary}", file=sys.stderr)
            return 2
        print(packet.summary)
        for action in packet.actions:
            print(f"  {action}")
    return code


def _cmd_dismiss(args: argparse.Namespace) -> int:
    if not args.file and not args.all:
        print("error: dismiss needs a file name or --all", file=sys.stderr)
        return 2
    try:
        result = run(
            args.tenant,
            "ap",
            "dismiss",
            shadow=True,
            params={"file": args.file or "", "all": args.all},
            ledger_dir=args.ledger_dir,
        )
    except (
        TenantNotFoundError,
        UnknownAgentError,
        UnknownJobError,
        ValueError,
        LedgerRestoreError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if result.status == "error":
        print(f"error: {result.summary}", file=sys.stderr)
        return 1
    print(result.summary)
    return 0


def _cmd_identify(args: argparse.Namespace) -> int:
    params = {
        "file": args.file,
        "vendor": args.vendor,
        "invoice_number": args.number,
        "amount": args.amount,
    }
    try:
        result = run(
            args.tenant, "ap", "identify", shadow=True, params=params, ledger_dir=args.ledger_dir
        )
    except (
        TenantNotFoundError,
        UnknownAgentError,
        UnknownJobError,
        ValueError,
        LedgerRestoreError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if result.status == "error":
        print(f"error: {result.summary}", file=sys.stderr)
        return 1
    print(result.summary)
    return 0


def _cmd_mail_consent(args: argparse.Namespace) -> int:
    """The owner's one-time device-code consent for the tenant mailbox.

    Interactive by design (no job calls it): prints the verification URL and
    the user code, blocks until the sign-in completes, persists the MSAL
    cache under the keychain names in tenant.toml ``[mail]``, then proves
    the jobs' own provider can acquire silently from that entry. Stdout
    carries the URL, the code, the signed-in account, and the expiry;
    never a token.
    """
    import os
    from datetime import UTC, datetime, timedelta

    from ..adapters.mail import PROVIDERS
    from ..contracts.mail import MailAuthError
    from .config import load_tenant

    try:
        tenant = load_tenant(args.tenant)
    except (TenantNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    mail = tenant.mail
    provider = mail.provider.strip().lower()
    if provider not in PROVIDERS:
        print(
            f"error: [mail].provider for tenant {args.tenant!r} is {mail.provider!r}; "
            f"set it to one of {', '.join(PROVIDERS)} in tenant.toml (the engine ships no default)",
            file=sys.stderr,
        )
        return 2
    required = (
        ("client_id", "tenant_id", "keychain_service", "keychain_account")
        if provider == "graph"
        else ("client_id", "client_secret_env", "keychain_service", "keychain_account")
    )
    missing = [name for name in required if not getattr(mail, name)]
    if missing:
        print(
            f"error: [mail] config incomplete for tenant {args.tenant!r}: "
            f"set {', '.join(missing)} in tenant.toml",
            file=sys.stderr,
        )
        return 2
    extra = [x for x in getattr(args, "scope", []) if x not in mail.scopes]
    scopes = list(mail.scopes) + extra

    def graph_consent():
        from ..adapters.graph_mail import device_code_consent, keychain_token_provider

        names = {
            "client_id": mail.client_id,
            "tenant_id": mail.tenant_id,
            "scopes": scopes,
            "keychain_service": mail.keychain_service,
            "keychain_account": mail.keychain_account,
        }

        def prompt(verification_uri: str, user_code: str, expires_in: int) -> None:
            print(f"Open {verification_uri} and enter the code {user_code}")
            print(
                f"(the code is good for {max(expires_in // 60, 1)} minutes; "
                f"sign in as {mail.keychain_account}; waiting for the sign-in to complete)"
            )
            sys.stdout.flush()

        return device_code_consent(prompt=prompt, **names), keychain_token_provider(**names)

    def gmail_consent():
        from ..adapters.gmail import loopback_consent, refresh_token_provider

        secret = os.environ.get(mail.client_secret_env, "")
        if not secret:
            raise MailAuthError(
                f"[mail].client_secret_env {mail.client_secret_env} is not set in this shell"
            )
        names = {
            "client_id": mail.client_id,
            "client_secret": secret,
            "keychain_service": mail.keychain_service,
            "keychain_account": mail.keychain_account,
        }

        def prompt(auth_url: str) -> None:
            print(f"Open {auth_url}")
            print(
                f"(sign in as {mail.keychain_account} and allow the mailbox scopes; "
                "waiting for Google to send the browser back here)"
            )
            sys.stdout.flush()

        consent = loopback_consent(prompt=prompt, scopes=scopes, **names)
        return consent, refresh_token_provider(**names)

    started = datetime.now(UTC)
    try:
        consent, verify = graph_consent() if provider == "graph" else gmail_consent()
    except MailAuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    expires_at = (started + timedelta(seconds=consent.expires_in)).replace(microsecond=0)
    print(f"signed in as {consent.username or '(no username claim)'}")
    if consent.username and consent.username.lower() != mail.keychain_account.lower():
        print(
            f"warning: signed-in account {consent.username} differs from [mail].keychain_account "
            f"{mail.keychain_account}; the jobs will read {consent.username}'s mailbox",
            file=sys.stderr,
        )
    try:
        verify()
    except MailAuthError as exc:
        print(
            f"error: consent stored but the engine cannot use it yet: {exc}",
            file=sys.stderr,
        )
        return 1
    print(
        f"verified: silent acquisition from the keychain succeeded; the first access token "
        f"expires {expires_at.isoformat()} and the cached consent renews it unattended"
    )
    return 0


def _cmd_init(args: argparse.Namespace) -> int:
    """``engine init <slug>``: render a tenant from the archetype templates,
    create its data tree and ledger, run the first audit. Exit 2 on a
    refusal (nothing written), 1 when the tenant exists but its first audit
    failed (re-run ``auditor run <slug> --local-only`` after fixing it)."""
    from .init import InitError, init_tenant

    try:
        result = init_tenant(
            args.slug,
            archetype=args.archetype,
            shape=args.shape,
            root=args.root,
            data_root=args.data_root,
            legal_name=args.legal_name,
            timezone=args.timezone,
            fiscal_year_start=args.fiscal_year_start,
            ledger_dir=args.ledger_dir,
            store_dir=args.store_dir,
            run_audit=not args.no_audit,
        )
    except InitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(
        f"tenant {result.slug} (archetype {result.archetype}, shape {result.shape}) "
        f"created at {result.tenant_dir}"
    )
    for path in result.files:
        print(f"  {path.relative_to(result.tenant_dir).as_posix()}")
    print(f"data root: {result.data_root} ({len(result.folders)} folders)")
    print(f"ledger: {result.ledger_root}")
    if result.audit_ran:
        if not result.audit_ok:
            print("first audit FAILED:", file=sys.stderr)
            print(result.audit_output, file=sys.stderr)
            return 1
        print(f"first audit: {result.report_path}")
    print(
        f"next: edit {result.tenant_dir / 'tenant.toml'} (people, accounts, bank export), "
        f"drop a file into {result.data_root / 'inbox'}, then "
        f"`engine run {result.slug} ap intake` from this directory"
    )
    return 0


def _obligations_file(tenant: str, cfg: Any, given: str | None = None) -> Path:
    """The tenant's obligations file: the one named, else the tenant's own."""
    from .config import tenant_dir

    if given:
        return Path(given)
    if cfg.deadlines.file:
        return Path(cfg.deadlines.file)
    return tenant_dir(tenant) / "obligations.toml"


def _cmd_talk(args: argparse.Namespace) -> int:
    """Talk to Tim in the terminal, as one person (core/talk): the same
    read tools ``engine mcp --as`` serves, held by Tim's own voice."""
    from ..talk import reply
    from ..tools.catalog import Tools
    from ..tools.viewer import ViewerError, viewer_for
    from .clock import local_today
    from .config import load_tenant
    from .runner import resolve_ledger_root

    cfg = load_tenant(args.tenant)
    try:
        viewer = viewer_for(args.tenant, args.as_person)
    except ViewerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    ledger_root = resolve_ledger_root(args.tenant, args.ledger_dir)
    tools = Tools(
        args.tenant,
        ledger_root=ledger_root,
        obligations_file=_obligations_file(args.tenant, cfg),
        today=local_today(cfg.identity.timezone),
        lead_days=tuple(cfg.deadlines.lead_days),
        viewer=viewer,
        ministry=cfg.identity.legal_name,
    )

    def one(said: str) -> bool:
        answer = reply(tools, said, tenant=args.tenant, ledger_root=ledger_root)
        print(answer.say)
        return answer.status == "ok"

    if args.say is not None:
        return 0 if one(args.say) else 1
    print(f"Talking to Tim as {args.as_person}. An empty line ends it.", file=sys.stderr)
    for line in sys.stdin:
        if not line.strip():
            break
        one(line.strip())
    return 0


def _cmd_spend(args: argparse.Namespace) -> int:
    """What a ministry's models cost this month, by job type, with the cap
    and Tim's soft line (card V3): one plain line, or JSON for the board."""
    import json

    from ..talk.meter import Meter, spend, spend_line
    from .config import load_tenant
    from .runner import resolve_ledger_root

    cfg = load_tenant(args.tenant)
    meter = Meter(resolve_ledger_root(args.tenant, args.ledger_dir))
    report = spend(cfg.llm, meter, args.tenant)
    print(json.dumps(report, indent=2) if args.json else spend_line(report))
    return 0


def _cmd_mcp(args: argparse.Namespace) -> int:
    """Serve the read-only tools over MCP on stdio (Ask Tim, step 1), or with
    ``--onboarding`` the tenantless onboarding conversation (#448)."""
    from ..tools.catalog import Tools
    from ..tools.mcp_stdio import McpServer
    from .clock import local_today
    from .config import load_tenant
    from .runner import resolve_ledger_root

    if args.onboarding:
        from ..onboarding.mcp import OnboardingTools

        if args.pack:
            from ..pack import PackError, check, load_pack

            try:
                problems = check(load_pack(args.pack))
            except PackError as exc:
                problems = [str(exc)]
            if problems:
                for problem in problems:
                    print(f"error: {problem}", file=sys.stderr)
                return 2
        onboarding = OnboardingTools(
            answers_dir=Path(args.answers_dir or "."),
            root=args.root,
            data_root=args.data_root,
            ledger_dir=args.ledger_dir,
            run_audit=not args.no_audit,
            pack=args.pack,
        )
        print("engine mcp: serving the onboarding conversation on stdio", file=sys.stderr)
        return McpServer(
            onboarding, name=onboarding.server_name, instructions=onboarding.instructions
        ).serve()
    if not args.tenant:
        print("error: name a tenant, or pass --onboarding to onboard a new one", file=sys.stderr)
        return 2
    cfg = load_tenant(args.tenant)
    viewer = None
    if args.as_person:
        from ..tools.viewer import ViewerError, viewer_for

        try:
            viewer = viewer_for(args.tenant, args.as_person)
        except ViewerError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    ob = _obligations_file(args.tenant, cfg, args.obligations_file)
    if args.today is not None:
        # A replay reads a past day's books as of that day (2026-10-12). The
        # HTTP door is what people use, so it always keeps the real clock.
        if args.http:
            print("error: --today is for stdio replays only, never --http", file=sys.stderr)
            return 2
        from datetime import date

        try:
            date.fromisoformat(args.today)
        except ValueError:
            print(f"error: --today {args.today!r} is not a day (YYYY-MM-DD)", file=sys.stderr)
            return 2
    if args.http:
        return _serve_mcp_http(args, cfg, ob)
    tools = Tools(
        args.tenant,
        ledger_root=resolve_ledger_root(args.tenant, args.ledger_dir),
        obligations_file=ob,
        today=args.today or local_today(cfg.identity.timezone),
        lead_days=tuple(cfg.deadlines.lead_days),
        viewer=viewer,
        ministry=cfg.identity.legal_name,
    )
    who = f" as {args.as_person}" if viewer else ""
    when = f", as of {args.today}" if args.today else ""
    print(f"engine mcp: serving {args.tenant} read-only{who} on stdio{when}", file=sys.stderr)
    return McpServer(tools).serve()


def _serve_mcp_http(args: argparse.Namespace, cfg: Any, ob: Path) -> int:
    """``engine mcp <tenant> --http``: the read tools on one HTTP endpoint,
    each request answered as the person its bearer token signs in
    (core/tools/mcp_http.py). Bind to localhost behind a TLS proxy."""
    import threading
    import time
    from urllib.parse import urlsplit

    from ..mileage import Tracker
    from ..tools.decide import finisher_for
    from ..tools.mcp_http import Introspection, RefuseAll, TokenFile, Verifier, make_server
    from ..tools.receipts import ReceiptDesk
    from ..tools.witness_desk import WitnessDesk
    from .clock import local_today
    from .private_file import state_folder
    from .runner import resolve_ledger_root

    if not args.resource:
        print("error: --http needs --resource, the endpoint's public https URL", file=sys.stderr)
        return 2
    if args.introspect and args.tokens:
        print("error: --introspect or --tokens, not both", file=sys.stderr)
        return 2
    verifier: Verifier = RefuseAll()
    desk = None
    who = "nobody (no --introspect or --tokens)"
    if args.introspect:
        if not args.introspect_secret:
            print(
                "error: --introspect needs --introspect-secret, the shared secret's file",
                file=sys.stderr,
            )
            return 2
        try:
            verifier = Introspection(
                url=args.introspect, secret_file=args.introspect_secret, resource=args.resource
            )
        except (OSError, ValueError) as err:
            print(f"error: {err}", file=sys.stderr)
            return 2
        who = f"people the sign-in service at {args.introspect} vouches for"
        # The same doorkeeper witnesses a person's own yes (Face ID): a tenant
        # whose authority.toml opened [presence] door gets decide_card.
        origin = "{0.scheme}://{0.netloc}".format(urlsplit(args.introspect))
        desk = WitnessDesk(url=origin, secret_file=args.introspect_secret)
    elif args.tokens:
        verifier, who = TokenFile(args.tokens), "invitation tokens"
    ledger_root = resolve_ledger_root(args.tenant, args.ledger_dir)
    kept = state_folder(ledger_root, args.tenant)
    server = make_server(
        args.tenant,
        tenants_root=None,
        ledger_root=ledger_root,
        obligations_file=ob,
        verifier=verifier,
        resource=args.resource,
        authorization_servers=list(args.authorization_server),
        host=args.bind,
        port=args.port,
        today=lambda: local_today(cfg.identity.timezone),
        lead_days=tuple(cfg.deadlines.lead_days),
        desk=desk,
        # On the box (a sign-in service vouches for people), a person who
        # files expenses can hand Tim a receipt from their phone's camera.
        # Kept beside the books (core.engine.private_file), so a room that naps
        # and wakes (shot 4, card H3) honors the link or button in her hand.
        receipts=ReceiptDesk(state=kept / "receipt-links.json") if desk is not None else None,
        month_key=kept / "month-page.key" if desk is not None else None,
        # And their phone's tracker app reports drives: miles and times only.
        tracker=Tracker() if desk is not None else None,
    )
    if desk is not None:

        def _flush_every_minute() -> None:
            while True:
                time.sleep(60)
                server.flush_drives()

        threading.Thread(target=_flush_every_minute, daemon=True, name="drives").start()
    print(
        f"engine mcp: serving {args.tenant} on http://{args.bind}:{server.server_address[1]}"
        f"/mcp for {args.resource}; admitting {who}",
        file=sys.stderr,
    )
    # A person's tap finishes their decision (engine #480): one thread hears
    # each yes from the doorkeeper the moment it lands and writes it.
    finisher = finisher_for(args.tenant, tenants_root=None, ledger_root=ledger_root, desk=desk)
    if finisher is not None:
        finisher.start()
        print("engine mcp: the door's finisher is waiting for taps", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _cmd_door_token(args: argparse.Namespace) -> int:
    """``engine door-token <tenant> <person> --tokens FILE``: a new invitation
    token for one person in authority.toml. Prints the token once; the file
    keeps only its hash."""
    from ..tools.mcp_http import new_token
    from ..tools.viewer import ViewerError, viewer_for

    try:
        viewer_for(args.tenant, args.person)
    except ViewerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(new_token(args.tokens, args.person))
    return 0


def _cmd_capabilities(args: argparse.Namespace) -> int:
    """``engine capabilities``: what Tim can do today, built from the code."""
    import json

    from ..capabilities import build, markdown

    rules, asks = _rules_offered(args.tenant) if args.tenant else (True, True)
    page = build(args.shape, rules=rules, asks=asks)
    if args.format == "json":
        print(json.dumps(page, indent=1))
    else:
        print(markdown(page), end="")
    return 0


def _rules_offered(tenant: str) -> tuple[bool, bool]:
    """Whether the ministry's door offers the rules tools (it wrote rules
    down), and the ask for an OK (a rule asks for one first)."""
    from .authority_gate import load_policy

    policy = load_policy(tenant)
    if policy is None or policy.rules is None or policy.rules_error:
        return False, False
    return True, policy.rules.asks_first


def _cmd_board_pack(args: argparse.Namespace) -> int:
    """``engine board-pack``: the statements for a span as a page and a PDF
    (chunk c29), read through the same tool Tim answers with, as one person
    under the ministry's own rules. Writes two files; changes no books."""
    from ..tools.board_pack_page import html, pdf
    from .config import load_tenant

    tools = _tools_as(args)
    if tools is None:
        return 2
    cfg = load_tenant(args.tenant)
    pack = _pack_of(tools, args.as_person, args.period)
    if pack is None:
        return 2
    name = cfg.identity.legal_name or args.tenant
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"{args.tenant}-board-pack-{pack['from']}-to-{pack['through']}"
    (out / f"{stem}.html").write_text(html(pack, name), encoding="utf-8")
    (out / f"{stem}.pdf").write_bytes(pdf(pack, name))
    print(pack["says"])
    print(f"wrote {out / stem}.html and .pdf")
    return 0 if pack["matches_the_books"] and pack["balanced"] else 1


def _cmd_donor_letters(args: argparse.Namespace) -> int:
    """``engine donor-letters``: a year's donor letters as pages and PDFs
    (chunk c32), read through the same tool Tim answers with, as one person
    under the ministry's own rules: each donor's year-end giving statement,
    an acknowledgment of each gift of $250 or more, and a thank-you for each
    grant from a donor-advised fund. Writes files; changes no books; sends
    nothing."""
    import re
    from decimal import Decimal

    from ..agents.expenses import donors
    from ..tools import paper

    tools = _tools_as(args)
    if tools is None:
        return 2
    if "donor_letters" not in {s.name for s in tools.specs()}:
        print(f"error: {args.as_person} doesn't look over every month", file=sys.stderr)
        return 2
    got = tools.call("donor_letters", {"year": args.year})
    if "donors" not in got:
        print(f"error: {got.get('say', 'no donors')}", file=sys.stderr)
        return 2
    if got["needs"]:
        print(f"error: the letters still need {', '.join(got['needs'])}", file=sys.stderr)
        return 2
    head = donors.Letterhead(**got["letterhead"])
    today, year = got["as_of"], got["year"]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    def write(stem: str, title: str, blocks: list) -> None:
        name = re.sub(r"[^a-z0-9]+", "-", stem.casefold()).strip("-")
        base = out / f"{args.tenant}-{year}-{name}"
        base.with_suffix(".html").write_text(paper.html(title, blocks), encoding="utf-8")
        base.with_suffix(".pdf").write_bytes(paper.pdf(blocks))

    for d in got["donors"]:
        person = donors.Donor(
            d["name"],
            [
                donors.Gift(g["date"], int(Decimal(g["dollars"]) * 100), d["name"], False)
                for g in d["gifts"]
            ],
        )
        where = d.get("address", "")
        write(
            f"statement {d['name']}",
            f"{year} giving statement",
            donors.statement(person, head, year, today, where),
        )
        for g in person.acknowledged:
            write(
                f"acknowledgment {g.day} {d['name']}",
                "Gift acknowledgment",
                donors.acknowledgment(g, head, today, where),
            )
    for f in got["fund_grants"]:
        g = donors.Gift(f["date"], int(Decimal(f["dollars"]) * 100), f["from"], True)
        write(
            f"thank you {g.day} {g.donor}",
            "Thank you",
            donors.thank_you(g, head, today, f.get("address", "")),
        )
    print(got["says"])
    print(f"wrote the letters to {out}")
    return 0


def _cmd_tax_packet(args: argparse.Namespace) -> int:
    """``engine tax-packet``: a missionary's tax packet for a year as a page
    and a PDF (chunk c33), read through the same ``tax_packet`` tool Tim
    answers with, as one person under the ministry's own rules: the person
    themself, or someone they shared it with naming them with --person (c38).
    Writes files; changes no books; sends nothing."""
    import re

    from ..agents.expenses import packet
    from ..tools import paper

    tools = _tools_as(args)
    if tools is None:
        return 2
    if "tax_packet" not in {s.name for s in tools.specs()}:
        print(f"error: {args.as_person} has no tax packet to see", file=sys.stderr)
        return 2
    asked = {"year": args.year} | ({"person": args.person} if args.person else {})
    got = tools.call("tax_packet", asked)
    if "payers" not in got:
        print(f"error: {got.get('say', 'no packet')}", file=sys.stderr)
        return 2
    blocks = packet.page(got)
    name = re.sub(r"[^a-z0-9]+", "-", got["person"].casefold()).strip("-")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    base = out / f"{args.tenant}-{got['year']}-tax-packet-{name}"
    base.with_suffix(".html").write_text(paper.html(blocks[0][1], blocks), encoding="utf-8")
    base.with_suffix(".pdf").write_bytes(paper.pdf(blocks))
    print(got["says"])
    print(f"wrote {base}.html and .pdf")
    return 0


def _pack_of(tools, person: str, period: str) -> dict | None:
    """The board pack for ``period`` as ``person`` sees it, or None, said on
    stderr, when they may not see the books or the span has no statements."""
    if "board_pack" not in {s.name for s in tools.specs()}:
        print(f"error: {person} may not see the books", file=sys.stderr)
        return None
    pack = tools.call("board_pack", {"period": period})
    if "activities" not in pack:
        print(f"error: {pack.get('say', 'no statements for that span')}", file=sys.stderr)
        return None
    return pack


def _write_packs(args: argparse.Namespace, notices: list[dict]) -> None:
    """Each month's board pack a notice names, as ``board-pack-YYYY-MM.pdf``
    in ``--packs``, for the doorkeeper to attach (t-pack-by-email). Written
    once, as the first person it goes to (everyone mailed it sees every
    month, so it reads the same for each), and only when the statements
    agree with the books and balance: no file, no email, so the doorkeeper
    tries again the next day."""
    from ..tools.board_pack_page import pdf
    from .config import load_tenant

    out = Path(args.packs)
    for notice in notices:
        if notice.get("kind") != "board_pack":
            continue
        target = out / f"board-pack-{notice['month']}.pdf"
        if target.exists():
            continue
        tools = _tools_as(argparse.Namespace(**{**vars(args), "as_person": notice["to"]}))
        pack = _pack_of(tools, notice["to"], notice["month"]) if tools is not None else None
        if pack is None or not (pack["matches_the_books"] and pack["balanced"]):
            continue
        cfg = load_tenant(args.tenant)
        out.mkdir(parents=True, exist_ok=True)
        part = target.with_suffix(".part")
        part.write_bytes(pdf(pack, cfg.identity.legal_name or args.tenant))
        part.replace(target)


def _cmd_cpa_export(args: argparse.Namespace) -> int:
    """``engine cpa-export``: a year's books for an accountant (chunk c30),
    read through the same tool Tim answers with. Writes three files: the
    Beancount-format journal, the trial balance, and the filings' numbers."""
    import json

    tools = _tools_as(args)
    if tools is None:
        return 2
    if "cpa_export" not in {s.name for s in tools.specs()}:
        print(f"error: {args.as_person} doesn't look over every month", file=sys.stderr)
        return 2
    done = tools.call("cpa_export", {"year": args.year, "pay_kind": args.pay_kind})
    if "beancount" not in done:
        print(f"error: {done.get('say', 'no books for that year')}", file=sys.stderr)
        return 2
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"{args.tenant}-books-{args.year}"
    (out / f"{stem}.beancount").write_text(done["beancount"], encoding="utf-8")
    (out / f"{stem}-trial-balance.csv").write_text(done["trial_balance_csv"], encoding="utf-8")
    (out / f"{stem}-filings.json").write_text(
        json.dumps(done["filings"], indent=2) + "\n", encoding="utf-8"
    )
    print(done["says"])
    print(f"wrote {out / stem}.beancount, -trial-balance.csv and -filings.json")
    return 0 if done["round_trips"] and done["balanced"] and done["matches_the_books"] else 1


def _tools_as(args: argparse.Namespace):
    """The door's read tools as one person under the ministry's own rules,
    for a command that writes files from a tool's answer (board-pack,
    cpa-export); None, said, when the person isn't on the ministry's list."""
    from ..tools.catalog import Tools
    from ..tools.viewer import ViewerError, viewer_for
    from .clock import local_today
    from .config import load_tenant
    from .runner import resolve_ledger_root

    cfg = load_tenant(args.tenant)
    try:
        viewer = viewer_for(args.tenant, args.as_person)
    except ViewerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None
    return Tools(
        args.tenant,
        ledger_root=resolve_ledger_root(args.tenant, args.ledger_dir),
        viewer=viewer,
        today=local_today(cfg.identity.timezone),
    )


def _cmd_room(args: argparse.Namespace) -> int:
    """Pack a room to move, or restore one (core/engine/room.py); prints its counts."""
    import json

    from . import room

    try:
        if args.room_cmd == "pack":
            done = room.pack(args.folder, args.out)
        else:
            done = room.restore(args.packed, args.into)
    except room.PackError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"files": len(done["files"]), "tables": done["tables"]}, sort_keys=True))
    return 0


def _cmd_faq(args: argparse.Namespace) -> int:
    """``engine faq``: the questions people ask about Tim, with Tim's answers."""
    import json

    from ..capabilities.faq import faq_for, load_faq, markdown

    entries = load_faq()
    if args.tenant:
        # What that ministry's door offers, from the same page `engine
        # capabilities --tenant` builds (a guide for one ministry, 2026-10-15).
        from ..capabilities import build
        from ..talk import voice_on
        from .config import load_tenant

        rules, asks = _rules_offered(args.tenant)
        shape = args.shape or load_tenant(args.tenant).identity.shape or None
        page = build(shape, rules=rules, asks=asks)
        voice = voice_on(load_tenant(args.tenant).llm)  # card T2: his own page first
        entries = faq_for((t["name"] for t in [*page["ask"], *page["tell"]]), voice=voice)
    print(json.dumps(entries, indent=1) if args.format == "json" else markdown(entries), end="")
    return 0


def _cmd_interview(args: argparse.Namespace) -> int:
    """``engine interview new|list``: a missionary's own setup questions
    (core/onboarding/interview.py, board c49). ``new`` prints her link once;
    ``list`` says where each stands and, once she said yes, where her
    ministry file is."""
    import time

    from ..onboarding.interview import INTERVIEWS, InterviewBook
    from .runner import resolve_ledger_root

    book = InterviewBook(resolve_ledger_root(INTERVIEWS, args.ledger_dir))
    if args.interview_cmd == "new":
        base = args.base.rstrip("/")
        if not base.startswith("https://") and not base.startswith("http://127.0.0.1"):
            print("--base is the box's https address", file=sys.stderr)
            return 2
        try:
            token = book.mint(
                name=args.name, email=args.email, by=args.by, days=args.days, now=time.time()
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"{base}/start#{token}")
        return 0
    for row in book.rows():
        until = time.strftime("%Y-%m-%d", time.gmtime(row["expires"]))
        where = row["file"] or ", ".join(row["support"]) or f"open until {until}"
        print(f"{row['status']:8} {row['name']} <{row['email']}> {row['legal_name']}  {where}")
    return 0


def _cmd_rules(args: argparse.Namespace) -> int:
    """``engine rules <tenant>``: every policy rule that applies, highest
    layer first, in Tim's words with the words it came from."""
    from ..policy import PolicyError
    from .authority_gate import load_rules

    root = Path(args.root) if args.root else None
    try:
        book = load_rules(args.tenant, tenants_root=root)
    except (TenantNotFoundError, PolicyError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if book is None:
        print(f"{args.tenant} has no rules/ folder: no handbook rules apply.")
        return 0
    for layer in book.layers:
        print(f"{layer.document} ({layer.layer})")
        for rule in layer.rules:
            print(f"  {rule.id}: {rule.says}")
            print(f'    {rule.cite()}: "{rule.source.quote}"')
    return 0


def _cmd_pack(args: argparse.Namespace) -> int:
    """``engine pack check <folder>``: whether an agency pack can set up a
    ministry (core/pack). Every problem is printed; exit 1 while any remain."""
    from ..pack import PackError, check, load_pack

    try:
        pack = load_pack(args.folder)
    except PackError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    problems = check(pack)
    for problem in problems:
        print(f"  {problem}")
    if problems:
        print(f"{pack.name}: {len(problems)} to fix before it sets up a ministry")
        return 1
    print(
        f"{pack.name}: ready. {len(pack.roles)} roles, {len(pack.numbers)} numbers, "
        "each cited and read in its copy where one is kept."
    )
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    """``engine doctor <tenant>``: what this host is still missing (row
    7.21). Exit 0 when nothing is owed, 1 with one line per missing item, 2
    when the tenant itself cannot be read. Never prints a secret value."""
    from .doctor import run_doctor

    try:
        report = run_doctor(args.tenant, tenants_root=args.root, create_folders=args.create_folders)
    except (TenantNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(describe("engine"))
    for line in report.lines():
        print(line)
    if report.ok:
        return 0
    sys.stdout.flush()  # the checklist first, then the summary, in a terminal and in a log
    print(
        f"\n{len(report.missing)} item(s) missing; fix them or say so in "
        f"tenants/{args.tenant}/tenant.toml, then run this again",
        file=sys.stderr,
    )
    if any(c.name.startswith("folder ") and "does not exist" in c.detail for c in report.missing):
        print(
            f"missing folders: `engine doctor {args.tenant} --create-folders` creates them",
            file=sys.stderr,
        )
    return 1


def _cmd_onboard(args: argparse.Namespace) -> int:
    """``engine onboard <slug>``: the onboarding conversation as JSON, for any
    agent to drive (docs/tenant-kit-design.md, section 6). Prints the next
    question; ``--answer ID=VALUE`` records answers into the answers file;
    ``--apply`` renders the tenant from them and runs doctor. Exit 2 on an
    answer it cannot take or an apply it cannot do yet."""
    import json

    from ..onboarding import OnboardingError, apply, next_question, plan, record
    from .init import InitError

    store = Path(args.answers or f"onboarding-{args.slug}.json")
    answers = json.loads(store.read_text(encoding="utf-8")) if store.exists() else {}
    try:
        for item in args.answer or []:
            qid, sep, value = item.partition("=")
            if not sep:
                raise OnboardingError(f"--answer {item!r} is not ID=VALUE")
            parsed: object = json.loads(value) if value.strip().startswith("[") else value
            answers = record(answers, qid.strip(), parsed)
        store.write_text(json.dumps(answers, indent=2) + "\n", encoding="utf-8")
        if args.apply:
            result = apply(
                plan(answers, args.slug),
                root=args.root,
                data_root=args.data_root,
                ledger_dir=args.ledger_dir,
                run_audit=not args.no_audit,
            )
            report = result.doctor
            out = {
                "created": str(result.tenant_dir),
                "files": [p.relative_to(result.tenant_dir).as_posix() for p in result.init.files],
                "doctor": [f"{c.name}: {c.status}: {c.detail}" for c in report.checks],
                "missing": len(report.missing),
            }
            print(json.dumps(out, indent=2))
            return 0
    except (OnboardingError, InitError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {"slug": args.slug, "next": next_question(answers), "answers": answers}, indent=2
        )
    )
    return 0


def _cmd_voice_check(args: argparse.Namespace) -> int:
    """``engine voice-check <tenant> <file> [--register NAME]``: the tenant's
    kit/voice.toml against a file (docs/tenant-kit-design.md, section 2).
    Exit 0 clean, 1 warnings, 2 fatal or a refusal. No model call."""
    from ..voice import VoiceError, check_text, exit_code
    from .config import tenant_dir
    from .kit import KitError, load_part

    root = Path(args.root) if args.root else None
    directory = tenant_dir(args.tenant, tenants_root=root)
    try:
        voice = load_part(directory, "voice")
    except KitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if voice is None:
        print(
            f"error: {directory / 'kit' / 'voice.toml'} does not exist; "
            "`engine init` renders one for a new tenant",
            file=sys.stderr,
        )
        return 2
    path = Path(args.file)
    try:
        text = path.read_text(encoding="utf-8")
        hits = check_text(text, voice, register=args.register)
    except (OSError, VoiceError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(
        f"voice-check {path.name}  register={args.register or 'none'}  "
        f"spelling={voice['spelling']}  preset={voice['preset']}"
    )
    for hit in hits:
        label = f"{hit.severity.upper():<6} {hit.check:<15} line {hit.line}"
        print(f"{label}: {hit.message}  | {hit.excerpt}")
    code = exit_code(hits)
    fatal = sum(h.severity == "fatal" for h in hits)
    result = ("CLEAN", "WARN", "FATAL")[code]
    print(f"RESULT: {result} ({fatal} fatal, {len(hits) - fatal} warn)")
    return code


def _cmd_schedule(args: argparse.Namespace) -> int:
    """``engine schedule <tenant>``: render this host's crontab from
    ``[host.schedule]`` (row 7.21). Prints it, or writes it with ``--out``.
    The container entrypoint calls this at every boot, which is why the file
    it produces says GENERATED at the top."""
    from .config import load_tenant
    from .schedule import render_crontab

    try:
        cfg = load_tenant(args.tenant, tenants_root=Path(args.root) if args.root else None)
        text = render_crontab(
            cfg,
            tenant=args.tenant,
            repo=args.repo,
            log_dir=args.log_dir,
            every_minute=args.every_minute,
        )
    except (TenantNotFoundError, ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        sys.stdout.write(text)
    return 0


def _cmd_evals(args: argparse.Namespace) -> int:
    """``engine evals``: the model eval sets and the evidence the gate reads
    (row 7.13, ``docs/model-seam-design.md``). ``list`` shows the sets;
    ``run`` scores one job against one model and writes
    ``core/llm/eval_sets/<job>/results/<model_id>.json``. Exit 1 when a case
    fails (the file is still written: a red result is a result), 2 on a
    usage or configuration error."""
    from ..llm import evals as ev
    from .config import load_tenant

    root = ev.eval_sets_root()
    if args.evals_command == "list":
        if not root.is_dir():
            print(f"no eval sets under {root}")
            return 0
        gated = set(ev.gated_jobs())
        for directory in sorted(p for p in root.iterdir() if p.is_dir()):
            job = directory.name
            if job not in gated:
                print(f"{job}: no cases, NOT gated")
                continue
            spec = ev.load_job(job)
            scored = sorted(p.stem for p in (directory / ev.RESULTS_DIRNAME).glob("*.json"))
            print(
                f"{job}: {len(spec.cases)} cases, gated; "
                f"results for {', '.join(scored) if scored else 'no model yet'}"
            )
        return 0

    resolved = None
    if args.tier:
        if not args.tenant:
            print("error: --tier names a tier in a tenant's table; add --tenant", file=sys.stderr)
            return 2
        try:
            # check_evals=False: the command that PRODUCES the evidence has to
            # read the tenant file that lacks it.
            tenant = load_tenant(
                args.tenant,
                tenants_root=Path(args.tenants_root) if args.tenants_root else None,
                check_evals=False,
            )
            resolved = ev.tier_model(tenant.llm, args.tier)
        except (TenantNotFoundError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        model, adapter_name = resolved.model, resolved.adapter
    elif args.model:
        # A model id names no adapter, no endpoint, and no key variable, so a
        # bare --model runs on the fixture adapter seeded from the cases: it
        # scores the harness and says so in the file. --tier is the honest
        # path for a real model, and the gate will not accept a fixture
        # results file for a tier that runs anything else.
        model, adapter_name = args.model, "fixture"
    else:
        print(
            "error: name the model to score: --tier <name> --tenant <slug> for a real "
            "one, or --model <id> for a seeded fixture run",
            file=sys.stderr,
        )
        return 2

    if adapter_name == "fixture":
        make_adapter = ev.seeded_fixture_adapter
    else:
        from ..llm.policy import build_adapter

        wire = build_adapter(resolved)

        def make_adapter(case, _wire=wire):  # one adapter serves every case
            return _wire

    try:
        report = ev.run_eval_set(
            args.job,
            model=model,
            adapter_name=adapter_name,
            tier=args.tier or "",
            make_adapter=make_adapter,
            timeout_s=args.timeout,
            engine_commit=ev.current_commit(),
        )
    except ev.EvalSetError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    for case in sorted(report.results, key=lambda r: r.case):
        mark = "ok  " if case.passed else "FAIL"
        print(f"  {mark} {case.case}")
        if case.error:
            print(f"       {case.error}")
        for check in sorted(case.checks, key=lambda c: c.field):
            if not check.passed:
                print(f"       {check.field}: expected {check.expected!r}, got {check.actual!r}")
    print(report.summary)
    print(f"written: {ev.write_results(report)}")
    return 0 if report.failed == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="engine",
        description="Back-office agent engine: idempotent jobs against a git-backed ledger.",
    )
    parser.add_argument(
        "--version", action=VersionAction, help="print the version and commit, then exit"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="run a job: engine run <tenant> <agent> <job>")
    run_p.add_argument("tenant", help="tenant slug (a directory under tenants/)")
    run_p.add_argument("agent", help="agent name (a directory under core/agents/)")
    run_p.add_argument("job", help="job name exposed by that agent")
    run_p.add_argument(
        "--shadow",
        action="store_true",
        help="shadow mode: write to the ledger but act on nothing external",
    )
    run_p.add_argument(
        "--ledger-dir",
        default=None,
        help="ledger base directory (default: $ENGINE_LEDGER_ROOT or ./.ledger)",
    )
    run_p.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="K=V",
        help="pass a job parameter (repeatable)",
    )
    run_p.add_argument("--json", action="store_true", help="emit the RunResult as JSON")

    sub.add_parser("agents", help="list discoverable agents and their jobs")

    tracker_p = sub.add_parser(
        "tracker", help="a token for one person's phone tracker app (miles and times only)"
    )
    tracker_p.add_argument("tenant")
    tracker_p.add_argument("person", help="their id under [people] in authority.toml")
    tracker_p.add_argument("--domain", default="", help="the box's domain, for the server URL")

    receipt_p = sub.add_parser(
        "receipt-link", help="a person's standing receipt link for their home screen"
    )
    receipt_p.add_argument("tenant")
    receipt_p.add_argument("person", help="their id under [people] in authority.toml")
    receipt_p.add_argument("--domain", default="", help="the box's domain, for the link")
    receipt_p.add_argument("--revoke", action="store_true", help="retire their link instead")

    people_p = sub.add_parser("people", help="the ministry's people")
    people_sub = people_p.add_subparsers(dest="people_command", required=True)
    people_add = people_sub.add_parser(
        "add", help="new people from one list: 'Name = role, address, looked after by Name'"
    )
    people_add.add_argument("tenant")
    people_add.add_argument("list", help="a file with one person per line, or - for stdin")
    people_add.add_argument("--check", action="store_true", help="say who would come on; no writes")
    calendar_p = sub.add_parser(
        "calendar-link", help="retire a person's calendar feed link (a new one comes from setup)"
    )
    calendar_p.add_argument("tenant")
    calendar_p.add_argument("person", help="their id under [people] in authority.toml")
    calendar_p.add_argument("--revoke", action="store_true", required=True)

    notices_p = sub.add_parser(
        "notices", help="the emails the box should send, as JSON lines for the doorkeeper"
    )
    notices_p.add_argument("tenant")
    notices_p.add_argument("--days", type=int, default=7)
    notices_p.add_argument(
        "--packs", default=None, help="write each board pack a notice names here, as a PDF"
    )
    notices_p.add_argument("--ledger-dir", default=None)

    queue_p = sub.add_parser("queue", help="review the approval queue")
    queue_sub = queue_p.add_subparsers(dest="queue_command", required=True)
    q_list = queue_sub.add_parser("list", help="list approval items")
    q_list.add_argument("tenant")
    q_list.add_argument("--status", default=None, help="filter: pending|approved|rejected")
    q_list.add_argument("--ledger-dir", default=None)
    route_help = {
        "waiting": "what is waiting on a person, with their latest reminder (authority.toml only)",
        "delegate": "hand a role you hold to someone until a date (a person, at a terminal)",
        "revoke": "end a delegation you gave before its date",
        "handoff": "hand one card on its route to someone who can decide it",
        "on-it": "say you will decide a card by a date; its clock waits until then",
    }
    for verb, text in route_help.items():
        q_route = queue_sub.add_parser(verb, help=text)
        q_route.add_argument("tenant")
        q_route.add_argument("--as", dest="as_principal", required=True, metavar="NAME")
        q_route.add_argument("--ledger-dir", default=None)
        if verb in ("handoff", "on-it"):
            q_route.add_argument("--id", type=int, required=True)
        if verb in ("delegate", "handoff"):
            q_route.add_argument("--to", required=True, metavar="NAME")
        if verb == "delegate":
            q_route.add_argument("--role", required=True)
            q_route.add_argument("--until", required=True, metavar="YYYY-MM-DD")
        if verb == "revoke":
            q_route.add_argument("--delegation", required=True, metavar="ID")
        if verb == "on-it":
            q_route.add_argument("--by", required=True, metavar="YYYY-MM-DD")
    for verb in ("approve", "reject"):
        q_verb = queue_sub.add_parser(verb, help=f"{verb} a pending item")
        q_verb.add_argument("tenant")
        q_verb.add_argument("--id", type=int, required=True)
        q_verb.add_argument("--ledger-dir", default=None)
        q_verb.add_argument(
            "--as",
            dest="as_principal",
            default=None,
            metavar="NAME",
            help="who is deciding, a person or agent in the tenant's authority.toml "
            "(required when the tenant has one; a person decides only at a terminal)",
        )
        if verb == "approve":
            q_verb.add_argument(
                "--param",
                action="append",
                default=[],
                metavar="K=V",
                help="correct a card fact at approval time (e.g. channel=Check "
                "instrument_ref=3050); merges into the stored params",
            )

    diff_p = sub.add_parser(
        "shadow-diff",
        help="parity report: engine shadow rows vs the legacy ledger (read-only)",
    )
    diff_p.add_argument("tenant")
    diff_p.add_argument("agent", help="currently: ap")
    diff_p.add_argument("--legacy-xlsx", default=None, help="override [ap].legacy_ledger_xlsx")
    diff_p.add_argument("--since", default=None, help="window start (ISO date)")
    diff_p.add_argument("--ledger-dir", default=None)
    diff_p.add_argument("--no-save", action="store_true", help="print only, save nothing")

    sweep_p = sub.add_parser(
        "sweep",
        help="filesystem sweep: invoice-like files the engine has not booked (read-only)",
    )
    sweep_p.add_argument("tenant")
    sweep_p.add_argument("agent", help="currently: ap")
    sweep_p.add_argument("--landing-dir", default=None, help="override [ap].landing_dir")
    sweep_p.add_argument("--ledger-dir", default=None)
    sweep_p.add_argument("--no-save", action="store_true", help="print only, save nothing")

    status_p = sub.add_parser(
        "status",
        help="owner write-back: set an invoice's status (scheduled / paid)",
    )
    status_p.add_argument("tenant")
    status_p.add_argument("ref", help="invoice number")
    status_p.add_argument(
        "--vendor", default=None, help="disambiguate when a number repeats across vendors"
    )
    target = status_p.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--scheduled", action="store_true", help="mark Scheduled (payment committed)"
    )
    target.add_argument("--paid", action="store_true", help="mark Paid (cleared the bank)")
    target.add_argument(
        "--set",
        default=None,
        metavar="STATUS",
        help="set an explicit status, e.g. 'Void - Duplicate'",
    )
    status_p.add_argument(
        "--check",
        default="",
        metavar="REF",
        help="the check number (or instrument ref) this payment was made with; "
        "recorded on the row so the engine can record the payment in the "
        "accounting system (W2) and match it when it clears; a bill paid with "
        "several checks names each with its amount, '9170 $10,000.00 + 9171 "
        "$3,253.00' (a send date may follow each: 'on 11/24')",
    )
    status_p.add_argument(
        "--date",
        default="",
        metavar="YYYY-MM-DD",
        help="the payment date to record with --check (defaults to the day of the flip)",
    )
    status_p.add_argument("--ledger-dir", default=None)

    page_p = sub.add_parser(
        "status-page",
        help="render the read-only status page (last run per job, pending cards, "
        "the newest audit report's NEW section)",
    )
    page_p.add_argument("tenant")
    page_p.add_argument(
        "--out",
        default=None,
        metavar="PATH",
        help="where to write the page (default: <[close].report_dir>/_status/status.html)",
    )
    page_p.add_argument("--stdout", action="store_true", help="print the HTML instead of writing")
    page_p.add_argument(
        "--report-dir", default=None, help="override the tenant's [auditor].report_dir"
    )
    page_p.add_argument("--ledger-dir", default=None)

    close_p = sub.add_parser(
        "close-preflight",
        help="run the month-end close checklist; exits 0 OK / 1 WARN / 2 BLOCK",
    )
    close_p.add_argument("tenant")
    close_p.add_argument("--month", default=None, help="YYYY-MM (default: previous month)")
    close_p.add_argument(
        "--statement-balance",
        default=None,
        help="the bank statement's ending balance, read off QBO's statement tab",
    )
    close_p.add_argument(
        "--packet",
        action="store_true",
        help="also render the close packet workbook (skipped when the preflight blocks)",
    )
    close_p.add_argument("--ledger-dir", default=None)

    dismiss_p = sub.add_parser(
        "dismiss", help="mark an unprocessed file as not an invoice (will not resurface)"
    )
    dismiss_p.add_argument("tenant")
    dismiss_p.add_argument(
        "file", nargs="?", default=None, help="the unprocessed file name (omit with --all)"
    )
    dismiss_p.add_argument(
        "--all", action="store_true", help="dismiss every unprocessed file at once"
    )
    dismiss_p.add_argument("--ledger-dir", default=None)

    identify_p = sub.add_parser(
        "identify", help="record an unprocessed file as an invoice (manual entry)"
    )
    identify_p.add_argument("tenant")
    identify_p.add_argument("file", help="the unprocessed file name")
    identify_p.add_argument("--vendor", required=True)
    identify_p.add_argument("--number", required=True, help="invoice number")
    identify_p.add_argument("--amount", required=True, help="invoice amount, e.g. 272.00")
    identify_p.add_argument("--ledger-dir", default=None)

    jobs_p = sub.add_parser("jobs", help="the job ledger: retries")
    jobs_sub = jobs_p.add_subparsers(dest="jobs_command", required=True)
    resume_p = jobs_sub.add_parser(
        "resume",
        help="execute the retries that are due (a scheduler runs this every 15 minutes); "
        "prints one line and exits 0 when nothing is due",
    )
    resume_p.add_argument("tenant")
    resume_p.add_argument(
        "--now",
        action="store_true",
        help="owner tool: run every scheduled retry now, due or not",
    )
    resume_p.add_argument("--ledger-dir", default=None)
    resume_p.add_argument("--json", action="store_true", help="emit the RunResults as JSON")

    runner_p = sub.add_parser(
        "runner", help="agentic sessions: engine runner run <tenant> <skill path>"
    )
    runner_sub = runner_p.add_subparsers(dest="runner_command", required=True)
    runner_run = runner_sub.add_parser(
        "run",
        help="run one skill through the runner (core/llm/runner.py) and print the result",
        description=runner_cli.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    runner_run.add_argument("tenant")
    runner_run.add_argument("skill", help="path to a SKILL.md in the Agent Skills format")
    runner_run.add_argument(
        "--adapter",
        choices=runner_cli.ADAPTER_CHOICES,
        default=None,
        help="override the runner the tenant policy chose (default: the tier's adapter decides)",
    )
    runner_run.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="K=V",
        help="a lane parameter; see the description for every key",
    )
    runner_run.add_argument("--json", action="store_true", help="emit the RunnerResult as JSON")

    mail_p = sub.add_parser("mail", help="owner acts on the tenant mailbox connection")
    mail_sub = mail_p.add_subparsers(dest="mail_command", required=True)
    consent_p = mail_sub.add_parser(
        "consent",
        help="interactive device-code consent: prints a URL + code, waits for the sign-in, "
        "stores the MSAL cache under tenant.toml [mail] keychain names, verifies silently",
    )
    consent_p.add_argument("tenant")
    consent_p.add_argument(
        "--scope",
        action="append",
        default=[],
        help="an extra delegated scope to consent in the same sign-in (repeatable), "
        "e.g. Calendars.ReadWrite for deadlines/calendar; [mail].scopes is unchanged",
    )
    talk_p = sub.add_parser(
        "talk",
        help="talk to Tim in the terminal as one person: his own voice over the read tools",
    )
    talk_p.add_argument("tenant")
    talk_p.add_argument(
        "--as", dest="as_person", required=True, help="the person (an id in authority.toml)"
    )
    talk_p.add_argument("--say", default=None, help="say one thing and print Tim's reply")
    talk_p.add_argument("--ledger-dir", default=None)
    spend_p = sub.add_parser(
        "spend",
        help="what the ministry's models cost this month, by job type, with the cap "
        "and Tim's soft line",
    )
    spend_p.add_argument("tenant")
    spend_p.add_argument("--json", action="store_true", help="the report as JSON")
    spend_p.add_argument("--ledger-dir", default=None)

    mcp_p = sub.add_parser(
        "mcp",
        help="serve the read-only tools over MCP on stdio (for a chat client); "
        "--onboarding serves the onboarding conversation, no tenant needed",
    )
    mcp_p.add_argument("tenant", nargs="?", default=None)
    mcp_p.add_argument("--ledger-dir", default=None)
    mcp_p.add_argument("--obligations-file", default=None)
    mcp_p.add_argument(
        "--http",
        action="store_true",
        help="serve on one HTTP endpoint instead of stdio; each request answers as the "
        "person its bearer token signs in",
    )
    mcp_p.add_argument("--resource", default=None, help="with --http: the endpoint's public URL")
    mcp_p.add_argument(
        "--tokens", default=None, help="with --http: the invitation-token file (hashes only)"
    )
    mcp_p.add_argument(
        "--introspect",
        default=None,
        metavar="URL",
        help="with --http: the sign-in service's introspection endpoint (RFC 7662), "
        "which says who each token is",
    )
    mcp_p.add_argument(
        "--introspect-secret",
        default=None,
        metavar="FILE",
        help="with --introspect: the file holding the secret shared with the sign-in service",
    )
    mcp_p.add_argument(
        "--authorization-server",
        action="append",
        default=[],
        help="with --http: a sign-in service named in the resource metadata (repeatable)",
    )
    mcp_p.add_argument(
        "--today",
        default=None,
        metavar="YYYY-MM-DD",
        help="stdio only: the day due dates count from, for a replay of a past day's books",
    )
    mcp_p.add_argument("--bind", default="127.0.0.1", help="with --http: the address to bind")
    mcp_p.add_argument("--port", type=int, default=8765, help="with --http: the port")
    mcp_p.add_argument(
        "--as",
        dest="as_person",
        default=None,
        metavar="PERSON",
        help="answer as this person (their id in authority.toml): every tool shows only "
        "what they may view",
    )
    mcp_p.add_argument(
        "--onboarding",
        action="store_true",
        help="serve the onboarding conversation for a tenant that does not exist yet",
    )
    mcp_p.add_argument(
        "--answers-dir",
        default=None,
        help="with --onboarding: where onboarding-<slug>.json lives (default: here)",
    )
    mcp_p.add_argument(
        "--root", default=None, help="with --onboarding: tenants directory (default: ./tenants)"
    )
    mcp_p.add_argument(
        "--data-root", default=None, help="with --onboarding: where the tenant's folders go"
    )
    mcp_p.add_argument(
        "--pack",
        default=None,
        help="with --onboarding: set every ministry up from this agency pack (core/pack)",
    )
    mcp_p.add_argument(
        "--no-audit",
        action="store_true",
        help="with --onboarding: skip the first auditor run on apply",
    )

    token_p = sub.add_parser(
        "door-token",
        help="make an invitation token for one person (prints it once; the file keeps its hash)",
    )
    token_p.add_argument("tenant")
    token_p.add_argument("person", help="their id in authority.toml")
    token_p.add_argument("--tokens", required=True, help="the invitation-token file")

    caps_p = sub.add_parser(
        "capabilities", help="what Tim can do today, built from the code (markdown or json)"
    )
    caps_p.add_argument("--format", choices=("md", "json"), default="md")
    caps_p.add_argument(
        "--shape",
        default=None,
        help="only the setup questions this shape is asked (e.g. nonprofit-solo)",
    )
    caps_p.add_argument(
        "--tenant",
        default=None,
        help="what this ministry's door offers (no handbook rules = no rules tools)",
    )

    faq_p = sub.add_parser("faq", help="the questions people ask about Tim, with Tim's answers")
    faq_p.add_argument("--format", choices=("md", "json"), default="md")
    faq_p.add_argument("--tenant", default=None, help="only the answers for what its door offers")
    faq_p.add_argument("--shape", default=None, help="the tenant's shape (with --tenant)")

    room_p = sub.add_parser("room", help="a house's room, packed to move and restored (card H3)")
    room_sub = room_p.add_subparsers(dest="room_cmd", required=True)
    rp = room_sub.add_parser("pack", help="fold a room's folders into one file with a manifest")
    rp.add_argument("folder", type=Path, help="the room's folder (holds church/ and box/)")
    rp.add_argument("--out", type=Path, required=True)
    rr = room_sub.add_parser("restore", help="unpack a room only where every count matches")
    rr.add_argument("packed", type=Path)
    rr.add_argument(
        "--into", type=Path, required=True, help="the new room's folder; must not exist"
    )

    iv_p = sub.add_parser(
        "interview", help="a missionary's own setup questions: make her link, see who answered"
    )
    iv_sub = iv_p.add_subparsers(dest="interview_cmd", required=True)
    iv_new = iv_sub.add_parser("new", help="a one-time link for one missionary")
    iv_new.add_argument("--name", required=True, help="her name, as she goes by it")
    iv_new.add_argument("--email", required=True, help="her own address")
    iv_new.add_argument("--by", required=True, help="your name: she reads who sent it")
    iv_new.add_argument("--base", required=True, help="this box's address, https://...")
    iv_new.add_argument("--days", type=int, default=14, help="how long the link works")
    iv_new.add_argument("--ledger-dir", default=None)
    iv_list = iv_sub.add_parser("list", help="every link made here, and where each stands")
    iv_list.add_argument("--ledger-dir", default=None)

    bp_p = sub.add_parser(
        "board-pack", help="a ministry's statements for a span, as a page and a PDF"
    )
    bp_p.add_argument("tenant")
    bp_p.add_argument("--as", dest="as_person", required=True, help="whose eyes (their role)")
    bp_p.add_argument("--period", default="", help="2026, 2026-Q3, October 2026; YTD if left out")
    bp_p.add_argument("--out", required=True, help="the folder the page and the PDF go in")
    bp_p.add_argument("--ledger-dir", default=None)

    dl_p = sub.add_parser(
        "donor-letters", help="a year's donor letters and giving statements, as pages and PDFs"
    )
    dl_p.add_argument("tenant")
    dl_p.add_argument("--as", dest="as_person", required=True, help="whose eyes (their role)")
    dl_p.add_argument("--year", default="", help="2026; this year if left out")
    dl_p.add_argument("--out", required=True, help="the folder the letters go in")
    dl_p.add_argument("--ledger-dir", default=None)

    tp_p = sub.add_parser(
        "tax-packet", help="a missionary's tax packet for a year, as a page and a PDF"
    )
    tp_p.add_argument("tenant")
    tp_p.add_argument("--as", dest="as_person", required=True, help="whose eyes (their role)")
    tp_p.add_argument("--person", default="", help="the missionary, for whoever looks over")
    tp_p.add_argument("--year", default="", help="2026; this year if left out")
    tp_p.add_argument("--out", required=True, help="the folder the page and the PDF go in")
    tp_p.add_argument("--ledger-dir", default=None)

    cpa_p = sub.add_parser(
        "cpa-export", help="a year's books for an accountant: Beancount text, trial balance"
    )
    cpa_p.add_argument("tenant")
    cpa_p.add_argument("--as", dest="as_person", required=True, help="whose eyes (their role)")
    cpa_p.add_argument("--year", required=True)
    cpa_p.add_argument("--pay-kind", default="", help="the ministry's kind for pay")
    cpa_p.add_argument("--out", required=True, help="the folder the files go in")
    cpa_p.add_argument("--ledger-dir", default=None)

    pack_p = sub.add_parser("pack", help="agency packs: check one before it sets up a ministry")
    pack_p.add_argument("action", choices=["check"])
    pack_p.add_argument("folder", help="the pack's folder (pack.toml, authority.toml, rules/)")

    rules_p = sub.add_parser(
        "rules", help="the ministry's policy rules, layer by layer, each with its source"
    )
    rules_p.add_argument("tenant")
    rules_p.add_argument("--root", default=None, help="tenants directory (default: ./tenants)")

    doctor_p = sub.add_parser(
        "doctor",
        help="report what this host is missing for a tenant: secrets, folders, "
        "the ledger and its remote, the scheduler and its commands",
    )
    doctor_p.add_argument("tenant")
    doctor_p.add_argument("--root", default=None, help="tenants directory (default: ./tenants)")
    doctor_p.add_argument(
        "--create-folders",
        action="store_true",
        help="create every folder the tenant names that does not exist yet, then report",
    )

    onboard_p = sub.add_parser(
        "onboard",
        help="the onboarding conversation as JSON for an agent: the next question, "
        "--answer ID=VALUE, then --apply to create the tenant (docs/tenant-kit-design.md)",
    )
    onboard_p.add_argument("slug", help="tenant slug: lowercase letters, digits, hyphens")
    onboard_p.add_argument(
        "--answers", default=None, help="the answers file (default: onboarding-<slug>.json)"
    )
    onboard_p.add_argument(
        "--answer", action="append", help="record an answer, ID=VALUE (repeatable)"
    )
    onboard_p.add_argument(
        "--apply", action="store_true", help="create the tenant from the answers"
    )
    onboard_p.add_argument("--root", default=None, help="tenants directory (default: ./tenants)")
    onboard_p.add_argument("--data-root", default=None, help="where the tenant's folders go")
    onboard_p.add_argument("--ledger-dir", default=None, help="ledger base directory")
    onboard_p.add_argument(
        "--no-audit", action="store_true", help="skip the first auditor run --local-only"
    )

    voice_p = sub.add_parser(
        "voice-check",
        help="check a file against the tenant's kit/voice.toml: spelling, banned words "
        "and shapes, glossary, register (exit 0 clean, 1 warn, 2 fatal)",
    )
    voice_p.add_argument("tenant")
    voice_p.add_argument("file", help="the text to check")
    voice_p.add_argument(
        "--register", default=None, help="a [registers.<name>] in voice.toml, e.g. formal"
    )
    voice_p.add_argument("--root", default=None, help="tenants directory (default: ./tenants)")

    schedule_p = sub.add_parser(
        "schedule",
        help="render this host's crontab from [host.schedule] (the container's scheduler)",
    )
    schedule_p.add_argument("tenant")
    schedule_p.add_argument("--root", default=None, help="tenants directory (default: ./tenants)")
    schedule_p.add_argument(
        "--repo", default=".", help="where the code lives on this host (default: .)"
    )
    schedule_p.add_argument(
        "--log-dir", default="logs", help="where each job appends its output (default: logs)"
    )
    schedule_p.add_argument(
        "--out", default=None, help="write the crontab here instead of printing it"
    )
    schedule_p.add_argument(
        "--every-minute",
        action="store_true",
        help="rewrite every enabled entry to * * * * * (one cycle now, for a smoke test); "
        "the commands are unchanged",
    )
    init_p = sub.add_parser(
        "init",
        help="create a tenant from an archetype template: engine init <slug> "
        "[--archetype A|B|C] [--shape SHAPE]",
    )
    init_p.add_argument("slug", help="tenant slug: lowercase letters, digits, hyphens")
    init_p.add_argument(
        "--archetype",
        default="A",
        choices=("A", "B", "C"),
        help="A project-coded technical services (default), B construction subs and "
        "trades, C agencies (docs/archetypes.md)",
    )
    init_p.add_argument(
        "--shape",
        default="commercial-small",
        choices=SHAPES,
        help="commercial or nonprofit; solo, small or organization (default commercial-small; "
        "docs/tenant-kit-design.md)",
    )
    init_p.add_argument("--root", default=None, help="tenants directory (default: ./tenants)")
    init_p.add_argument(
        "--data-root",
        default=None,
        help="where the tenant's folders go (default: <slug>-data beside the tenants "
        "directory); tenant.toml names it relative to the current directory",
    )
    init_p.add_argument("--legal-name", default="", help="the business's legal name")
    init_p.add_argument("--timezone", default="America/New_York", help="IANA zone name")
    init_p.add_argument(
        "--fiscal-year-start", type=int, default=1, help="first month of the fiscal year (1-12)"
    )
    init_p.add_argument("--ledger-dir", default=None, help="ledger base directory")
    init_p.add_argument("--store-dir", default=None, help="auditor store base directory")
    init_p.add_argument(
        "--no-audit", action="store_true", help="skip the first auditor run --local-only"
    )

    evals_p = sub.add_parser(
        "evals",
        help="model eval sets: score a model on a job and write the results file the "
        "tenant config gate reads (row 7.13)",
    )
    evals_sub = evals_p.add_subparsers(dest="evals_command", required=True)
    evals_sub.add_parser("list", help="the eval sets, their case counts, and who has been scored")
    evals_run = evals_sub.add_parser(
        "run", help="run one job's cases against one model and write results/<model_id>.json"
    )
    evals_run.add_argument("job", help="the model job type, e.g. invoice_extract")
    evals_run.add_argument(
        "--tier", default="", help="a tier in the tenant's [llm.tiers] (needs --tenant)"
    )
    evals_run.add_argument("--tenant", default="", help="tenant slug whose tiers name the model")
    evals_run.add_argument(
        "--tenants-root", default="", help="tenants directory (default: ./tenants)"
    )
    evals_run.add_argument(
        "--model",
        default="",
        help="score a model id on the fixture adapter, seeded from the cases (a harness "
        "self-test; use --tier to reach a real model)",
    )
    evals_run.add_argument(
        "--timeout", type=int, default=180, help="seconds per case (default 180)"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "agents":
        return _cmd_agents()
    nested = {
        ("jobs", "resume"): _cmd_jobs_resume,
        ("runner", "run"): _cmd_runner_run,
        ("mail", "consent"): _cmd_mail_consent,
    }
    sub = getattr(args, f"{args.command}_command", None)
    handler = nested.get((args.command, sub)) or _COMMANDS.get(args.command)
    if handler is None:
        parser.error(f"unknown command {args.command!r}")
        return 2  # unreachable; parser.error exits
    code = handler(args)
    _refresh_view(args)
    return code


_NO_REFRESH = {
    "mcp",
    "init",
    "doctor",
    "schedule",
    "evals",
    "status-page",
    "voice-check",
    "onboard",
}


def _refresh_view(args: argparse.Namespace) -> None:
    """End of the invocation: an AP write left the delivered workbook stale,
    so render it once (#326). Never in shadow, never changes the exit code."""
    tenant = getattr(args, "tenant", None)
    if not tenant or getattr(args, "shadow", False) or args.command in _NO_REFRESH:
        return
    from ..agents.ap.view_refresh import drain

    try:
        result = drain(tenant, ledger_dir=getattr(args, "ledger_dir", None))
    except (TenantNotFoundError, ValueError) as exc:
        print(f"view refresh skipped: {exc}", file=sys.stderr)
        return
    if result.anomaly:
        print(result.anomaly, file=sys.stderr)
    elif result.rendered:
        print("view refreshed: the AP workbook matches the ledger again", file=sys.stderr)


_COMMANDS = {
    "run": _cmd_run,
    "queue": _cmd_queue,
    "notices": _cmd_notices,
    "people": _cmd_people,
    "tracker": _cmd_tracker,
    "receipt-link": _cmd_receipt_link,
    "calendar-link": _cmd_calendar_link,
    "shadow-diff": _cmd_shadow_diff,
    "sweep": _cmd_sweep,
    "status": _cmd_status,
    "status-page": _cmd_status_page,
    "close-preflight": _cmd_close_preflight,
    "dismiss": _cmd_dismiss,
    "identify": _cmd_identify,
    "mcp": _cmd_mcp,
    "talk": _cmd_talk,
    "spend": _cmd_spend,
    "door-token": _cmd_door_token,
    "capabilities": _cmd_capabilities,
    "faq": _cmd_faq,
    "room": _cmd_room,
    "interview": _cmd_interview,
    "board-pack": _cmd_board_pack,
    "donor-letters": _cmd_donor_letters,
    "tax-packet": _cmd_tax_packet,
    "cpa-export": _cmd_cpa_export,
    "rules": _cmd_rules,
    "pack": _cmd_pack,
    "doctor": _cmd_doctor,
    "voice-check": _cmd_voice_check,
    "onboard": _cmd_onboard,
    "schedule": _cmd_schedule,
    "init": _cmd_init,
    "evals": _cmd_evals,
}


if __name__ == "__main__":
    raise SystemExit(main())
