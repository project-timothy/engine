"""The report, month, and categorize jobs: a person's month becomes a review
card, then a built report package (workbook, receipts zip, manifest).
"""

from __future__ import annotations

import hashlib
import shutil
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from ...engine.contracts import ApprovalSpec, EventSpec, JobContext, JobOutput
from ...engine.result import Anomaly
from ...engine.runkey import RunKey
from ...policy import Spent, check_month, with_oks
from ...tools.viewer import unit_of
from ..policy.jobs import ASK_CARD, approved_oks, clock_in_force, rules_in_force
from . import categories as cats
from . import records, statements, year_close
from .common import (
    _apply_owner_price,
    _approved_cards,
    _events_of,
    _filing_dir,
    _now,
    _owner_prices_by_sha,
    _person_slug,
    _tenant_month,
)
from .confirm_card import (
    BUILT_EVENT,
    CONFIRM_REASON,
    _confirm_spec,
    _recover_stranded,
    _stranded_reports,
    _with_recovery,
)
from .dedup import duplicate_groups
from .records import (
    CATEGORY_EVENT,
    PRICED_EVENT,
    REVIEW_CARD,
    STATUS_OPEN,
)
from .report_lines import (
    _FX_FIELDS,
    _bank_items,
    _categories_by_sha,
    _drive_items,
    _fx_inputs,
    _is_door_receipt,
    _line_words,
    _manifest_note,
    _name_of,
    _own_part,
    _price_claims,
    _report_content_hash,
    _rules_in_force_key,
    _unconsumed,
    _with_statements,
    owed_back,
)
from .schema import (
    ManifestLine,
    ReportManifest,
)

DRAFT_CARD = "expenses.report_draft"


def _report_key(ctx: JobContext) -> str:
    key = RunKey(ctx, "expreport")
    key.add("person", str(key.param("person") or ""))
    # price:<sha16> directives are run inputs (an owner-supplied amount);
    # each is declared by name, and the scan itself is the wildcard read.
    for name in sorted(k for k, _v in ctx.params.items() if k.startswith("price:")):
        key.param(name)
    # Issue #135: the key hashes the month run() will actually use. Hashing
    # the unresolved param ('') let September's default-month survey replay
    # August's result verbatim; a new month is a new run by definition.
    key.add("month", str(key.param("month") or _tenant_month(ctx)))
    key.add("content", _report_content_hash(_unconsumed(ctx)))
    bank = _bank_items(ctx, str(key.param("month") or _tenant_month(ctx)))
    if bank:  # a tenant with no statements keeps the key it always had
        key.add("bank", _report_content_hash(bank))
    rates, pending_day = _fx_inputs(ctx, _unconsumed(ctx))
    key.value("fx", rates)
    key.value("placed", sorted(_categories_by_sha(ctx).items()))
    key.add("fx_pending_day", pending_day)
    key.value("approved", sorted(c["idempotency_key"] for c in _approved_cards(ctx, REVIEW_CARD)))
    # 03-F6: a stranded report (rows committed, nothing recorded) is a run
    # input; once recovered the key moves on and the next run builds.
    key.value("stranded", [r["id"] for r in _stranded_reports(ctx)])
    rules = _rules_in_force_key(ctx)
    if rules is not None:  # a ministry with no rules in force keeps the key it had
        key.value("rules", rules)
        key.config("fiscal")
        oks = _approved_cards(ctx, ASK_CARD)
        if oks:  # a yes given before the spend changes what the card says
            key.value("oks", sorted(c["idempotency_key"] for c in oks))
    key.param("filing_dir")
    key.config("expenses", "identity")  # persons, prefix, the metadata legal name
    key.config("books.cost_object")  # the fund or project label on a card line
    return key.digest()


