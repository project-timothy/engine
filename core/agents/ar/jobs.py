"""AR agent jobs: remittance.

One job, two halves, both deterministic:

1. **the advice**: a message whose subject carries the tenant's remittance
   marker is parsed body-only and recorded once per payment number
   (``ar.remittance.received``);
2. **the bank**: a deposit on one of the bank's own statement files whose
   amount equals a recorded remittance closes the loop
   (``ar.remittance.cleared``), which is free evidence: the statement tier
   already parses and ties the Deposits section every morning.

Both halves are idempotent against the EVENT LOG, not against the run key: a
mailbox that gains one message moves the key, and the payment recorded
yesterday must not be recorded again today.

What this job deliberately does NOT do: touch the AR register, write the
accounting system, or move money. The register card (``ar.remittance_record``)
waits for the AR design day to settle whether the workbook stays the system
of record or the accounting system becomes it. This job records what the
customer said and what the bank did; every downstream act reads those events.
"""

from __future__ import annotations

import hashlib
from datetime import date, timedelta
from pathlib import Path

from ...engine.clock import local_today
from ...engine.contracts import EventSpec, JobContext, JobHandler, JobOutput
from ...engine.result import Anomaly
from ...engine.runkey import RunKey
from ..mail.schema import sender_is_denied
from .schema import (
    Remittance,
    RemittanceMessage,
    RemittanceNotRecognized,
    parse_remittance,
    sender_matches,
    subject_is_remittance,
)

RECEIVED_EVENT = "ar.remittance.received"
CLEARED_EVENT = "ar.remittance.cleared"
UNPARSED = "ar.remittance.unparsed"
STATEMENT_UNPARSED = "ar.remittance.statement_unparsed"
STATEMENT_UNREADABLE = "ar.remittance.statement_unreadable"

NOTE_DIR = "_ar"

# The candidate listing, fetched while the key is computed and handed to
# run() for the same context (the runner calls key() then run() once per
# context; listing twice could hash a different mailbox than the run acts on).
_CANDIDATES: dict[int, list[RemittanceMessage]] = {}


# ---- the mailbox -------------------------------------------------------------


def _denied(ctx: JobContext) -> list[str]:
    """The mail lane's denylist, same param override it accepts."""
    override = ctx.params.get("denied_senders")
    if override is not None:
        return [t for t in str(override).split(",") if t.strip()]
    return list(ctx.tenant.mail.denied_senders)


def _matches(ctx: JobContext, message: RemittanceMessage) -> bool:
    """The three gates, in the order that decides whether a body is read at
    all: the tenant's privacy denylist first (the mail lane's own boundary,
    honored here so a denied sender's body is never opened whatever its
    subject says), then the subject marker, then the sender list."""
    ar = ctx.tenant.ar
    if sender_is_denied(message.sender, _denied(ctx)):
        return False
    return subject_is_remittance(message.subject, ar.remittance_subject) and sender_matches(
        message.sender, list(ar.remittance_senders)
    )


def _candidates(ctx: JobContext) -> list[RemittanceMessage]:
    """Every message the gates admit, body loaded. A fixture file carries the
    whole shape; the live path lists metadata and reads ONLY the bodies of
    the messages that already matched.

    An auth or transport failure propagates: the runner records the failed
    run with its cause (#172) and the morning's exit code says so. A lane
    that swallowed a dead mailbox would look exactly like a quiet week.
    """
    import json

    override = ctx.params.get("messages_file")
    if override:
        raw = json.loads(Path(override).read_text())
        return [m for m in (RemittanceMessage(**item) for item in raw) if _matches(ctx, m)]

    from ...adapters.mail import client_for

    mail = ctx.tenant.mail
    if not mail.client_id:
        return []
    client = client_for(mail)
    since = (
        ctx.params.get("since")
        or (date.today() - timedelta(days=ctx.tenant.ar.since_days)).isoformat()
    )
    picked: list[RemittanceMessage] = []
    listing = client.list_messages(
        since=since, with_attachments=False, folder=ctx.tenant.ar.mail_folder
    )
    for summary in listing:
        message = RemittanceMessage(
            id=summary.id,
            sender=summary.sender,
            subject=summary.subject,
            date=summary.received,
        )
        if not _matches(ctx, message):
            continue
        _content_type, message.body = client.get_body(message.id)
        picked.append(message)
    return picked


# ---- ledger memory -----------------------------------------------------------


def _recorded(ctx: JobContext) -> dict[str, Remittance]:
    """Every remittance this ledger already holds, by payment number."""
    seen: dict[str, Remittance] = {}
    for event in ctx.ledger.read_event_log():
        if event.get("event_type") != RECEIVED_EVENT:
            continue
        payload = event.get("payload") or {}
        try:
            remittance = Remittance(**payload)
        except (TypeError, ValueError):  # pragma: no cover - a payload this job wrote
            continue
        seen[remittance.payment_number] = remittance
    return seen


def _cleared(ctx: JobContext) -> set[str]:
    return {
        str((e.get("payload") or {}).get("payment_number", ""))
        for e in ctx.ledger.read_event_log()
        if e.get("event_type") == CLEARED_EVENT
    }


