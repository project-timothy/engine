"""Close agent jobs: ``preflight`` and ``packet``.

Both are LOOKS, not mutations: they read the ledger, the auditor's store
(as data), and the accounting system, then regenerate the month's
CLOSE_PREFLIGHT.md (and, over a block-free month, the close packet
workbook). Their idempotency keys carry a second-granularity stamp plus
the statement balance — every invocation re-looks, because the whole point
of repeat-until-green is that the world changed between runs. The runs
table becomes the record of the ceremony; the only files touched are the
report and packet, regenerated in place. The one true mutation of this
agent's life — sealing the period — is the ``lock`` job (build step 5),
approval-gated per invariant 7.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from ...authority import CardRule
from ...engine.contracts import EventSpec, JobContext, JobHandler, JobOutput
from ...engine.runkey import RunKey
from . import checks as close_checks
from .report import write_preflight
from .schema import MONTH_PATTERN, PreflightReport


def _qbo_read_client(ctx: JobContext):
    """Factory hook: evals monkeypatch this with a fake."""
    from ...adapters.qbo import QboClient

    token_file = ctx.params.get("qbo_token_file") or ctx.tenant.qbo.token_file
    if not token_file:
        raise ValueError("no QBO token file: set [qbo].token_file in tenant.toml")
    return QboClient(token_file)


def _closing_month(ctx: JobContext, *, now: datetime | None = None) -> str:
    """Explicit ``month=YYYY-MM`` param, else the previous calendar month in
    the tenant's timezone (run early August, close July)."""
    explicit = str(ctx.params.get("month", "") or "")
    if explicit:
        if not MONTH_PATTERN.match(explicit):
            raise ValueError(f"month must be YYYY-MM, got {explicit!r}")
        return explicit
    now = now or datetime.now(UTC)
    try:
        local = now.astimezone(ZoneInfo(ctx.tenant.identity.timezone))
    except Exception:
        local = now
    first_of_month = local.replace(day=1)
    previous = first_of_month - timedelta(days=1)
    return previous.strftime("%Y-%m")


def _preflight_key(ctx: JobContext) -> str:
    # Every look is a fresh look: second-granularity stamp PLUS the params,
    # so a quick retry after fixing something never replays a stale verdict
    # (live lesson 2026-07-21: two same-minute runs with different statement
    # balances replayed the first one's BLOCK).
    key = RunKey(ctx, "close-preflight")
    key.add("month", _closing_month(ctx))
    key.param("statement_balance")
    key.config("close")
    key.stamp()
    return key.digest()


def _statement_balance_cents(ctx: JobContext) -> int | None:
    """The one hand-carried number of the ceremony, as integer cents."""
    from decimal import Decimal, InvalidOperation

    raw = str(ctx.params.get("statement_balance", "") or "").replace("$", "").replace(",", "")
    if not raw:
        return None
    try:
        return int((Decimal(raw) * 100).to_integral_value())
    except InvalidOperation as exc:
        raise ValueError(
            f"statement_balance must be a dollar amount, got {ctx.params['statement_balance']!r}"
        ) from exc


def _build_close_ctx(ctx: JobContext, month: str) -> close_checks.CloseContext:
    return close_checks.CloseContext(
        tenant_slug=ctx.tenant_slug,
        ledger=ctx.ledger,
        month=month,
        qbo=_qbo_read_client(ctx),
        auditor_store=close_checks.default_auditor_store(ctx.tenant_slug),
        uncategorized_block_over_cents=int(round(ctx.tenant.close.uncategorized_block_over * 100)),
        bank_account=ctx.tenant.close.bank_account,
        statement_balance_cents=_statement_balance_cents(ctx),
        owner_names=list(ctx.tenant.close.owner_names),
        payroll_markers=list(ctx.tenant.close.payroll_markers),
        expenses_dir=ctx.tenant.close.expenses_dir,
        invoice_register_xlsx=ctx.tenant.close.invoice_register_xlsx,
    )