def _render_workbook(path: Path, manifest: ReportManifest, legal_name: str) -> None:
    """The report xlsx: hardcoded values, tenant metadata (invariant 10)."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Expense Report"
    ws.append([f"{legal_name} — Expense Report {manifest.month}"])
    ws.append([f"Person: {manifest.person}"])
    ws.append([])
    ws.append(["Receipt", "Vendor", "Date", "Amount", "Category", "Project", "Note"])
    for line in manifest.lines:
        ws.append(
            [
                line.receipt_file,
                line.vendor,
                line.expense_date,
                line.amount_cents / 100,
                line.category,
                line.project,
                line.note,
            ]
        )
    ws.append([])
    ws.append(["Total", "", "", manifest.total_cents / 100])
    props = wb.properties
    props.creator = legal_name
    props.lastModifiedBy = legal_name
    wb.save(path)


def _parse_split(value: str) -> list[tuple[int, str, str]]:
    """Parse an owner split directive: ``amount:category[:note]|...``.

    Amounts are dollars, Decimal-parsed to cents (invariant 2's sibling:
    code computes, exactly). Raises ValueError on any malformed part.
    """
    from decimal import Decimal, InvalidOperation

    parts: list[tuple[int, str, str]] = []
    for chunk in str(value).split("|"):
        fields = chunk.strip().split(":", 2)
        if len(fields) < 2 or not fields[0].strip() or not fields[1].strip():
            raise ValueError(f"split part {chunk!r} is not amount:category[:note]")
        try:
            cents = int((Decimal(fields[0].strip()) * 100).to_integral_value())
        except InvalidOperation as exc:
            raise ValueError(f"split amount {fields[0]!r} is not a number") from exc
        note = fields[2].strip() if len(fields) == 3 else ""
        parts.append((cents, fields[1].strip(), note))
    if len(parts) < 2:
        raise ValueError("a split needs at least two parts")
    return parts


def _parse_price(value: str) -> tuple[int, str, str]:
    """Parse an owner price directive: ``amount:category[:note]``. One
    receipt, one amount, Decimal-parsed to cents, never zero or negative."""
    from decimal import Decimal, InvalidOperation

    fields = str(value).strip().split(":", 2)
    if len(fields) < 2 or not fields[0].strip() or not fields[1].strip():
        raise ValueError(f"price {value!r} is not amount:category[:note]")
    try:
        cents = int((Decimal(fields[0].strip()) * 100).to_integral_value())
    except InvalidOperation as exc:
        raise ValueError(f"price amount {fields[0]!r} is not a number") from exc
    if cents <= 0:
        raise ValueError(f"price amount {fields[0]!r} must be positive")
    note = fields[2].strip() if len(fields) == 3 else ""
    return cents, fields[1].strip(), note


def _price_directives(ctx: JobContext) -> dict[str, str]:
    """``price:<sha256[:16]>`` params on the report run: the owner pricing
    a receipt the extractor could not read (a two-receipts-on-one-scan
    page, a faded total) or correcting one it read wrong. The value is
    recorded as ``expense.owner_priced`` so every later run, including the
    approval that builds the report, carries it; a corrected receipt keeps
    its extractor amount in the event as ``previous_amount_cents``."""
    return {
        k.split(":", 1)[1]: str(v)
        for k, v in ctx.params.items()
        if k.startswith("price:") and k.split(":", 1)[1]
    }


def _split_directives(card: dict) -> dict[str, str]:
    """``split:<sha256[:16]>`` params from the approved review card (#110)."""
    return {
        k.split(":", 1)[1]: str(v)
        for k, v in (card.get("params") or {}).items()
        if k.startswith("split:") and k.split(":", 1)[1]
    }


def _price_items(
    items: list[dict], directives: dict[str, str], person: str, month: str
) -> tuple[list[dict], list[EventSpec], JobOutput | None]:
    """Lay the run's price directives over the person's unconsumed items.
    Owner values enter only through the directive; a malformed value or a
    prefix naming no receipt in this report refuses the whole build, and
    nothing is recorded (the split directive's rule, invariant 2)."""
    if not directives:
        return items, [], None
    pending = dict(directives)
    out: list[dict] = []
    events: list[EventSpec] = []
    for i in items:
        sha = str(i["sha256"])
        directive = pending.pop(sha[:16], None)
        if directive is None:
            out.append(i)
            continue
        try:
            cents, category, note = _parse_price(directive)
        except ValueError as exc:
            return (
                items,
                [],
                JobOutput(
                    status="ok",
                    summary=f"expenses report: price for {i['file']} is invalid; nothing built",
                    anomalies=[
                        Anomaly(code="expenses.price_invalid", detail=f"{i['file']}: {exc}")
                    ],
                ),
            )
        price = {
            "sha256": sha,
            "file": str(i.get("file") or ""),
            "amount_cents": cents,
            "category": category,
            "note": note,
            "previous_amount_cents": i.get("amount_cents"),
            "person": person,
            "month": month,
        }
        events.append(
            EventSpec(
                key=f"exppriced:{sha}:{cents}:{category}", event_type=PRICED_EVENT, payload=price
            )
        )
        out.append(_apply_owner_price(i, price))
    if pending:
        unknown = ", ".join(sorted(pending))
        return (
            items,
            [],
            JobOutput(
                status="ok",
                summary="expenses report: price directive names no receipt for "
                f"{person} {month}; nothing built",
                anomalies=[
                    Anomaly(
                        code="expenses.price_invalid",
                        detail=f"price sha prefix(es) {unknown} match no unconsumed receipt",
                    )
                ],
            ),
        )
    return out, events, None


def _pending_review_keys(ctx: JobContext, person: str, month: str) -> list[str]:
    """The keys of this person's month's review cards still waiting, so a
    new card for the same month retires the old one instead of piling up."""
    prefix = f"apr:{ctx.tenant_slug}:expenses:{REVIEW_CARD}:"
    rows = ctx.ledger.conn.execute(
        "SELECT idempotency_key FROM approval_queue "
        "WHERE tenant = ? AND action_type = ? AND status = 'pending'",
        (ctx.tenant_slug, REVIEW_CARD),
    ).fetchall()
    keys = [str(r[0])[len(prefix) :] for r in rows if str(r[0]).startswith(prefix)]
    return sorted(k for k in keys if k.startswith(f"expreport:{person}:{month}:"))


def _warnings(items: list[dict]) -> list[str]:
    """What a reviewer should know before a yes: each line's own flags, and
    scans that look like the same receipt twice."""
    warnings = [str(f) for i in items for f in i.get("flags", [])]
    dupes = duplicate_groups(
        [
            {
                "file": i.get("file"),
                "amount": i.get("amount_cents"),
                "date": i.get("expense_date"),
            }
            for i in items
        ]
    )
    for group in dupes:
        warnings.append("possible duplicate scans: " + ", ".join(sorted(group)))
    return warnings


def _already_words(ctx: JobContext, person: str, month: str, approved: list[dict], yes) -> str:
    """One line naming what the person's month already holds a yes for:
    "Already approved Oct 13: 2 receipts, $42.31". Receipts already in a
    report count on the day their report was built."""
    zone = ZoneInfo(ctx.tenant.identity.timezone)
    days: dict[str, list[int]] = {}

    def add(on: str, cents: int) -> None:
        when = datetime.fromisoformat(on).astimezone(zone).date().isoformat()
        days.setdefault(when, []).append(cents)

    for i in approved:
        add(yes[str(i["sha256"])].on, int(i["amount_cents"]))
    for r in ctx.ledger.conn.execute(
        "SELECT l.amount_cents, r.created_at FROM expense_line l "
        "JOIN expense_report r ON r.id = l.report_id "
        "WHERE r.tenant = ? AND r.person = ? AND r.month = ? AND r.shadow = 0",
        (ctx.tenant_slug, person, month),
    ).fetchall():
        add(str(r[1]), int(r[0]))
    said = []
    for when, cents in sorted(days.items()):
        day = date.fromisoformat(when)
        n = len(cents)
        said.append(
            f"{day:%b} {day.day}: {n} receipt{'s' if n != 1 else ''}, ${sum(cents) / 100:,.2f}"
        )
    return "Already approved " + "; ".join(said) if said else ""


def _legacy_yes(ctx: JobContext, person: str, month: str, items: list[dict]) -> dict | None:
    """An approved card from before cards named their receipts: it covers the
    month's receipts when its key carries their content, as it always did."""
    key = f"expreport:{person}:{month}:{_report_content_hash(items)[:16]}"
    return next(
        (
            c
            for c in _approved_cards(ctx, REVIEW_CARD)
            if c["idempotency_key"].endswith(key) and not records.card_receipts(c["params"])
        ),
        None,
    )


def _line_day(i: dict) -> str:
    """A line's own day (YYYY-MM-DD), "" when the books do not know it. A
    door receipt carries it in its file name."""
    door = "file" in i and _is_door_receipt(str(i["file"]))
    try:
        return date.fromisoformat(
            (_name_of(i) if door else str(i.get("expense_date", "")))[:10]
        ).isoformat()
    except ValueError:
        return ""


def _spent_words(i: dict) -> str:
    """A line the way a reviewer names it: what it was and its day ("Meals,
    Oct 12"). A door receipt is named by its category, as on the card."""
    door = "file" in i and _is_door_receipt(str(i["file"]))
    what = i.get("category") if door else i.get("note") or i.get("vendor")
    what = " ".join(str(what or "A receipt").split())
    what = what if len(what) <= 40 else what[:39].rstrip() + "…"
    day = _line_day(i)
    if not day:
        return what
    on = date.fromisoformat(day)
    return f"{what}, {on:%b} {on.day}"


def _policy_findings(
    ctx: JobContext, person: str, month: str, items: list[dict]
) -> tuple[str, bool] | None:
    """What the ministry's rules in force say about this person's month (the
    whole month: this card's lines, earlier yeses, and lines already in a
    report), in plain words with each rule's page; and whether a rule asks
    for the person's own Face ID. None when no rule is in force."""
    authority = ctx.authority
    if _rules_in_force_key(ctx) is None:
        return None
    if authority.rules_error:
        # Rules that will not load cannot say whether one asks for the
        # person's own yes, so the card asks for it until they load again.
        return (
            f"The ministry's rules could not be checked: {authority.rules_error}. Until "
            "they load again, this yes takes Face ID or the emailed link.",
            True,
        )
    who = unit_of(person)
    held = authority.policy.people.get(who)
    roles = held.roles if held else ()
    spent = [
        Spent(
            key=str(i["sha256"]),
            words=_spent_words(i),
            kind=str(i.get("category") or ""),
            cents=int(i["amount_cents"]),
            receipt=bool(i.get("drive")) or not i.get("bank") or bool(i.get("filed_to")),
            bank=bool(i.get("bank")),
            day=_line_day(i),
        )
        for i in items
        if i.get("amount_cents") is not None
    ]
    start = ctx.tenant.fiscal.year_start_month
    year, mon = int(month[:4]), int(month[5:7])
    first = f"{year if mon >= start else year - 1}-{start:02d}"
    year_before: dict[str, int] = {}
    for r in ctx.ledger.conn.execute(
        "SELECT r.month, l.receipt_sha256, l.vendor, l.note, l.expense_date, l.amount_cents, "
        "l.category FROM expense_line l JOIN expense_report r ON r.id = l.report_id "
        "WHERE r.tenant = ? AND r.person = ? AND r.shadow = 0 AND r.month >= ? AND r.month <= ?",
        (ctx.tenant_slug, person, first, month),
    ).fetchall():
        if r["month"] < month:
            year_before[r["category"]] = year_before.get(r["category"], 0) + int(r["amount_cents"])
        else:
            spent.append(
                Spent(
                    str(r["receipt_sha256"]),
                    _spent_words(dict(r)),
                    str(r["category"]),
                    int(r["amount_cents"]),
                    receipt=True,
                    day=_line_day(dict(r)),
                )
            )
    in_force = rules_in_force(ctx.ledger, ctx.tenant_slug, authority.rules)
    spent = with_oks(spent, approved_oks(ctx.ledger.conn, ctx.tenant_slug, who))
    found = check_month(in_force, spent, person=who, roles=roles, year_before=year_before)
    return "; ".join(f.says for f in found) or "none", any(f.face_id for f in found)


def _review_card(
    ctx: JobContext, person: str, month: str, items: list[dict], waiting: list[dict], yes
) -> JobOutput:
    """The card for what in a person's month still waits on a yes: every
    line, what the yes commits, what it leaves alone, and what only moved.
    A month of a closed year raises none: its books no longer change."""
    if year_close.closed_for(month, year_close.read(ctx.ledger.conn, ctx.tenant_slug)):
        return JobOutput(
            status="ok", summary=f"expenses report: {person} {month} is in closed books; no card"
        )
    approved = [i for i in items if str(i["sha256"]) in yes]
    card_key = f"expreport:{person}:{month}:{_report_content_hash(waiting)[:16]}"
    rules = _rules_in_force_key(ctx)
    if rules is not None:  # new rules in force are a new card (the old one retires)
        card_key += ":r" + hashlib.sha256("".join(rules).encode()).hexdigest()[:8]
        oks = approved_oks(ctx.ledger.conn, ctx.tenant_slug, unit_of(person))
        if oks:  # so is a yes given before the spend, for this person only
            card_key += ":k" + hashlib.sha256(repr(oks).encode()).hexdigest()[:8]
    label = ctx.tenant.books.cost_object.label
    funds = len({i.get("project") for i in items if i.get("project")})
    lines_text = "; ".join(_line_words(i, label, funds=funds) for i in waiting)
    older = [k for k in _pending_review_keys(ctx, person, month) if k != card_key]
    shas = {str(i["sha256"]) for i in waiting}
    params = {
        "person": person,
        "month": month,
        # What the yes commits: the ministry owes back only what the
        # person paid; a line the ministry paid itself is on the card
        # for the yes, never owed (t-statements).
        "total_cents": str(sum(owed_back(i) for i in waiting)),
        "lines": lines_text,
        "warnings": "; ".join(_warnings(waiting)) or "none",
        "receipts": ",".join(sorted(shas)),
    }
    already = _already_words(ctx, person, month, approved, yes)
    if already:
        params["already"] = already
    policy = _policy_findings(ctx, person, month, items)
    if policy is not None:
        params["policy"], face_id = policy
        if face_id:
            params["policy_face_id"] = "true"
    log = ctx.ledger.read_event_log()
    theirs = [ln for ln in statements.month_lines(log, month) if ln["person"] == person]
    moved = statements.moved_words(theirs)
    if moved:
        params["moved"] = moved
    repay = statements.repay_words(theirs)
    if repay:
        params["repay"] = repay  # the ministry's money they spent on themselves (t-cash-count)
    authority = ctx.authority
    rules = authority.rules if authority is not None and not authority.rules_error else None
    plan = clock_in_force(
        ctx.ledger.conn, ctx.tenant_slug, rules, authority.policy if authority else None, person
    )
    held = statements.cash_words_at(log, person, statements.month_end(month), plan)
    if held:
        params["cash"] = held
    return JobOutput(
        status="ok",
        summary=f"expenses report: {len(waiting)} line(s) for {person} {month} awaiting review",
        approvals=[
            ApprovalSpec(
                key=card_key,
                action_type=REVIEW_CARD,
                params=params,
                reason="only owner-approved values enter the workbook or the ledger (invariant 2)",
                supersedes_keys=older,
            )
        ],
    )


def _build_report(
    ctx: JobContext,
    person: str,
    month: str,
    items: list[dict],
    changed: frozenset[str] = frozenset(),
) -> JobOutput:
    events: list[EventSpec] = []
    actions: list[str] = []
    filing = _filing_dir(ctx)

    unpriced = [i for i in items if not i.get("amount_cents")]
    if unpriced:
        names = ", ".join(str(i.get("file")) for i in unpriced)
        return JobOutput(
            status="ok",
            summary=f"expenses report: {len(unpriced)} receipt(s) lack an amount "
            f"({names}); resolve extraction first",
        )

    # A yes is to the receipts its card named (records, 2026-10-14): a late
    # receipt waits on a card of its own, and the month builds once every
    # receipt in it holds a yes its facts still match.
    yes_cards = _approved_cards(ctx, REVIEW_CARD)
    yes = records.yeses(yes_cards, ctx.ledger.read_event_log(), changed)
    legacy = _legacy_yes(ctx, person, month, items)
    waiting = [] if legacy else [i for i in items if str(i["sha256"]) not in yes]
    if waiting:
        return _review_card(ctx, person, month, items, waiting, yes)

    # Yes'd: the ministry's own money is never owed; its cash paying first, only
    # the person's own part of that purchase is (the owner, 2026-10-16).
    items = [i if i.get("paid_by") != "ministry" else _own_part(i) for i in items if owed_back(i)]
    if not items:
        return JobOutput(
            status="ok",
            summary=f"expenses report: {person} {month} approved; the ministry owes nothing back",
        )
    content = _report_content_hash(items)
    total = sum(int(i["amount_cents"]) for i in items)

    if ctx.shadow:
        return JobOutput(
            status="ok",
            summary=f"expenses report: would build {person} {month} "
            f"(${total / 100:,.2f}, {len(items)} lines)",
        )

    # Owner-directed splits (issue #110, the Kala/hotel-folio shape): the
    # approved card may carry split:<sha16> directives. Owner values enter
    # only through the card; code enforces the arithmetic — parts that do
    # not sum exactly to the receipt refuse the whole build, nothing writes.
    covering = {y.card for s, y in yes.items() if s in {str(i["sha256"]) for i in items}}
    splits: dict[str, str] = {}
    for card in [legacy] if legacy else [c for c in yes_cards if int(c["id"]) in covering]:
        splits.update(_split_directives(card))
    lines: list[ManifestLine] = []
    for i in items:
        sha = str(i["sha256"])
        base = dict(
            receipt_file=str(i["file"]),
            receipt_sha256=sha,
            vendor=str(i.get("vendor") or ""),
            expense_date=str(i.get("expense_date") or ""),
            project=str(i.get("project") or ""),
        )
        directive = splits.pop(sha[:16], None)
        if directive is None:
            lines.append(
                ManifestLine(
                    amount_cents=int(i["amount_cents"]),
                    category=str(i.get("category") or ""),
                    note=_manifest_note(i),
                    part=int(i.get("part") or 0),  # a split line's parts are lines of their own
                    **{k: str(i.get(k) or "") for k in _FX_FIELDS},
                    **base,
                )
            )
            continue
        try:
            parts = _parse_split(directive)
        except ValueError as exc:
            return JobOutput(
                status="ok",
                summary=f"expenses report: split for {i['file']} is invalid; nothing built",
                anomalies=[Anomaly(code="expenses.split_invalid", detail=f"{i['file']}: {exc}")],
            )
        if sum(c for c, _, _ in parts) != int(i["amount_cents"]):
            return JobOutput(
                status="ok",
                summary=f"expenses report: split for {i['file']} does not sum to the "
                "receipt; nothing built",
                anomalies=[
                    Anomaly(
                        code="expenses.split_invalid",
                        detail=f"{i['file']}: parts sum "
                        f"{sum(c for c, _, _ in parts)} cents, receipt is "
                        f"{int(i['amount_cents'])} cents",
                    )
                ],
            )
        for n, (cents, category, note) in enumerate(parts, start=1):
            lines.append(
                ManifestLine(amount_cents=cents, category=category, note=note, part=n, **base)
            )
    if splits:
        unknown = ", ".join(sorted(splits))
        return JobOutput(
            status="ok",
            summary="expenses report: split directive names no receipt in this "
            "report; nothing built",
            anomalies=[
                Anomaly(
                    code="expenses.split_invalid",
                    detail=f"split sha prefix(es) {unknown} match no receipt in the report",
                )
            ],
        )

    report_key = f"expense-report:{ctx.tenant_slug}:{person}:{month}:{content[:16]}"
    manifest = ReportManifest(
        report_key=report_key,
        tenant=ctx.tenant_slug,
        person=person,
        month=month,
        total_cents=total,
        lines=lines,
    )

    slug = f"{_person_slug(person)}_EXP-{month}"
    report_dir = filing / month / "reports" / slug
    prefix = ctx.tenant.expenses.file_prefix
    prefix = f"{prefix}_" if prefix else ""
    xlsx = report_dir / f"{prefix}Expense_Report_{month}.xlsx"
    zip_path = report_dir / f"{prefix}Receipts_{month}.zip"
    manifest_path = report_dir / "manifest.json"
    for target in (xlsx, zip_path, manifest_path):
        ctx.guard.check_write(target)
    report_dir.mkdir(parents=True, exist_ok=True)
    _render_workbook(xlsx, manifest, ctx.tenant.identity.legal_name)
    filed_dir = filing / month / "_filed" / content[:16]
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as bundle:
        for i in items:
            src = Path(str(i["filed_to"]))
            if src.is_file():
                bundle.write(src, arcname=src.name)
    manifest_path.write_text(manifest.model_dump_json(indent=1))

    # Rows commit BEFORE the receipts move (issue #135): a crash mid-move
    # then leaves committed rows, a complete zip, and visibly loose
    # receipts — never a retry that rebuilds the package around an empty
    # zip because the files moved but nothing recorded their consumption.
    now = _now()
    ctx.ledger.conn.execute(
        "INSERT OR IGNORE INTO expense_report (idempotency_key, tenant, person, month, "
        "total_cents, status, report_path, manifest_path, shadow, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            report_key,
            ctx.tenant_slug,
            person,
            month,
            total,
            STATUS_OPEN,
            str(xlsx),
            str(manifest_path),
            0,
            now,
            now,
        ),
    )
    report_id = ctx.ledger.conn.execute(
        "SELECT id FROM expense_report WHERE idempotency_key = ?", (report_key,)
    ).fetchone()[0]
    for line in manifest.lines:
        ctx.ledger.conn.execute(
            "INSERT OR IGNORE INTO expense_line (idempotency_key, report_id, tenant, "
            "receipt_file, receipt_sha256, vendor, expense_date, amount_cents, category, "
            "project, note, said_amount, said_currency, fx_rate, fx_date, fx_source, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"{report_key}:line:{line.receipt_sha256}" + (f":{line.part}" if line.part else ""),
                report_id,
                ctx.tenant_slug,
                line.receipt_file,
                line.receipt_sha256,
                line.vendor,
                line.expense_date,
                line.amount_cents,
                line.category,
                line.project,
                line.note,
                *(getattr(line, k) for k in _FX_FIELDS),
                now,
            ),
        )
    ctx.ledger.conn.commit()

    for i in items:
        src = Path(str(i["filed_to"]))
        if src.is_file():
            dest = filed_dir / src.name
            ctx.guard.check_write(dest)
            filed_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), dest)

    actions.append(f"built {xlsx.name}: ${total / 100:,.2f}, {len(items)} lines")
    events.append(
        EventSpec(
            key=f"expreport-built:{content[:16]}",
            event_type=BUILT_EVENT,
            payload={
                "report_id": report_id,
                "report_key": report_key,
                "person": person,
                "month": month,
                "total_cents": total,
                "lines": len(manifest.lines),
                "report_path": str(xlsx),
            },
        )
    )
    confirm = _confirm_spec(
        ctx,
        {"id": report_id, "person": person, "month": month, "total_cents": total},
        reason=CONFIRM_REASON,
    )
    return JobOutput(
        status="ok",
        summary=f"expenses report: built {person} {month} (${total / 100:,.2f})",
        actions=actions,
        events=events,
        approvals=[confirm],
    )


