"""The close checklist: deterministic, month-scoped checks.

Build step 1 implements checks 1 (machinery), 4 (AP tie-out), and
7 (categorization sweep) — the ones needing only the ledger, the auditor's
store as data, and accounting-system reads. Later steps add the rest; until
then they render as TODO so the report never silently narrows.

Every check returns a :class:`CheckResult` and raises nothing: a crashed
check is itself a BLOCK (the preflight must never die of one bad check).
"""

from __future__ import annotations

import calendar
import os
import sqlite3
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ...ledger import Ledger
from ..ap.status import is_settled
from .schema import CheckResult

RECONCILE_PAID_EVENT = "ap.reconcile.paid"
UNCATEGORIZED_MARKERS = ("uncategor", "ask my accountant")
AUDITOR_STALE_HOURS = 48
_DETAIL_CAP = 8  # keep the report readable; the count says how many more


@dataclass
class CloseContext:
    """Everything the checks need. ``qbo`` is whatever the factory hook in
    jobs.py produced; evals hand in a fake. ``statement_balance_cents`` is
    the one hand-carried number of the ceremony (None until the owner reads
    it off QBO's statement tab)."""

    tenant_slug: str
    ledger: Ledger
    month: str  # YYYY-MM
    qbo: object
    auditor_store: Path
    uncategorized_block_over_cents: int
    bank_account: str = ""
    statement_balance_cents: int | None = None
    owner_names: list[str] | None = None
    payroll_markers: list[str] | None = None
    expenses_dir: str = ""
    invoice_register_xlsx: str = ""


def month_bounds(month: str) -> tuple[str, str]:
    year, mm = int(month[:4]), int(month[5:7])
    last = calendar.monthrange(year, mm)[1]
    return f"{month}-01", f"{month}-{last:02d}"


def _dollars_to_cents(text: str) -> int | None:
    cleaned = str(text).replace("$", "").replace(",", "").strip()
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    if negative:
        cleaned = cleaned[1:-1]
    if not cleaned:
        return None
    try:
        cents = int((Decimal(cleaned) * 100).to_integral_value())
    except InvalidOperation:
        return None
    return -cents if negative else cents