def _run_checks(close_ctx: close_checks.CloseContext, now_iso: str) -> list:
    results = []
    named_checks = [
        ("machinery", lambda: close_checks.check_machinery(close_ctx, now_iso=now_iso)),
        ("statement-anchor", lambda: close_checks.check_statement_anchor(close_ctx)),
        ("ap-tie-out", lambda: close_checks.check_ap_tie_out(close_ctx)),
        ("payroll", lambda: close_checks.check_payroll(close_ctx)),
        ("owner-transactions", lambda: close_checks.check_owner_transactions(close_ctx)),
        ("categorization", lambda: close_checks.check_categorization(close_ctx)),
        ("expenses", lambda: close_checks.check_expenses(close_ctx)),
        ("ar-snapshot", lambda: close_checks.check_ar_snapshot(close_ctx)),
    ]
    for name, run_check in named_checks:
        try:
            results.append(run_check())
        except Exception as exc:  # a crashed check is a BLOCK, never a dead preflight
            results.append(
                close_checks.CheckResult(
                    name=name,
                    status="BLOCK",
                    summary=f"the check crashed: {exc}",
                )
            )
    anchor = next(r for r in results if r.name == "statement-anchor")
    results.append(close_checks.check_feed_acceptance(close_ctx, anchor))
    return results


def _preflight_run(ctx: JobContext) -> JobOutput:
    month = _closing_month(ctx)
    now_iso = datetime.now(UTC).isoformat()
    close_ctx = _build_close_ctx(ctx, month)
    results = _run_checks(close_ctx, now_iso)

    report = PreflightReport(tenant=ctx.tenant_slug, month=month, ran_at=now_iso, checks=results)
    counts = report.counts()
    actions = []
    if ctx.shadow or not ctx.tenant.close.report_dir:
        actions.append("preflight rendered (not written: shadow or no [close].report_dir)")
    else:
        path = write_preflight(report, report_dir=ctx.tenant.close.report_dir, guard=ctx.guard)
        actions.append(f"wrote {path}")

    summary = (
        f"close preflight {month}: verdict {report.worst} "
        f"({counts['OK']} OK, {counts['WARN']} WARN, {counts['BLOCK']} BLOCK, "
        f"{counts['TODO']} todo)"
    )
    return JobOutput(
        status="ok",
        summary=summary,
        actions=actions,
        events=[
            EventSpec(
                key=f"preflight:{month}:{report.ran_at[:16]}",
                event_type="close.preflight",
                payload={
                    "month": month,
                    "verdict": report.worst,
                    **{k.lower(): v for k, v in counts.items()},
                },
            )
        ],
    )


def _packet_key(ctx: JobContext) -> str:
    key = RunKey(ctx, "close-packet")
    key.add("month", _closing_month(ctx))
    key.param("statement_balance")
    key.config("close")
    key.stamp()
    return key.digest()


def _packet_run(ctx: JobContext) -> JobOutput:
    """Render the close packet — only over a month with no BLOCK."""
    from .packet import render_packet

    month = _closing_month(ctx)
    now_iso = datetime.now(UTC).isoformat()
    close_ctx = _build_close_ctx(ctx, month)
    results = _run_checks(close_ctx, now_iso)
    report = PreflightReport(tenant=ctx.tenant_slug, month=month, ran_at=now_iso, checks=results)

    if report.worst == "BLOCK":
        blocked = ", ".join(c.name for c in results if c.status == "BLOCK")
        return JobOutput(
            status="ok",
            summary=f"close packet {month}: NOT rendered — preflight blocks on {blocked}",
            actions=["run close-preflight, resolve the blocks, then render again"],
        )
    if ctx.shadow or not ctx.tenant.close.report_dir:
        return JobOutput(
            status="ok",
            summary=f"close packet {month}: rendered nothing (shadow or no report_dir)",
        )
    out_path = f"{ctx.tenant.close.report_dir}/{month}/Close_Packet_{month}.xlsx"
    path = render_packet(
        close_ctx,
        results,
        legal_name=ctx.tenant.identity.legal_name,
        out_path=out_path,
        guard=ctx.guard,
    )
    return JobOutput(
        status="ok",
        summary=f"close packet {month}: rendered (verdict {report.worst})",
        actions=[f"wrote {path}"],
        events=[
            EventSpec(
                key=f"packet:{month}:{now_iso[:16]}",
                event_type="close.packet",
                payload={"month": month, "verdict": report.worst, "path": str(path)},
            )
        ],
    )