def _report_run(ctx: JobContext) -> JobOutput:
    person = str(ctx.params.get("person") or "")
    month = str(ctx.params.get("month") or _tenant_month(ctx))
    # 03-F6: a stranded report is recovered whenever the job runs, in either
    # mode, independent of unconsumed receipts (its rows consumed them).
    recovery = _recover_stranded(ctx)
    items = _unconsumed(ctx)

    if person:
        mine = [i for i in items if i.get("person") == person and str(i.get("month")) == month]
        if not mine:
            return _with_recovery(
                recovery,
                JobOutput(
                    status="ok",
                    summary=f"expenses report: nothing unconsumed for {person} {month}",
                ),
            )
        return _with_recovery(recovery, _report_for(ctx, person, month, mine))

    # Survey mode (decision 4's month-end half): one draft card per person
    # holding unconsumed receipts, so the close never waits on loose paper.
    approvals: list[ApprovalSpec] = []
    by_person: dict[str, list[dict]] = {}
    for i in _off_cards(ctx, items):
        if str(i.get("month")) == month:
            by_person.setdefault(str(i.get("person")), []).append(i)
    for who, theirs in sorted(by_person.items()):
        approvals.append(
            ApprovalSpec(
                key=f"expdraft:{who}:{month}:{_report_content_hash(theirs)[:12]}",
                action_type=DRAFT_CARD,
                params={"person": who, "month": month, "count": str(len(theirs))},
                reason="unconsumed receipts for the month; run "
                f"`expenses report --param person={who}` to build the report",
            )
        )
    return _with_recovery(
        recovery,
        JobOutput(
            status="ok",
            summary=f"expenses report survey: {len(approvals)} person(s) with unconsumed "
            f"receipts in {month}",
            approvals=approvals,
        ),
    )


