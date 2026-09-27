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

from ..llm import runner_cli
from .config import TenantNotFoundError
from .registry import (
    UnknownAgentError,
    UnknownJobError,
    list_agents,
    load_agent_jobs,
    load_approval_checks,
)
from .runner import (
    LOCK_ANOMALY,
    RETRY_ATTEMPT,
    LedgerLocked,
    ledger_write_lock,
    resume_due,
    run,
)


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
    except (TenantNotFoundError, UnknownAgentError, UnknownJobError, ValueError) as exc:
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


def _cmd_queue(args: argparse.Namespace) -> int:
    from ..ledger import Ledger
    from .runner import resolve_ledger_root

    root = resolve_ledger_root(args.tenant, args.ledger_dir)
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
    try:
        with ledger_write_lock(root), Ledger.open(root) as ledger:

            def _check(agent: str, action_type: str, params: dict) -> str | None:
                fn = load_approval_checks(agent).get(action_type)
                return fn(ledger, args.tenant, params) if fn else None

            try:
                overrides = _parse_params(getattr(args, "param", []) or [])
                result = ledger.resolve_approval(
                    args.tenant,
                    args.id,
                    decision,
                    param_overrides=overrides,
                    check=_check if decision == "approved" else None,
                )
            except (LookupError, ValueError) as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
            noted = f" ({', '.join(f'{k}={v}' for k, v in overrides.items())})" if overrides else ""
            ledger.commit(
                agent="queue",
                job=decision,
                idempotency_key=f"approval-{args.id}",
                summary=f"approval #{args.id} {decision}: {result['action_type']}{noted}",
            )
    except LedgerLocked as exc:
        return _refuse_locked(exc)
    print(f"approval #{args.id} {decision} ({result['action_type']}){noted}")
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
    except (TenantNotFoundError, UnknownAgentError, UnknownJobError, ValueError) as exc:
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
    except (TenantNotFoundError, UnknownAgentError, UnknownJobError, ValueError) as exc:
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
    except (TenantNotFoundError, UnknownAgentError, UnknownJobError, ValueError) as exc:
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
    from datetime import UTC, datetime, timedelta

    from ..adapters.graph_mail import (
        GraphAuthError,
        device_code_consent,
        keychain_token_provider,
    )
    from .config import load_tenant

    try:
        tenant = load_tenant(args.tenant)
    except (TenantNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    mail = tenant.mail
    missing = [
        name
        for name in ("client_id", "tenant_id", "keychain_service", "keychain_account")
        if not getattr(mail, name)
    ]
    if missing:
        print(
            f"error: [mail] config incomplete for tenant {args.tenant!r}: "
            f"set {', '.join(missing)} in tenant.toml",
            file=sys.stderr,
        )
        return 2
    names = {
        "client_id": mail.client_id,
        "tenant_id": mail.tenant_id,
        "scopes": list(mail.scopes),
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

    started = datetime.now(UTC)
    try:
        consent = device_code_consent(prompt=prompt, **names)
    except GraphAuthError as exc:
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
        keychain_token_provider(**names)()
    except GraphAuthError as exc:
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
    print(f"tenant {result.slug} (archetype {result.archetype}) created at {result.tenant_dir}")
    for path in result.files:
        print(f"  {path.name}")
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


def _cmd_doctor(args: argparse.Namespace) -> int:
    """``engine doctor <tenant>``: what this host is still missing (row
    7.21). Exit 0 when nothing is owed, 1 with one line per missing item, 2
    when the tenant itself cannot be read. Never prints a secret value."""
    from .doctor import run_doctor

    try:
        report = run_doctor(args.tenant, tenants_root=args.root)
    except (TenantNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
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
    return 1


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

    queue_p = sub.add_parser("queue", help="review the approval queue")
    queue_sub = queue_p.add_subparsers(dest="queue_command", required=True)
    q_list = queue_sub.add_parser("list", help="list approval items")
    q_list.add_argument("tenant")
    q_list.add_argument("--status", default=None, help="filter: pending|approved|rejected")
    q_list.add_argument("--ledger-dir", default=None)
    for verb in ("approve", "reject"):
        q_verb = queue_sub.add_parser(verb, help=f"{verb} a pending item")
        q_verb.add_argument("tenant")
        q_verb.add_argument("--id", type=int, required=True)
        q_verb.add_argument("--ledger-dir", default=None)
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
        "accounting system (W2) and match it when it clears",
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
    doctor_p = sub.add_parser(
        "doctor",
        help="report what this host is missing for a tenant: secrets, folders, "
        "the ledger and its remote, the scheduler and its commands",
    )
    doctor_p.add_argument("tenant")
    doctor_p.add_argument("--root", default=None, help="tenants directory (default: ./tenants)")

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
        help="create a tenant from an archetype template: engine init <slug> [--archetype A|B|C]",
    )
    init_p.add_argument("slug", help="tenant slug: lowercase letters, digits, hyphens")
    init_p.add_argument(
        "--archetype",
        default="A",
        choices=("A", "B", "C"),
        help="A project-coded technical services (default), B construction subs and "
        "trades, C agencies (docs/archetypes.md)",
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
    if args.command == "run":
        return _cmd_run(args)
    if args.command == "queue":
        return _cmd_queue(args)
    if args.command == "shadow-diff":
        return _cmd_shadow_diff(args)
    if args.command == "sweep":
        return _cmd_sweep(args)
    if args.command == "status":
        return _cmd_status(args)
    if args.command == "status-page":
        return _cmd_status_page(args)
    if args.command == "close-preflight":
        return _cmd_close_preflight(args)
    if args.command == "dismiss":
        return _cmd_dismiss(args)
    if args.command == "identify":
        return _cmd_identify(args)
    if args.command == "jobs" and args.jobs_command == "resume":
        return _cmd_jobs_resume(args)
    if args.command == "runner" and args.runner_command == "run":
        return _cmd_runner_run(args)
    if args.command == "mail" and args.mail_command == "consent":
        return _cmd_mail_consent(args)
    if args.command == "doctor":
        return _cmd_doctor(args)
    if args.command == "schedule":
        return _cmd_schedule(args)
    if args.command == "init":
        return _cmd_init(args)
    if args.command == "evals":
        return _cmd_evals(args)
    parser.error(f"unknown command {args.command!r}")
    return 2  # unreachable; parser.error exits


if __name__ == "__main__":
    raise SystemExit(main())