# ---- the bank statement ------------------------------------------------------


def _statement_dir(ctx: JobContext) -> Path | None:
    raw = str(ctx.params.get("statement_dir") or ctx.tenant.bank_csv.statement_dir or "")
    return Path(raw).expanduser() if raw.strip() else None


def _deposits(ctx: JobContext) -> tuple[list[tuple[str, object]], list[str], list[Anomaly]]:
    """Every deposit line the bank's own statement files carry, as
    ``(file name, BankLine)``, plus log notes and the anomalies of files
    that could not be read.

    A folder that is not there is "no statements yet", not a failure. A
    folder that IS there and cannot be listed is a failure and says so
    (2026-09-13: the silent readdir denial).
    """
    from ...adapters.bank_statement_pdf import (
        BankStatementError,
        StatementNotRecognized,
        parse_statement_file,
        statement_files,
    )

    directory = _statement_dir(ctx)
    if directory is None:
        return [], ["statement tier skipped: no statement_dir param and none in [bank_csv]"], []
    if not directory.is_dir():
        return [], [f"statement tier: no statement folder at {directory}"], []
    try:
        paths = statement_files(directory, ctx.tenant.bank_csv)
    except OSError as exc:
        return (
            [],
            [f"statement tier: COULD NOT LIST {directory}"],
            [
                Anomaly(
                    code=STATEMENT_UNREADABLE,
                    detail=(
                        f"could not list the statement folder {directory}: {exc}. This is not "
                        "'no statement this morning': the folder was not read, so a remittance "
                        "the bank has already paid stays open."
                    ),
                )
            ],
        )
    lines: list[tuple[str, object]] = []
    notes: list[str] = []
    anomalies: list[Anomaly] = []
    for path in paths:
        try:
            sections = parse_statement_file(path, ctx.tenant.bank_csv)
        except StatementNotRecognized:
            continue
        except BankStatementError as exc:
            anomalies.append(Anomaly(code=STATEMENT_UNPARSED, detail=str(exc)))
            continue
        lines.extend((path.name, line) for line in sections.deposits)
    if lines:
        notes.append(f"statement tier: {len(lines)} deposit line(s) from {len(paths)} file(s)")
    return lines, notes, anomalies


def _matching_deposit(
    ctx: JobContext,
    remittance: Remittance,
    deposits: list[tuple[str, object]],
    claimed: set[int],
) -> tuple[int, str, object] | None:
    """The first unclaimed deposit that IS this payment: the amount to the
    cent, the payer's wording when the tenant names any, and a date on or
    after the payment date (a remittance is written before the money lands).

    ``claimed`` carries the line positions this run already used. One deposit
    is one payment: two advices for the same amount inside the same window
    must not both claim the single line that only one of them explains.
    """
    if remittance.amount_cents <= 0:
        return None
    markers = [m for m in ctx.tenant.ar.deposit_markers if m.strip()]
    window = ctx.tenant.ar.clearing_window_days
    paid_on = _as_date(remittance.payment_date)
    for position, (file_name, line) in enumerate(deposits):
        if position in claimed:
            continue
        if int((line.amount * 100).to_integral_value()) != remittance.amount_cents:
            continue
        if markers and not any(m.strip().lower() in line.description.lower() for m in markers):
            continue
        if paid_on is not None:
            landed = _as_date(line.date)
            if landed is None:
                continue
            if landed < paid_on - timedelta(days=1) or landed > paid_on + timedelta(days=window):
                continue
        return position, file_name, line
    return None


def _as_date(raw: str) -> date | None:
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


# ---- the note the morning brief reads ----------------------------------------


def _note_dir(ctx: JobContext) -> Path | None:
    raw = str(ctx.params.get("report_dir") or ctx.tenant.ar.report_dir or "").strip()
    if not raw:
        raw = str(ctx.tenant.close.report_dir or "").strip()
    return Path(raw).expanduser() if raw else None


def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def render_note(day: str, received: list[Remittance], cleared: list[dict]) -> str:
    """The note in the section shape the morning brief's reader already
    understands: ``## Money in`` first, one bullet per remittance."""
    lines = [
        f"# AR remittances {day}",
        "",
        "Written by the engine's daily run. Every line is a customer's own "
        "remittance advice or the bank's own statement, never an inference.",
        "",
    ]
    if received:
        lines += ["## Money in", ""]
        for remittance in received:
            detail = ", ".join(
                f"{line.invoice_number} {_money(line.amount_paid_cents)}"
                for line in remittance.invoices
            )
            paid_on = remittance.payment_date or "a date the advice did not state"
            bullet = (
                f"- Remittance {remittance.payment_number}: "
                f"{_money(remittance.amount_cents)} paid {paid_on}"
            )
            if detail:
                bullet += f" ({detail})"
            lines.append(bullet + ". Watch the deposit.")
        lines.append("")
    if cleared:
        lines += ["## Cleared", ""]
        for item in cleared:
            lines.append(
                f"- Remittance {item['payment_number']}: {_money(item['amount_cents'])} landed "
                f"{item['deposit_date']} on {item['statement_file']}."
            )
        lines.append("")
    return "\n".join(lines)