def _placed(ctx: JobContext, items: list[dict], extra: dict[str, str] | None = None) -> list[dict]:
    placed = {**_categories_by_sha(ctx), **(extra or {})}
    return [
        {**i, "category": placed[str(i.get("sha256"))]} if str(i.get("sha256")) in placed else i
        for i in items
    ]


def _report_for(
    ctx: JobContext,
    person: str,
    month: str,
    mine: list[dict],
    placed: dict[str, str] | None = None,
    pay: dict[str, str] | None = None,
) -> JobOutput:
    """One person's month: door claims priced (and any new rate recorded),
    categories placed, owner price directives applied, then the report built
    or its card raised. ``placed``: categories this run is recording; ``pay``:
    whose each receipt is, as this run records it. The person's ministry
    drives ride the same card. A receipt paid from their own pay is priced
    (so its day's rate is recorded for the tax packet) and then rides no
    card (c34 gap C)."""
    mine = _with_statements(ctx, month, person, mine)
    mine, rate_events = _price_claims(ctx, _placed(ctx, mine, placed))
    mine = _off_cards(ctx, mine, pay)
    if not mine:
        summary = f"expenses report: nothing on a card for {person} {month}"
        return JobOutput(status="ok", summary=summary, events=rate_events)
    priced, price_events, price_error = _price_items(mine, _price_directives(ctx), person, month)
    if price_error is not None:
        return price_error.model_copy(update={"events": [*rate_events, *price_error.events]})
    recorded, prices = _categories_by_sha(ctx), _owner_prices_by_sha(ctx)
    changed = {s for s, c in (placed or {}).items() if recorded.get(s) != c}
    for e in price_events:
        was = prices.get(str(e.payload["sha256"]), {})
        if any(was.get(k) != e.payload.get(k) for k in ("amount_cents", "category", "note")):
            changed.add(str(e.payload["sha256"]))
    output = _build_report(ctx, person, month, priced, frozenset(changed))
    events = [*rate_events, *price_events, *output.events]
    return output.model_copy(update={"events": events}) if events else output