LOCK_ACTION = "close.lock_period"
LOCKED_EVENT = "close.locked"


def _lock_card_state(ctx: JobContext, month: str) -> tuple[str, int | None]:
    """(status, card_id) of the newest lock card for this month, or
    ("none", None). Newest wins: a rejected card followed by a re-run means
    the owner is asking again."""
    rows = ctx.ledger.conn.execute(
        "SELECT id, params_json, status FROM approval_queue WHERE tenant=? AND action_type=? "
        "ORDER BY id DESC",
        (ctx.tenant_slug, LOCK_ACTION),
    ).fetchall()
    import json

    for row in rows:
        try:
            params = json.loads(row["params_json"])
        except json.JSONDecodeError:
            continue
        if str(params.get("month", "")) == month:
            return str(row["status"]), int(row["id"])
    return "none", None


def _lock_key(ctx: JobContext) -> str:
    # The card's state is IN the key: the pre-approval run (parks the card)
    # and the post-approval run must never replay each other. So is the
    # readback date (seal redesign, incident 2026-08-03): after approval
    # the run records "awaiting the owner's UI act", and the owner setting
    # the date is what must break that replay so the verification re-run
    # executes and records the seal.
    month = _closing_month(ctx)
    state, card_id = _lock_card_state(ctx, month)
    current = _qbo_read_client(ctx).fetch_book_close_date()
    key = RunKey(ctx, "close-lock")
    key.add("month", month).add("state", state).add("card", str(card_id))
    key.add("readback", str(current))
    key.config("close")
    return key.digest()