def _cents_str(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _capped(items: list[str]) -> list[str]:
    if len(items) <= _DETAIL_CAP:
        return items
    return items[:_DETAIL_CAP] + [f"... and {len(items) - _DETAIL_CAP} more"]


# ---- check 1: machinery clean ----------------------------------------------


def default_auditor_store(slug: str) -> Path:
    """The auditor's store location, by the auditor's own convention
    ($AUDITOR_STORE_ROOT or ./.auditor). Data coupling, not code coupling:
    this is a file on disk, read with our own SQL."""
    base = os.environ.get("AUDITOR_STORE_ROOT") or ".auditor"
    return Path(base).expanduser() / slug / "auditor.sqlite3"


def check_machinery(ctx: CloseContext, *, now_iso: str) -> CheckResult:
    """The nightly checker trusts the book, and looked recently."""
    name = "machinery"
    if not ctx.auditor_store.exists():
        return CheckResult(
            name=name,
            status="BLOCK",
            summary=f"no auditor store at {ctx.auditor_store}; a close over a book "
            "no checker watches is theater — stand the auditor up first",
        )
    conn = sqlite3.connect(f"file:{ctx.auditor_store}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        open_rows = conn.execute(
            "SELECT severity, lens, subject FROM findings WHERE tenant=? AND state='open' "
            "ORDER BY severity DESC, first_seen",
            (ctx.tenant_slug,),
        ).fetchall()
        last_run = conn.execute(
            "SELECT started_at, status FROM auditor_runs WHERE tenant=? ORDER BY id DESC LIMIT 1",
            (ctx.tenant_slug,),
        ).fetchone()
    finally:
        conn.close()

    critical = [r for r in open_rows if r["severity"] == "CRITICAL"]
    if critical:
        return CheckResult(
            name=name,
            status="BLOCK",
            summary=f"the auditor's checklist holds {len(critical)} open CRITICAL item(s)",
            details=_capped([f"({r['lens']}) {r['subject']}" for r in critical]),
        )

    from datetime import datetime

    stale = last_run is None
    if last_run is not None:
        age_hours = (
            datetime.fromisoformat(now_iso) - datetime.fromisoformat(last_run["started_at"])
        ).total_seconds() / 3600
        stale = age_hours > AUDITOR_STALE_HOURS or last_run["status"] != "ok"
    if stale:
        return CheckResult(
            name=name,
            status="WARN",
            summary="the auditor has not completed a clean run in the last "
            f"{AUDITOR_STALE_HOURS}h; its clean checklist is stale",
        )
    if open_rows:
        return CheckResult(
            name=name,
            status="WARN",
            summary=f"the auditor's checklist holds {len(open_rows)} open non-critical item(s)",
            details=_capped([f"({r['lens']}) {r['subject']}" for r in open_rows]),
        )
    return CheckResult(
        name=name,
        status="OK",
        summary=f"auditor checklist empty; last nightly {last_run['started_at'][:10]} clean",
    )


# ---- check 4: AP tie-out ---------------------------------------------------


def _paid_without_evidence(ctx: CloseContext) -> list[str]:
    """Month's Paid rows with nothing showing the money actually cleared."""
    rows = ctx.ledger.conn.execute(
        "SELECT * FROM ap_invoices WHERE tenant=? AND status='Paid' AND payment_date LIKE ?",
        (ctx.tenant_slug, f"{ctx.month}%"),
    ).fetchall()
    reconciled: set[int] = set()
    for event in ctx.ledger.read_event_log():
        if event.get("event_type") == RECONCILE_PAID_EVENT:
            invoice_id = (event.get("payload") or {}).get("invoice_id")
            if invoice_id is not None:
                reconciled.add(int(invoice_id))
    bad: list[str] = []
    for row in rows:
        if row["check_ref"] or row["id"] in reconciled:
            continue
        flips = ctx.ledger.conn.execute(
            "SELECT actor, note FROM ap_status_history WHERE invoice_id=? AND status_to='Paid'",
            (row["id"],),
        ).fetchall()
        if any(f["actor"] == "owner" or f["note"] for f in flips):
            continue
        bad.append(
            f"#{row['id']} {row['vendor']} / {row['invoice_number']} "
            f"({_cents_str(row['amount_cents'])}): Paid {row['payment_date']} with no "
            "check ref, no reconcile event, no owner flip"
        )
    return bad


def _bill_drift(ctx: CloseContext) -> tuple[list[str], int]:
    """Engine-pushed bills vs the accounting system; returns (problems,
    open_pushed_balance_cents) so the aging comparison reuses the fetch."""
    rows = ctx.ledger.conn.execute(
        "SELECT * FROM ap_invoices WHERE tenant=? AND qbo_bill_id IS NOT NULL "
        "AND qbo_bill_id != ''",
        (ctx.tenant_slug,),
    ).fetchall()
    if not rows:
        return [], 0
    held = ctx.qbo.fetch_bills_with_balance([str(r["qbo_bill_id"]) for r in rows])
    problems: list[str] = []
    open_balance = 0
    for row in rows:
        label = f"#{row['id']} {row['vendor']} / {row['invoice_number']}"
        bill = held.get(str(row["qbo_bill_id"]))
        if bill is None:
            problems.append(f"{label}: bill {row['qbo_bill_id']} no longer exists in QBO")
            continue
        if bill["total_cents"] != row["amount_cents"]:
            problems.append(
                f"{label}: bill total {_cents_str(bill['total_cents'])} vs ledger "
                f"{_cents_str(row['amount_cents'])}"
            )
        if is_settled(str(row["status"])):
            if bill["balance_cents"] != 0:
                problems.append(
                    f"{label}: ledger says settled but the bill still carries "
                    f"{_cents_str(bill['balance_cents'])} open in QBO"
                )
        else:
            open_balance += bill["balance_cents"]
            if bill["balance_cents"] != row["amount_cents"]:
                problems.append(
                    f"{label}: ledger says open ({row['status']}) but the bill balance is "
                    f"{_cents_str(bill['balance_cents'])} of {_cents_str(row['amount_cents'])}"
                )
    return problems, open_balance


def _aged_payables_total_cents(ctx: CloseContext) -> int | None:
    """The AP aging grand total at month-end, from the report's TOTAL row."""
    from ...adapters.qbo import report_rows

    _, month_end = month_bounds(ctx.month)
    report = ctx.qbo.fetch_report("AgedPayables", {"report_date": month_end})
    total: int | None = None
    for row in report_rows(report):
        cells = row["cells"]
        if not cells:
            continue
        if row["kind"] == "summary" or cells[0].strip().upper().startswith("TOTAL"):
            for cell in reversed(cells):
                cents = _dollars_to_cents(cell)
                if cents is not None:
                    total = cents
                    break
    return total


def check_ap_tie_out(ctx: CloseContext) -> CheckResult:
    """The ledger, the delivered bills, and the AP aging tell one story."""
    name = "ap-tie-out"
    problems = _paid_without_evidence(ctx)
    drift, open_pushed = _bill_drift(ctx)
    problems.extend(drift)
    if problems:
        return CheckResult(
            name=name,
            status="BLOCK",
            summary=f"{len(problems)} AP item(s) do not tie out",
            details=_capped(problems),
        )
    aging = _aged_payables_total_cents(ctx)
    if aging is not None and aging != open_pushed:
        return CheckResult(
            name=name,
            status="WARN",
            summary=f"QBO AP aging shows {_cents_str(aging)} open but engine-pushed open "
            f"bills sum to {_cents_str(open_pushed)}; something in QBO's payables the "
            "ledger does not know (or vice versa)",
        )
    return CheckResult(
        name=name,
        status="OK",
        summary=f"month's Paid rows all evidenced; pushed bills coherent; AP aging ties at "
        f"{_cents_str(open_pushed)}",
    )


# ---- checks 2 + 3: the statement anchor and feed acceptance ----------------


def _register_balance_cents(ctx: CloseContext) -> int | None:
    """The bank account's book balance as of month-end, from the balance
    sheet report. None when the account row cannot be found."""
    from ...adapters.qbo import report_rows

    start, month_end = month_bounds(ctx.month)
    report = ctx.qbo.fetch_report("BalanceSheet", {"start_date": start, "end_date": month_end})
    needle = ctx.bank_account.strip().lower()
    if not needle:
        return None
    for row in report_rows(report):
        cells = row["cells"]
        if not cells or needle not in cells[0].strip().lower():
            continue
        for cell in reversed(cells[1:]):
            cents = _dollars_to_cents(cell)
            if cents is not None:
                return cents
    return None


def _committed_not_cleared(ctx: CloseContext) -> list[str]:
    """Checks written and committed in the ledger that no cleared view shows
    yet. CONTEXT for the anchor, not a reconciling term: in a feed-driven
    book a mailed check enters QBO only when it clears, so the register and
    the statement are both cleared-only views (design correction
    2026-07-21)."""
    rows = ctx.ledger.conn.execute(
        "SELECT * FROM ap_invoices WHERE tenant=? AND status IN "
        "('Scheduled', 'Scheduled in bill pay') AND check_ref != '' ORDER BY id",
        (ctx.tenant_slug,),
    ).fetchall()
    return [
        f"#{r['id']} {r['vendor']} / {r['invoice_number']}: "
        f"{_cents_str(r['amount_cents'])} (check {r['check_ref']}) committed, not yet cleared"
        for r in rows
    ]


def check_statement_anchor(ctx: CloseContext) -> CheckResult:
    """The statement's ending balance equals the register at month-end."""
    name = "statement-anchor"
    if ctx.statement_balance_cents is None:
        return CheckResult(
            name=name,
            status="WARN",
            summary="statement balance not provided; pass --statement-balance from "
            "QBO's statement tab at close time (the anchor is unverified until then)",
        )
    register = _register_balance_cents(ctx)
    if register is None:
        return CheckResult(
            name=name,
            status="BLOCK",
            summary=f"could not find account {ctx.bank_account!r} on the balance sheet "
            "at month-end; check [close].bank_account against the chart of accounts",
        )
    outstanding = _committed_not_cleared(ctx)
    if register == ctx.statement_balance_cents:
        return CheckResult(
            name=name,
            status="OK",
            summary=f"statement and register both read {_cents_str(register)} at month-end"
            + (
                f"; {len(outstanding)} committed check(s) still travelling (context below)"
                if outstanding
                else ""
            ),
            details=_capped(outstanding),
        )
    delta = ctx.statement_balance_cents - register
    return CheckResult(
        name=name,
        status="BLOCK",
        summary=f"statement reads {_cents_str(ctx.statement_balance_cents)} but the "
        f"register reads {_cents_str(register)} at month-end (delta {_cents_str(delta)}); "
        "the register must explain the statement to the cent",
        details=_capped(outstanding),
    )


def check_feed_acceptance(ctx: CloseContext, anchor: CheckResult) -> CheckResult:
    """No feed line for the month still sits unaccepted. The For-Review queue
    has no API, so this is inferred from the anchor: an unexplained delta IS
    unaccepted (or misbooked) lines in aggregate; the close-time look at the
    Banking tab confirms zero directly."""
    name = "feed-acceptance"
    if anchor.status == "WARN":
        return CheckResult(
            name=name,
            status="WARN",
            summary="inferred from the statement anchor, which is unverified until "
            "--statement-balance is provided",
        )
    if anchor.status == "BLOCK":
        return CheckResult(
            name=name,
            status="BLOCK",
            summary="the anchor's unexplained delta is unaccepted or misbooked feed "
            "lines in aggregate; open the Banking tab and accept the month before "
            "re-running",
        )
    return CheckResult(
        name=name,
        status="OK",
        summary="the register explains the statement to the cent; confirm the Banking "
        "tab shows 0 to review at sign-off",
    )


# ---- check 7: categorization sweep -----------------------------------------


def _column_index(report: dict, title: str) -> int | None:
    """Index of the report column titled ``title``, from Columns metadata."""
    cols = (report.get("Columns") or {}).get("Column", [])
    for i, col in enumerate(cols):
        if str(col.get("ColTitle", "")).strip().lower() == title.lower():
            return i
    return None


def check_categorization(ctx: CloseContext) -> CheckResult:
    """No month activity sits on Uncategorized or Ask My Accountant."""
    from ...adapters.qbo import report_rows

    name = "categorization"
    start, end = month_bounds(ctx.month)
    report = ctx.qbo.fetch_report("GeneralLedger", {"start_date": start, "end_date": end})
    # QBO's GL data rows END with the running Balance column; Amount sits
    # before it (captured live 2026-08-24). Resolve the Amount cell by the
    # report's own column metadata — a last-numeric-cell scan reads the
    # running balance, misstating the offender total and able to cross the
    # BLOCK threshold on a tiny offender (#136).
    amount_idx = _column_index(report, "Amount")
    offenders: list[str] = []
    total = 0
    for row in report_rows(report):
        if row["kind"] != "data":
            continue
        section = " / ".join(row["path"]).lower()
        if not any(marker in section for marker in UNCATEGORIZED_MARKERS):
            continue
        cents = None
        if amount_idx is not None and amount_idx < len(row["cells"]):
            cents = _dollars_to_cents(row["cells"][amount_idx])
        else:
            # No column metadata (older captures): last numeric cell, the
            # pre-#136 behavior.
            for cell in reversed(row["cells"]):
                cents = _dollars_to_cents(cell)
                if cents is not None:
                    break
        total += abs(cents or 0)
        label = " ".join(c for c in row["cells"][:4] if c).strip() or "(unlabelled line)"
        offenders.append(f"{label}: {_cents_str(cents or 0)} in {' / '.join(row['path'])}")
    if not offenders:
        return CheckResult(name=name, status="OK", summary="no uncategorized activity in the month")
    status = "BLOCK" if total > ctx.uncategorized_block_over_cents else "WARN"
    return CheckResult(
        name=name,
        status=status,
        summary=f"{len(offenders)} uncategorized line(s) totalling {_cents_str(total)}"
        + (
            f" (over the {_cents_str(ctx.uncategorized_block_over_cents)} block threshold)"
            if status == "BLOCK"
            else ""
        ),
        details=_capped(offenders),
    )


# ---- check 5: payroll landed -----------------------------------------------


def check_payroll(ctx: CloseContext) -> CheckResult:
    """Both semi-monthly payroll journal entries exist in the books. The
    payroll processor's own QBO sync writes them with its name in DocNumber
    and the pay period in the note (grounded against the live book
    2026-07-21); detection is by tenant-configured markers, never guessed —
    the processor's name is tenant vocabulary, not core's."""
    name = "payroll"
    start, end = month_bounds(ctx.month)
    markers = [m.lower() for m in (ctx.payroll_markers or ["payroll"])]
    entries = ctx.qbo.fetch_journal_entries(start=start, end=end)
    payroll = [
        je
        for je in entries
        if any(
            m in f"{je.get('DocNumber', '')} {je.get('PrivateNote', '')}".lower() for m in markers
        )
    ]
    details = [
        f"{je.get('TxnDate', '?')}: {je.get('PrivateNote') or je.get('DocNumber') or 'entry'}"
        for je in payroll
    ]
    if not payroll:
        return CheckResult(
            name=name,
            status="BLOCK",
            summary="no payroll journal entries in the books for the month; the payroll "
            "sync either has not landed or was never matched",
        )
    if len(payroll) < 2:
        return CheckResult(
            name=name,
            status="WARN",
            summary=f"only {len(payroll)} payroll entry in the month "
            f"({details[0]}); semi-monthly expects two (fine mid-month, not at close)",
        )
    return CheckResult(
        name=name,
        status="OK",
        summary=f"{len(payroll)} payroll entries landed; confirm the set below is complete",
        details=details,
    )


# ---- check 6: owner transactions -------------------------------------------


def check_owner_transactions(ctx: CloseContext) -> CheckResult:
    """Every owner-shaped transaction in the month, enumerated with its
    coding. Owner-shaped: payee empty or matching a configured owner name
    (the live pattern: owner loan repayments post with no payee). The
    treatment question stays the owner's; this check makes the list."""
    name = "owner-transactions"
    start, end = month_bounds(ctx.month)
    owners = [n.lower() for n in (ctx.owner_names or [])]
    lines: list[str] = []
    miscoded = 0
    for txn in ctx.qbo.fetch_purchases(start=start, end=end):
        payee = str(((txn.get("EntityRef") or {}).get("name", "")) or "")
        owner_shaped = not payee or any(o in payee.lower() for o in owners)
        if not owner_shaped:
            continue
        accounts = []
        for line in txn.get("Line", []):
            detail = line.get("AccountBasedExpenseLineDetail") or {}
            account = str((detail.get("AccountRef") or {}).get("name", ""))
            if account:
                accounts.append(account)
        coded = ", ".join(sorted(set(accounts))) or "(no account)"
        if any(m in coded.lower() for m in UNCATEGORIZED_MARKERS) or coded == "(no account)":
            miscoded += 1
        amount = _dollars_to_cents(str(txn.get("TotalAmt", ""))) or 0
        lines.append(
            f"{txn.get('TxnDate', '?')} {payee or '(no payee)'} {_cents_str(amount)} -> {coded}"
        )
    if not lines:
        return CheckResult(
            name=name, status="OK", summary="no owner-shaped transactions in the month"
        )
    if miscoded:
        return CheckResult(
            name=name,
            status="WARN",
            summary=f"{len(lines)} owner-shaped transaction(s), {miscoded} without a real "
            "coding decision",
            details=_capped(lines),
        )
    return CheckResult(
        name=name,
        status="OK",
        summary=f"{len(lines)} owner-shaped transaction(s), all coded; confirm the "
        "treatment below reads consistently",
        details=_capped(lines),
    )


# ---- check 8: expenses folder ----------------------------------------------

# The single shared receipt-suffix definition lives with the expenses agent
# (docs/expenses-design.md §1); the alias keeps this module's existing name.
from ..expenses.schema import RECEIPT_SUFFIXES as _RECEIPT_SUFFIXES  # noqa: E402


def check_expenses(ctx: CloseContext) -> CheckResult:
    """Loose receipts for the month that no expense report has consumed.
    A filing reminder, never a close-blocker (the legacy close script's rule, kept).

    Consumed/filed paper does not count (May precedent, ratified in the
    the expenses agent design): `_filed/<report-id>/` holds receipts a report consumed;
    `_cc_charges/` and `_reference/` hold filed-not-loose paper. The expenses agent's
    manifest later teaches this check the report-side meaning of consumed;
    until then the folder convention is the truth."""
    name = "expenses"
    if not ctx.expenses_dir:
        return CheckResult(name=name, status="OK", summary="no expenses dir configured")
    month_dir = Path(ctx.expenses_dir).expanduser() / ctx.month
    if not month_dir.is_dir():
        return CheckResult(name=name, status="OK", summary=f"no receipts folder for {ctx.month}")
    consumed_dirs = {"_filed", "_cc_charges", "_reference"}
    loose = []
    consumed = 0
    for p in sorted(month_dir.rglob("*")):
        if not (p.is_file() and p.suffix.lower() in _RECEIPT_SUFFIXES):
            continue
        if p.relative_to(month_dir).parts[0] in consumed_dirs:
            consumed += 1
        else:
            loose.append(p.name)
    if not loose and not consumed:
        return CheckResult(
            name=name, status="OK", summary=f"receipts folder for {ctx.month} is empty"
        )
    if not loose:
        return CheckResult(
            name=name,
            status="OK",
            summary=f"all {consumed} receipt(s) for {ctx.month} consumed or filed",
        )
    return CheckResult(
        name=name,
        status="WARN",
        summary=f"{len(loose)} receipt(s) sitting in {month_dir.name}/ with no expense "
        "report; file them before sign-off",
        details=_capped(loose),
    )


# ---- check 9: AR snapshot --------------------------------------------------


def _register_month_rows(ctx: CloseContext) -> tuple[list[dict], str]:
    """Issued-invoice register rows for the month. Header row 4 of the
    'Issued Invoices' sheet (grounded 2026-07-21); returns (rows, error)."""
    from openpyxl import load_workbook

    path = Path(ctx.invoice_register_xlsx).expanduser()
    if not path.exists():
        return [], f"register not found at {path}"
    try:
        wb = load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:
        return [], f"register unreadable: {exc}"
    try:
        if "Issued Invoices" not in wb.sheetnames:
            return [], "register has no 'Issued Invoices' sheet"
        ws = wb["Issued Invoices"]
        rows_iter = ws.iter_rows(min_row=4, values_only=True)
        headers = [str(h or "").strip() for h in next(rows_iter, [])]
        rows: list[dict] = []
        for values in rows_iter:
            row = {h: v for h, v in zip(headers, values, strict=False) if h}
            issue = str(row.get("Issue Date", "") or "")
            if issue.startswith(ctx.month):
                rows.append(row)
        return rows, ""
    finally:
        wb.close()


def _aged_receivables_total_cents(ctx: CloseContext) -> int | None:
    from ...adapters.qbo import report_rows

    _, month_end = month_bounds(ctx.month)
    report = ctx.qbo.fetch_report("AgedReceivables", {"report_date": month_end})
    total: int | None = None
    for row in report_rows(report):
        cells = row["cells"]
        if not cells:
            continue
        if row["kind"] == "summary" or cells[0].strip().upper().startswith("TOTAL"):
            for cell in reversed(cells):
                cents = _dollars_to_cents(cell)
                if cents is not None:
                    total = cents
                    break
    return total


def check_ar_snapshot(ctx: CloseContext) -> CheckResult:
    """The issue side at a glance: month's register rows paired to PDFs on
    disk, plus the AR aging total. WARN-class in v1 — the AR vertical is a
    later phase; the close just refuses to look away from it."""
    name = "ar-snapshot"
    if not ctx.invoice_register_xlsx:
        return CheckResult(name=name, status="OK", summary="no invoice register configured")
    rows, error = _register_month_rows(ctx)
    if error:
        return CheckResult(name=name, status="WARN", summary=error)
    missing = []
    for row in rows:
        file_path = str(row.get("File Path", "") or "")
        if file_path.startswith("/") and not Path(file_path).exists():
            missing.append(f"{row.get('Invoice #', '?')}: {file_path}")
    aging = _aged_receivables_total_cents(ctx)
    aging_note = f"; AR aging at month-end {_cents_str(aging)}" if aging is not None else ""
    if missing:
        return CheckResult(
            name=name,
            status="WARN",
            summary=f"{len(missing)} register row(s) this month point at missing PDFs{aging_note}",
            details=_capped(missing),
        )
    return CheckResult(
        name=name,
        status="OK",
        summary=f"{len(rows)} issued invoice(s) this month, all paired to PDFs{aging_note}",
    )


# ---- placeholders for later build steps ------------------------------------


def todo(name: str, step: str) -> CheckResult:
    return CheckResult(name=name, status="TODO", summary=f"lands in build {step}")