def _off_cards(ctx: JobContext, items: list[dict], pay: dict[str, str] | None = None) -> list[dict]:
    """``items`` without the receipts paid from the person's own pay: the
    log's word on each, then this run's (``pay``)."""
    said = records.from_pay(ctx.ledger.read_event_log())
    for sha, whose in (pay or {}).items():
        if whose == records.FROM_PAY:
            said.add(sha)
        elif whose:
            said.discard(sha)
    return [i for i in items if str(i.get("sha256")) not in said]


# ---- job: month -------------------------------------------------------------


def _month_of(ctx: JobContext) -> str:
    """The month this run reviews: the one named, else the month that just
    ended on the tenant's own clock (the review follows the month)."""
    named = str(ctx.params.get("month") or "")
    if named:
        return named
    first = datetime.strptime(_tenant_month(ctx) + "-01", "%Y-%m-%d")
    return (first - timedelta(days=1)).strftime("%Y-%m")


def _month_key(ctx: JobContext) -> str:
    key = RunKey(ctx, "expmonth")
    key.param("month")
    for name in sorted(k for k, _v in ctx.params.items() if k.startswith("price:")):
        key.param(name)  # owner price directives, as the report job reads them
    month = _month_of(ctx)
    key.add("month_resolved", month)
    items = [i for i in _unconsumed(ctx) if str(i.get("month")) == month]
    key.add("content", _report_content_hash(items))
    key.add("drives", _report_content_hash(_drive_items(ctx, month)))
    bank = _bank_items(ctx, month)
    if bank:  # a tenant with no statements keeps the key it always had
        key.add("bank", _report_content_hash(bank))
    rates, pending_day = _fx_inputs(ctx, items)
    key.value("fx", rates)
    key.value("placed", sorted(_categories_by_sha(ctx).items()))
    key.add("fx_pending_day", pending_day)
    key.value("approved", sorted(c["idempotency_key"] for c in _approved_cards(ctx, REVIEW_CARD)))
    key.value("stranded", [r["id"] for r in _stranded_reports(ctx)])
    rules = _rules_in_force_key(ctx)
    if rules is not None:  # a ministry with no rules in force keeps the key it had
        key.value("rules", rules)
        key.config("fiscal")
        oks = _approved_cards(ctx, ASK_CARD)
        if oks:  # a yes given before the spend changes what the card says
            key.value("oks", sorted(c["idempotency_key"] for c in oks))
    closed = sorted(year_close.read(ctx.ledger.conn, ctx.tenant_slug))
    if closed:  # a ministry that never closed a year keeps the key it had
        key.value("closed", closed)
    key.param("filing_dir")
    key.config("expenses", "identity")
    key.config("books.cost_object")  # the fund or project label on a card line
    key.config("institution")
    return key.digest()