def _lock_run(ctx: JobContext) -> JobOutput:
    """Seal the period: owner-executes-UI + engine-verifies.

    Redesigned after incident 2026-08-03 (first live close): the accounting
    system's API silently ignores book-close-date writes — the field is
    UI-only — so the engine NEVER writes it. First run parks one
    close.lock_period card (refused over a BLOCK month; open WARNs ride on
    the card so approving is an informed acceptance) whose text instructs
    the exact UI act. After the owner approves and makes the UI change, the
    next run verifies by readback and records the seal; an unverified
    readback never records, it re-instructs. Shadow never records."""
    from ...engine.contracts import ApprovalSpec
    from ...engine.result import Anomaly

    month = _closing_month(ctx)
    _, month_end = close_checks.month_bounds(month)
    now_iso = datetime.now(UTC).isoformat()

    client = _qbo_read_client(ctx)
    current = client.fetch_book_close_date()
    state, card_id = _lock_card_state(ctx, month)

    if state == "approved":
        if ctx.shadow:
            return JobOutput(
                status="ok",
                summary=f"close lock {month}: would verify the owner-set book-close "
                f"date against {month_end} (shadow)",
            )
        if current and current >= month_end:
            # The record says what was READ, never only what was asked: a
            # readback past the month end (a typo, or two months sealed in
            # one UI act) still seals (the books are closed through at least
            # that day) but is named as an overshoot (honesty audit
            # 2026-09-03, F10).
            overshoot = current > month_end
            summary = (
                f"close lock {month}: SEALED — book-close date {current} "
                "verified by readback (owner-set in the UI)"
            )
            anomalies = []
            if overshoot:
                summary += f"; the readback OVERSHOOTS the asked {month_end}"
                anomalies.append(
                    Anomaly(
                        code="close.seal_date_overshoots",
                        detail=f"{month}: asked for book-close date {month_end}, "
                        f"the accounting system reads {current}; sealed (the books "
                        "are closed through at least the asked day), but every "
                        "month up to the readback is closed too — confirm the "
                        "later date is deliberate",
                    )
                )
            return JobOutput(
                status="ok",
                summary=summary,
                actions=[f"verified owner-set book-close date {current}"],
                events=[
                    EventSpec(
                        key=f"locked:{month}",
                        event_type=LOCKED_EVENT,
                        payload={
                            "month": month,
                            "close_date": month_end,
                            "readback": current,
                            "card_id": card_id,
                        },
                    )
                ],
                anomalies=anomalies,
            )
        return JobOutput(
            status="ok",
            summary=f"close lock {month}: approved, awaiting the owner's UI act — "
            f"set the book-close date to {month_end} (it reads "
            f"{current or 'unset'}), then run lock again to verify and record",
            anomalies=[
                Anomaly(
                    code="close.seal_unverified",
                    detail=f"{month}: book-close date reads {current or 'unset'}, "
                    f"needs {month_end}; the engine never writes this field "
                    "(UI-only, incident 2026-08-03) — the owner sets it at "
                    "Settings -> Advanced -> Accounting, the engine verifies",
                )
            ],
        )

    if current and current >= month_end:
        return JobOutput(
            status="ok",
            summary=f"close lock {month}: already sealed (book-close date {current})",
        )

    if state == "pending":
        return JobOutput(
            status="ok",
            summary=f"close lock {month}: awaiting approval (card #{card_id})",
        )

    # No live card (none, or the owner rejected the last one): run the
    # checklist and park a fresh card — unless the month blocks.
    close_ctx = _build_close_ctx(ctx, month)
    results = _run_checks(close_ctx, now_iso)
    report = PreflightReport(tenant=ctx.tenant_slug, month=month, ran_at=now_iso, checks=results)
    if report.worst == "BLOCK":
        blocked = ", ".join(c.name for c in results if c.status == "BLOCK")
        return JobOutput(
            status="ok",
            summary=f"close lock {month}: NOT parked — preflight blocks on {blocked}",
            actions=["resolve the blocks, then run lock again"],
        )
    warns = "; ".join(c.summary for c in results if c.status == "WARN") or "none"
    # A rejected card followed by a re-run means the owner is asking again —
    # but the runner's stable-subject dedup (#143) would swallow a re-park on
    # the same key against the rejected row (incident 2026-09-01, issue #161).
    # The re-ask rides a fresh key naming the newest rejected card.
    park_key = f"lock:{month}" if state != "rejected" else f"lock:{month}:reask-{card_id}"
    return JobOutput(
        status="needs_approval",
        summary=f"close lock {month}: approval card parked (verdict {report.worst})",
        approvals=[
            ApprovalSpec(
                key=park_key,
                action_type=LOCK_ACTION,
                params={
                    "month": month,
                    "close_date": month_end,
                    "verdict": report.worst,
                    "open_warns": warns,
                },
                reason=f"seal {month}: set the book-close date to {month_end} in "
                "the accounting system UI (Settings -> Advanced -> Accounting; "
                "the API ignores this field, so the engine cannot set it). "
                "Approve, make the UI change, then run lock again — the engine "
                "verifies by readback and records the seal. Reversible in the UI.",
            )
        ],
    )


STATEMENTS_ACTION = "close.send_statements"
STATEMENTS_SENT_EVENT = "close.statements_sent"
_STATEMENTS_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _graph_send_client(ctx: JobContext):
    """Factory hook: evals monkeypatch this with a fake.

    Acquires the token HERE, eagerly, rather than letting the client fetch
    it lazily inside the transport call: the send path stamps send_started
    on the card before the POST (issue #135), and a missing/expired MSAL
    cache must surface BEFORE that stamp, or an auth failure is recorded as
    a send whose outcome is unknown (honesty audit 2026-09-03, F11). A
    ``GraphAuthError`` raised from here means nothing was attempted."""
    from ...adapters.graph_mail import GraphMailClient, keychain_token_provider

    mail = ctx.tenant.mail
    provider = keychain_token_provider(
        client_id=mail.client_id,
        tenant_id=mail.tenant_id,
        scopes=list(mail.scopes),
        keychain_service=mail.keychain_service,
        keychain_account=mail.keychain_account,
    )
    token = provider()  # raises GraphAuthError with no stamp on any card
    return GraphMailClient(token_provider=lambda: token)