def _write_note(ctx: JobContext, day: str, text: str) -> Path | None:
    directory = _note_dir(ctx)
    if directory is None:
        return None
    target = ctx.guard.check_write(directory / NOTE_DIR / f"remittance-{day}.md")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


# ---- the job -----------------------------------------------------------------


def _remittance_key(ctx: JobContext) -> str:
    """Same candidate mail + same statement folder + same recorded set = same
    run. The recorded set rides the key because it decides what can still
    clear: yesterday's advice must be able to meet today's deposit."""
    candidates = _candidates(ctx)
    _CANDIDATES[id(ctx)] = candidates
    key = RunKey(ctx, "arremittance")
    key.value(
        "messages",
        sorted(f"{m.id}:{hashlib.sha256(m.body.encode('utf-8')).hexdigest()}" for m in candidates),
    )
    key.config("ar")  # the gates, the clearing window, the note destination
    key.config("bank_csv")  # the statement folder and how its files read
    key.config("mail")  # the mailbox the live listing reads
    key.config("close.report_dir")  # the note's fallback destination
    key.config("identity.timezone")  # the note's day
    key.param("messages_file")
    key.param("since")
    key.param("denied_senders")
    key.param("report_dir")
    key.param("statement_dir")
    directory = _statement_dir(ctx)
    if directory is not None and directory.is_dir():
        from ...adapters.bank_statement_pdf import statement_files

        try:
            key.files(statement_files(directory, ctx.tenant.bank_csv), label="statement_dir")
        except OSError:
            # A folder that cannot be listed must never replay: the run
            # executes again, and says so again, every morning until it is
            # fixed (the same rule ap/reconcile keeps).
            key.stamp()
    key.value("recorded", sorted(_recorded(ctx)))
    key.value("cleared", sorted(_cleared(ctx)))
    return key.digest()


def _remittance_run(ctx: JobContext) -> JobOutput:
    candidates = _CANDIDATES.pop(id(ctx), None)
    if candidates is None:  # defensive: key() always ran first under the runner
        candidates = _candidates(ctx)

    recorded = _recorded(ctx)
    cleared_already = _cleared(ctx)
    events: list[EventSpec] = []
    anomalies: list[Anomaly] = []
    actions: list[str] = []
    new: list[Remittance] = []
    duplicates = 0

    for message in candidates:
        try:
            remittance = parse_remittance(subject=message.subject, body=message.body)
        except RemittanceNotRecognized as exc:
            anomalies.append(
                Anomaly(code=UNPARSED, detail=f"{message.subject.strip()[:140]}: {exc}")
            )
            continue
        number = remittance.payment_number
        if number in recorded:
            duplicates += 1
            actions.append(f"remittance {number}: already recorded")
            continue
        recorded[number] = remittance
        new.append(remittance)
        paid_on = remittance.payment_date or "a date the advice did not state"
        summary = f"remittance {number}: {_money(remittance.amount_cents)} paid {paid_on}"
        if ctx.shadow:
            actions.append(f"would record {summary}")
            continue
        actions.append(f"recorded {summary}")
        events.append(
            EventSpec(
                key=f"remittance:{number}",
                event_type=RECEIVED_EVENT,
                payload=remittance.model_dump(),
            )
        )

    deposits, notes, statement_anomalies = _deposits(ctx)
    actions.extend(notes)
    anomalies.extend(statement_anomalies)
    cleared_now: list[dict] = []
    claimed: set[int] = set()
    for number in sorted(recorded):
        if number in cleared_already:
            continue
        found = _matching_deposit(ctx, recorded[number], deposits, claimed)
        if found is None:
            continue
        position, file_name, line = found
        claimed.add(position)
        payload = {
            "payment_number": number,
            "amount_cents": recorded[number].amount_cents,
            "payment_date": recorded[number].payment_date,
            "deposit_date": line.date,
            "deposit_description": line.description,
            "statement_file": file_name,
        }
        verb = "would clear" if ctx.shadow else "cleared"
        actions.append(
            f"{verb} remittance {number}: {_money(payload['amount_cents'])} on {line.date} "
            f"({file_name})"
        )
        if ctx.shadow:
            continue
        cleared_now.append(payload)
        events.append(EventSpec(key=f"cleared:{number}", event_type=CLEARED_EVENT, payload=payload))

    if not ctx.shadow and (new or cleared_now):
        day = local_today(str(ctx.tenant.identity.timezone or "UTC"))
        note = _write_note(ctx, day, render_note(day, new, cleared_now))
        actions.append(f"note written: {note}" if note else "no note: no report_dir configured")

    summary = (
        f"ar remittance: {len(new)} received, {len(cleared_now)} cleared, "
        f"{duplicates} already recorded"
    )
    return JobOutput(
        status="ok", summary=summary, actions=actions, events=events, anomalies=anomalies
    )


JOBS: dict[str, JobHandler] = {
    "remittance": JobHandler(key=_remittance_key, run=_remittance_run),
}