def _month_run(ctx: JobContext) -> JobOutput:
    """The missionary's month (alpha step 3): every person holding receipts
    for the month gets one report and one review card, so the reviewer's yes
    is the only step left. Approved cards build their report on the next run.
    A ministry whose institution takes its claims ([institution] submit_to)
    gets no report here: the send job carries those receipts, so none is
    ever both claimed from the institution and paid by the books."""
    month = _month_of(ctx)
    institution = ctx.tenant.institution
    if institution.submit_to:
        return JobOutput(
            status="ok",
            summary=f"expenses month {month}: receipts go to "
            f"{institution.name or 'the institution'} on each person's yes (the send job)",
        )
    recovery = _recover_stranded(ctx)
    by_person: dict[str, list[dict]] = {}
    for i in _unconsumed(ctx):
        if str(i.get("month")) == month:
            by_person.setdefault(str(i.get("person")), []).append(i)
    for d in [*_drive_items(ctx, month), *_bank_items(ctx, month)]:
        by_person.setdefault(d["person"], [])
    summaries, actions, events, approvals, anomalies = [], [], [], [], []
    seen: set[str] = set()
    for person, mine in sorted(by_person.items()):
        out = _report_for(ctx, person, month, mine)
        summaries.append(out.summary)
        actions += out.actions
        approvals += out.approvals
        anomalies += out.anomalies
        for e in out.events:
            if e.key not in seen:
                seen.add(e.key)
                events.append(e)
    return _with_recovery(
        recovery,
        JobOutput(
            status="ok",
            summary=f"expenses month {month}: "
            + ("; ".join(summaries) if summaries else "no receipts"),
            actions=actions,
            events=events,
            approvals=approvals,
            anomalies=anomalies,
        ),
    )