def _reveal_in_finder(path) -> bool:
    """Open the OS file browser on the rendered workbook (ceremony
    convenience, config-gated). Never fails the run: a headless box just
    skips the reveal."""
    import subprocess
    import sys

    if sys.platform != "darwin":
        return False
    try:
        proc = subprocess.run(["/usr/bin/open", "-R", str(path)], check=False, timeout=10)
    except Exception:
        return False
    # "revealed" only when open said so: a headless launchd context or a
    # missing path exits non-zero (honesty audit 2026-09-03, F14).
    return proc.returncode == 0


def _statements_card_state(ctx: JobContext, month: str) -> tuple[str, int | None]:
    """Newest close.send_statements card for this month (same newest-wins
    reading as the lock card)."""
    import json

    rows = ctx.ledger.conn.execute(
        "SELECT id, params_json, status FROM approval_queue WHERE tenant=? AND action_type=? "
        "ORDER BY id DESC",
        (ctx.tenant_slug, STATEMENTS_ACTION),
    ).fetchall()
    for row in rows:
        try:
            params = json.loads(row["params_json"])
        except json.JSONDecodeError:
            continue
        if str(params.get("month", "")) == month:
            return str(row["status"]), int(row["id"])
    return "none", None


def _statements_already_sent(ctx: JobContext, month: str, card_id: int | None) -> bool:
    """One send per approved card: the sent event is the replay guard."""
    import json

    rows = ctx.ledger.conn.execute(
        "SELECT payload_json FROM events WHERE tenant=? AND event_type=?",
        (ctx.tenant_slug, STATEMENTS_SENT_EVENT),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            continue
        if str(payload.get("month")) == month and payload.get("card_id") == card_id:
            return True
    return False


def _statements_card_params(ctx: JobContext, card_id: int) -> dict:
    import json

    row = ctx.ledger.conn.execute(
        "SELECT params_json FROM approval_queue WHERE id = ?", (card_id,)
    ).fetchone()
    try:
        return json.loads(row["params_json"]) if row else {}
    except json.JSONDecodeError:
        return {}


def _card_recipients(ctx: JobContext, card_id: int | None) -> list[str] | None:
    """The addresses written on the card, or ``None`` for a card that names
    none (parked before cards carried them)."""
    if card_id is None:
        return None
    raw = _statements_card_params(ctx, card_id).get("recipients")
    if raw is None:
        return None
    return [a.strip() for a in str(raw).split(",") if a.strip()]


def _stamp_send_started(ctx: JobContext, card_id: int) -> None:
    """Durably mark the send attempt on the card BEFORE the transport call
    (issue #135). The card's params are already a mutable owner-visible
    surface (approval overrides rewrite them); the events table cannot hold
    a mid-run marker because a run row does not exist yet. A standing stamp
    with no sent event = an attempt with an unrecorded outcome."""
    import json

    params = _statements_card_params(ctx, card_id)
    params["send_started"] = datetime.now(UTC).isoformat()
    ctx.ledger.conn.execute(
        "UPDATE approval_queue SET params_json = ? WHERE id = ?",
        (json.dumps(params, sort_keys=True), card_id),
    )
    ctx.ledger.conn.commit()


def _statements_key(ctx: JobContext) -> str:
    # Re-render on every run (the statements are a regenerated view), with
    # the card's state in the key so the park/send phases never replay each
    # other — belt and suspenders on top of the sent-event guard.
    month = _closing_month(ctx)
    state, card_id = _statements_card_state(ctx, month)
    key = RunKey(ctx, "close-statements")
    key.add("month", month).add("state", state).add("card", str(card_id))
    key.config("close", "identity")
    key.stamp()
    return key.digest()


def _statements_run(ctx: JobContext) -> JobOutput:
    """The ceremony's closing act: render the sealed month's statements,
    reveal them, and email them for partner review through the queue.

    Refuses over an unsealed month (the statements are the CONCLUSION of
    the ceremony, so the seal is the gate). Every run re-renders the
    workbook; the send leg is two-phase like the lock — park the card,
    owner approves, next run sends and records the sent event, after which
    re-runs report "already sent"."""
    import calendar as _calendar

    from ...adapters.graph_mail import GraphAuthError
    from ...engine.contracts import ApprovalSpec
    from ...engine.result import Anomaly
    from .statements import render_statements

    month = _closing_month(ctx)
    _, month_end = close_checks.month_bounds(month)

    client = _qbo_read_client(ctx)
    sealed_through = client.fetch_book_close_date()
    if not sealed_through or sealed_through < month_end:
        return JobOutput(
            status="ok",
            summary=f"close statements {month}: month is not sealed "
            f"(book-close date {sealed_through or 'unset'}) — finish the ceremony first",
        )

    if ctx.shadow or not ctx.tenant.close.report_dir:
        return JobOutput(
            status="ok",
            summary=f"close statements {month}: would render (shadow or no report_dir)",
        )

    close_ctx = _build_close_ctx(ctx, month)
    out_path = f"{ctx.tenant.close.report_dir}/{month}/Financial_Statements_{month}.xlsx"
    path = render_statements(
        close_ctx,
        legal_name=ctx.tenant.identity.legal_name,
        year_start_month=ctx.tenant.fiscal.year_start_month,
        out_path=out_path,
        guard=ctx.guard,
    )
    actions = [f"wrote {path}"]
    events = [
        EventSpec(
            key=f"statements:{month}:{datetime.now(UTC).isoformat()[:16]}",
            event_type="close.statements",
            payload={"month": month, "path": str(path)},
        )
    ]
    if ctx.tenant.close.reveal_in_finder and _reveal_in_finder(path):
        actions.append("revealed in Finder")

    recipients = list(ctx.tenant.close.statements_recipients)
    if not recipients:
        return JobOutput(
            status="ok",
            summary=f"close statements {month}: rendered (no reviewer configured, send skipped)",
            actions=actions,
            events=events,
        )

    year, mon = (int(part) for part in month.split("-"))
    month_name = f"{_calendar.month_name[mon]} {year}"
    state, card_id = _statements_card_state(ctx, month)

    if state == "pending":
        return JobOutput(
            status="ok",
            summary=f"close statements {month}: rendered; send awaiting approval (card #{card_id})",
            actions=actions,
            events=events,
        )

    if state == "approved":
        if _statements_already_sent(ctx, month, card_id):
            return JobOutput(
                status="ok",
                summary=f"close statements {month}: rendered; already sent (card #{card_id})",
                actions=actions,
                events=events,
            )
        if card_id is not None and _statements_card_params(ctx, card_id).get("send_started"):
            # A send for this card started and its outcome was never
            # recorded (crash between the transport accept and the sent
            # event). At-most-once: never resend automatically — the owner
            # checks Sent Items, and a FRESH approved card is the
            # deliberate re-send (the standing doctrine).
            return JobOutput(
                status="needs_approval",
                summary=f"close statements {month}: a send for card #{card_id} started "
                "but its outcome was never recorded — not resending; check Sent Items",
                actions=actions,
                events=events,
                anomalies=[
                    Anomaly(
                        code="close.statements_send_unconfirmed",
                        detail=f"card #{card_id}: send_started with no sent event; "
                        f"verify delivery in Sent Items, then approve the fresh card "
                        "only if the email never went out",
                    )
                ],
                approvals=[
                    ApprovalSpec(
                        key=f"statements:{month}:resend-after:{card_id}",
                        action_type=STATEMENTS_ACTION,
                        params={
                            "month": month,
                            "path": str(path),
                            "recipients": ", ".join(recipients),
                        },
                        reason=f"re-send the {month_name} statements (the prior send's "
                        "outcome was never recorded; approve only if it never arrived)",
                    )
                ],
            )
        # The owner approved the addresses ON THE CARD; the tenant file may
        # have moved since. Send to the card's list, and when the file
        # disagrees send nothing and ask again with the new addresses
        # (security review 2026-10-03, #390). A card from before cards named
        # their recipients keeps the old reading.
        approved_to = _card_recipients(ctx, card_id)
        if approved_to is not None and approved_to != recipients:
            return JobOutput(
                status="needs_approval",
                summary=f"close statements {month}: recipients changed since card "
                f"#{card_id} was approved; nothing sent, fresh card parked",
                actions=actions,
                events=events,
                anomalies=[
                    Anomaly(
                        code="close.statements_recipients_changed",
                        detail=f"card #{card_id} approved {', '.join(approved_to)}; the "
                        f"tenant file now names {', '.join(recipients)}",
                    )
                ],
                approvals=[
                    ApprovalSpec(
                        key=f"statements:{month}:recipients-changed:{card_id}",
                        action_type=STATEMENTS_ACTION,
                        params={
                            "month": month,
                            "path": str(path),
                            "recipients": ", ".join(recipients),
                        },
                        reason=f"email the {month_name} P&L and balance sheet to "
                        f"{', '.join(recipients)} for review (the recipients changed "
                        f"after card #{card_id} was approved)",
                    )
                ],
            )
        # Order is the contract: client with its token in hand FIRST, then
        # the send_started stamp, then the POST. An auth failure here has
        # attempted nothing, so it stamps nothing and says so; the next run
        # sends normally. Everything after the stamp keeps the #135
        # at-most-once doctrine (a crash there is an unconfirmed send).
        try:
            mailer = _graph_send_client(ctx)
        except GraphAuthError as exc:
            return JobOutput(
                status="error",
                summary=f"close statements {month}: rendered; send NOT attempted "
                f"(card #{card_id}): mail auth failed — {exc}",
                actions=actions,
                events=events,
                anomalies=[
                    Anomaly(
                        code="close.statements_send_not_attempted",
                        detail=f"card #{card_id}: no mail token, nothing was sent and "
                        f"nothing was stamped; restore the mailbox consent and run "
                        f"statements again (the approved card still stands): {exc}",
                    )
                ],
            )
        _stamp_send_started(ctx, card_id)
        subject = f"{ctx.tenant.identity.legal_name} financial statements: {month_name}"
        body = (
            f"{month_name} is closed. The P&L and balance sheet are attached for review.\n\n"
            "Sent by the back-office engine at the conclusion of the month-end close, "
            "on the owner's approval.\n"
        )
        mailer.send_mail(
            subject=subject,
            body=body,
            to=recipients,
            attachments=[(path.name, _STATEMENTS_MIME, path.read_bytes())],
        )
        actions.append(f"sent to {', '.join(recipients)}")
        events.append(
            EventSpec(
                key=f"statements-sent:{month}:{card_id}",
                event_type=STATEMENTS_SENT_EVENT,
                payload={
                    "month": month,
                    "card_id": card_id,
                    "recipients": recipients,
                    "path": str(path),
                },
            )
        )
        return JobOutput(
            status="ok",
            summary=f"close statements {month}: rendered and SENT to {', '.join(recipients)}",
            actions=actions,
            events=events,
        )

    # No live card (none, or the owner rejected the last one): park one.
    # Same #161 rule as the lock: a re-ask after a reject must not reuse the
    # stable key or the runner's dedup swallows it against the rejected row.
    park_key = (
        f"statements:{month}" if state != "rejected" else f"statements:{month}:reask-{card_id}"
    )
    return JobOutput(
        status="needs_approval",
        summary=f"close statements {month}: rendered; send approval card parked",
        actions=actions,
        events=events,
        approvals=[
            ApprovalSpec(
                key=park_key,
                action_type=STATEMENTS_ACTION,
                params={
                    "month": month,
                    "path": str(path),
                    "recipients": ", ".join(recipients),
                },
                reason=f"email the {month_name} P&L and balance sheet to "
                f"{', '.join(recipients)} for review",
            )
        ],
    )


JOBS: dict[str, JobHandler] = {
    "preflight": JobHandler(key=_preflight_key, run=_preflight_run),
    "packet": JobHandler(key=_packet_key, run=_packet_run),
    "lock": JobHandler(key=_lock_key, run=_lock_run),
    "statements": JobHandler(key=_statements_key, run=_statements_run),
}


# What deciding each card is, for a tenant with authority.toml (#435;
# core.authority.CardRule). A tenant without one never reads this.
CARD_AUTHORITY: dict[str, CardRule] = {
    LOCK_ACTION: CardRule("approve", "books", money=False),
    STATEMENTS_ACTION: CardRule("send", "message", money=False),
}