# ---- job: categorize --------------------------------------------------------


def _categorize_key(ctx: JobContext) -> str:
    key = RunKey(ctx, "expcat")
    sha = str(key.param("sha"))
    for name in ("category", "by", "said", "filing_dir", "whose"):
        key.param(name)
    for name in sorted(k for k, _v in ctx.params.items() if k.startswith("price:")):
        key.param(name)  # a month raised again reads owner price directives too
    # The same words twice is one change; a move back after another move is new.
    key.add(
        "before",
        sum(
            1 for e in _events_of(ctx, CATEGORY_EVENT) if e.get("payload", {}).get("sha256") == sha
        ),
    )
    key.config("expenses", "identity", "books.cost_object")
    return key.digest()


def _categorize_run(ctx: JobContext) -> JobOutput:
    """Place one receipt in a category (the owner's decisions of 2026-10-13):
    by the person's own words when it lands, or by a reviewer, or the person,
    asking their AI. A category moves no money, so the word decides; the
    change is recorded with who said it, and a month already waiting for its
    yes is raised again showing it, before anyone says yes."""
    sha = str(ctx.params.get("sha") or "")
    by = str(ctx.params.get("by") or "")
    said = str(ctx.params.get("said") or "")
    listed = list(ctx.tenant.expenses.category_accounts)
    whose = str(ctx.params.get("whose") or "")
    receipt = next((i for i in _unconsumed(ctx) if i["sha256"] == sha), None)
    asked = ctx.params.get("category")
    if asked is None and whose and receipt is not None:
        asked = _categories_by_sha(ctx).get(sha, receipt.get("category") or "")  # it stays put
    category = cats.canonical(str(asked or ""), listed)
    if category is None:
        names = ", ".join(listed) or "none yet"
        return JobOutput(
            status="ok",
            summary=f"expenses categorize: no category {ctx.params.get('category')!r} "
            f"(the ministry's are {names})",
        )
    if receipt is None:
        return JobOutput(
            status="ok", summary="expenses categorize: no such receipt waiting in a month"
        )
    before = sum(
        1 for e in _events_of(ctx, CATEGORY_EVENT) if e.get("payload", {}).get("sha256") == sha
    )
    event = EventSpec(
        key=f"expcat:{sha}:{before}",
        event_type=CATEGORY_EVENT,
        payload={
            "sha256": sha,
            "category": category,
            "by": by,
            "said": said,
            **({"whose": whose} if whose in (records.FROM_PAY, statements.MINISTRY) else {}),
        },
    )
    person, month = str(receipt.get("person")), str(receipt.get("month"))
    output = JobOutput(
        status="ok",
        summary=f"expenses categorize: {receipt.get('file')} -> "
        f"{category or cats.UNCATEGORIZED} (by {by or 'nobody named'})",
        events=[event],
    )
    if not _pending_review_keys(ctx, person, month):
        return output
    mine = [i for i in _unconsumed(ctx) if i.get("person") == person and i.get("month") == month]
    again = _report_for(ctx, person, month, mine, placed={sha: category}, pay={sha: whose})
    return again.model_copy(
        update={"summary": f"{output.summary}; {again.summary}", "events": [event, *again.events]}
    )
