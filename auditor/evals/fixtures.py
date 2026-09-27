"""Core-free ledger fixtures for lens evals.

The independence lint forbids importing core anywhere under ``auditor/``, so
these fixtures build engine-shaped ledgers with raw SQL. The DDL below is a
deliberate copy of the engine's storage contract; the parity eval in
``tests/unit/test_auditor_fixture_schema_parity.py`` (engine-side, where
importing core is allowed) fails the suite if the real schema drifts away
from this copy — a conscious tripwire, not an accident waiting to happen.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from ..config import AuditorTenantConfig
from ..ledger_reader import LedgerReader
from ..lenses import AuditContext

# Mirrors core/ledger/schema.py migrations 1-3, final shape.
LEDGER_DDL = [
    """
    CREATE TABLE runs (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        idempotency_key  TEXT    NOT NULL UNIQUE,
        tenant           TEXT    NOT NULL,
        agent            TEXT    NOT NULL,
        job              TEXT    NOT NULL,
        status           TEXT    NOT NULL,
        shadow           INTEGER NOT NULL DEFAULT 0,
        result_json      TEXT    NOT NULL,
        summary          TEXT    NOT NULL DEFAULT '',
        created_at       TEXT    NOT NULL
    )
    """,
    """
    CREATE TABLE events (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        idempotency_key  TEXT    NOT NULL UNIQUE,
        run_id           INTEGER NOT NULL REFERENCES runs(id),
        tenant           TEXT    NOT NULL,
        agent            TEXT    NOT NULL,
        event_type       TEXT    NOT NULL,
        payload_json     TEXT    NOT NULL,
        created_at       TEXT    NOT NULL
    )
    """,
    """
    CREATE TABLE approval_queue (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        idempotency_key  TEXT    NOT NULL UNIQUE,
        run_id           INTEGER NOT NULL REFERENCES runs(id),
        tenant           TEXT    NOT NULL,
        agent            TEXT    NOT NULL,
        action_type      TEXT    NOT NULL,
        params_json      TEXT    NOT NULL,
        status           TEXT    NOT NULL DEFAULT 'pending',
        created_at       TEXT    NOT NULL,
        resolved_at      TEXT
    )
    """,
    """
    CREATE TABLE ap_invoices (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        idempotency_key  TEXT    NOT NULL UNIQUE,
        tenant           TEXT    NOT NULL,
        vendor           TEXT    NOT NULL,
        invoice_number   TEXT    NOT NULL DEFAULT '',
        invoice_date     TEXT    NOT NULL DEFAULT '',
        due_date         TEXT,
        amount_cents     INTEGER NOT NULL,
        gl_account       TEXT    NOT NULL DEFAULT '',
        cost_type        TEXT    NOT NULL DEFAULT '',
        project          TEXT    NOT NULL DEFAULT '',
        status           TEXT    NOT NULL DEFAULT 'Received',
        payment_date     TEXT,
        check_ref        TEXT    NOT NULL DEFAULT '',
        notes            TEXT    NOT NULL DEFAULT '',
        source_file      TEXT    NOT NULL DEFAULT '',
        source_md5       TEXT    NOT NULL DEFAULT '',
        confidence       REAL,
        shadow           INTEGER NOT NULL DEFAULT 0,
        created_at       TEXT    NOT NULL,
        updated_at       TEXT    NOT NULL,
        qbo_bill_id      TEXT,
        qbo_payment_id   TEXT
    )
    """,
    """
    CREATE TABLE ap_status_history (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        idempotency_key  TEXT    NOT NULL UNIQUE,
        invoice_id       INTEGER NOT NULL REFERENCES ap_invoices(id),
        status_from      TEXT    NOT NULL,
        status_to        TEXT    NOT NULL,
        actor            TEXT    NOT NULL DEFAULT '',
        note             TEXT    NOT NULL DEFAULT '',
        created_at       TEXT    NOT NULL
    )
    """,
    # Migration 4 (the expenses agent) + migration 5's appended instrument_ref. The
    # year-end lens counts vendor-role expense payments from this table
    # (issue #120); expense_line stays engine-only, no lens touches it.
    """
    CREATE TABLE expense_report (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        idempotency_key     TEXT    NOT NULL UNIQUE,
        tenant              TEXT    NOT NULL,
        person              TEXT    NOT NULL,
        month               TEXT    NOT NULL,
        total_cents         INTEGER NOT NULL,
        status              TEXT    NOT NULL DEFAULT 'Open',
        report_path         TEXT    NOT NULL DEFAULT '',
        manifest_path       TEXT    NOT NULL DEFAULT '',
        qbo_purchase_id     TEXT,
        reimbursed_channel  TEXT    NOT NULL DEFAULT '',
        reimbursed_date     TEXT,
        cleared_date        TEXT,
        shadow              INTEGER NOT NULL DEFAULT 0,
        created_at          TEXT    NOT NULL,
        updated_at          TEXT    NOT NULL,
        instrument_ref      TEXT    NOT NULL DEFAULT ''
    )
    """,
    # Migration 4's line table: the projects lens (2026-09-04) reads its
    # ``project`` column; nothing else touches it.
    """
    CREATE TABLE expense_line (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        idempotency_key     TEXT    NOT NULL UNIQUE,
        report_id           INTEGER NOT NULL REFERENCES expense_report(id),
        tenant              TEXT    NOT NULL,
        receipt_file        TEXT    NOT NULL DEFAULT '',
        receipt_sha256      TEXT    NOT NULL DEFAULT '',
        vendor              TEXT    NOT NULL DEFAULT '',
        expense_date        TEXT    NOT NULL DEFAULT '',
        amount_cents        INTEGER NOT NULL,
        category            TEXT    NOT NULL DEFAULT '',
        project             TEXT    NOT NULL DEFAULT '',
        note                TEXT    NOT NULL DEFAULT '',
        created_at          TEXT    NOT NULL
    )
    """,
]

NOW = "2026-07-21T06:00:00+00:00"
_counter = {"n": 0}


def _key(prefix: str) -> str:
    _counter["n"] += 1
    return f"{prefix}:{_counter['n']}"


def make_ledger(root: Path) -> sqlite3.Connection:
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(root / "ledger.sqlite3")
    conn.row_factory = sqlite3.Row
    for statement in LEDGER_DDL:
        conn.execute(statement)
    conn.commit()
    return conn


def add_run(
    conn,
    *,
    tenant="t",
    agent="ap",
    job="intake",
    status="ok",
    shadow=0,
    created_at=NOW,
) -> int:
    cursor = conn.execute(
        "INSERT INTO runs (idempotency_key, tenant, agent, job, status, shadow, result_json, "
        "created_at) VALUES (?,?,?,?,?,?,?,?)",
        (_key("run"), tenant, agent, job, status, shadow, "{}", created_at),
    )
    conn.commit()
    return int(cursor.lastrowid)


def add_event(conn, *, tenant="t", event_type="x", payload=None, created_at=NOW, run_id=None):
    if run_id is None:
        run_id = add_run(conn, tenant=tenant, created_at=created_at)
    conn.execute(
        "INSERT INTO events (idempotency_key, run_id, tenant, agent, event_type, payload_json, "
        "created_at) VALUES (?,?,?,?,?,?,?)",
        (_key("evt"), run_id, tenant, "ap", event_type, json.dumps(payload or {}), created_at),
    )
    conn.commit()


def add_invoice(
    conn,
    *,
    tenant="t",
    vendor="Vendor",
    invoice_number="INV-1",
    amount_cents=10000,
    status="Received",
    payment_date=None,
    check_ref="",
    notes="",
    qbo_bill_id=None,
    created_at=NOW,
    cost_type="",
    invoice_date="",
    gl_account="",
    project="",
) -> int:
    cursor = conn.execute(
        "INSERT INTO ap_invoices (idempotency_key, tenant, vendor, invoice_number, amount_cents, "
        "status, payment_date, check_ref, notes, qbo_bill_id, created_at, updated_at, cost_type, "
        "invoice_date, gl_account, project) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            _key("inv"),
            tenant,
            vendor,
            invoice_number,
            amount_cents,
            status,
            payment_date,
            check_ref,
            notes,
            qbo_bill_id,
            created_at,
            created_at,
            cost_type,
            invoice_date or "",
            gl_account,
            project,
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def add_expense_report(
    conn,
    *,
    tenant="t",
    person="Person",
    month="2026-07",
    total_cents=10000,
    status="Open",
    created_at=NOW,
    instrument_ref=None,
    reimbursed_date=None,
    cleared_date=None,
    qbo_purchase_id=None,
) -> int:
    # The optional instrument/date columns are written only when given, so a
    # fixture that deliberately drifts the table (04-F7 eval) still inserts.
    columns = ["idempotency_key", "tenant", "person", "month", "total_cents", "status"]
    values: list = [_key("exp"), tenant, person, month, total_cents, status]
    columns += ["created_at", "updated_at"]
    values += [created_at, created_at]
    for column, value in (
        ("instrument_ref", instrument_ref),
        ("reimbursed_date", reimbursed_date),
        ("cleared_date", cleared_date),
        ("qbo_purchase_id", qbo_purchase_id),
    ):
        if value is not None:
            columns.append(column)
            values.append(value)
    cursor = conn.execute(
        f"INSERT INTO expense_report ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)})",
        values,
    )
    conn.commit()
    return int(cursor.lastrowid)


def add_expense_line(
    conn,
    report_id,
    *,
    tenant="t",
    vendor="Shop",
    amount_cents=1000,
    category="Supplies",
    project="",
    created_at=NOW,
) -> int:
    cursor = conn.execute(
        "INSERT INTO expense_line (idempotency_key, report_id, tenant, vendor, amount_cents, "
        "category, project, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (_key("line"), report_id, tenant, vendor, amount_cents, category, project, created_at),
    )
    conn.commit()
    return int(cursor.lastrowid)


def add_history(conn, invoice_id, status_from, status_to, *, actor="", note="", created_at=NOW):
    conn.execute(
        "INSERT INTO ap_status_history (idempotency_key, invoice_id, status_from, status_to, "
        "actor, note, created_at) VALUES (?,?,?,?,?,?,?)",
        (_key("hist"), invoice_id, status_from, status_to, actor, note, created_at),
    )
    conn.commit()


def add_approval(
    conn,
    *,
    tenant="t",
    action_type="ap.qbo_push_batch",
    params=None,
    status="pending",
    created_at=NOW,
    resolved_at=None,
) -> int:
    run_id = add_run(conn, tenant=tenant, created_at=created_at)
    cursor = conn.execute(
        "INSERT INTO approval_queue (idempotency_key, run_id, tenant, agent, action_type, "
        "params_json, status, created_at, resolved_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            _key("appr"),
            run_id,
            tenant,
            "ap",
            action_type,
            json.dumps(params or {}),
            status,
            created_at,
            resolved_at,
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


# ---- git fixtures for the backup heartbeat ---------------------------------


def _git(root: Path, *args: str, date: str | None = None) -> str:
    env = {
        "GIT_AUTHOR_NAME": "fixture",
        "GIT_AUTHOR_EMAIL": "fixture@test",
        "GIT_COMMITTER_NAME": "fixture",
        "GIT_COMMITTER_EMAIL": "fixture@test",
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
    }
    if date:
        env["GIT_AUTHOR_DATE"] = date
        env["GIT_COMMITTER_DATE"] = date
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return result.stdout.strip()


def init_ledger_repo(root: Path, *, commit_date: str = NOW) -> None:
    """A ledger git repo with one commit, fully 'pushed' (origin ref at HEAD)."""
    _git(root, "init", "-q", "-b", "main")
    (root / ".gitkeep").write_text("")
    _git(root, "add", "-A")
    commit(root, date=commit_date)
    mark_pushed(root)


def commit(root: Path, *, date: str, message: str = "ledger write") -> None:
    # date sets both author and committer clocks; the unpushed-age check in
    # the heartbeat lens reads the committer date.
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "--allow-empty", "-m", message, date=date)


def mark_pushed(root: Path) -> None:
    """Move the remote-tracking ref to HEAD, as a successful push would."""
    _git(root, "update-ref", "refs/remotes/origin/main", "HEAD")


# ---- lens context ----------------------------------------------------------

_BASE_CONFIG = AuditorTenantConfig(
    slug="t",
    timezone="UTC",
    landing_dir="",
    filing_dir="",
    workbook_path="",
    workbook_columns=[],
    timesheets_filing_dir="",
    qbo_token_file="",
    qbo_since_days=30,
    report_dir="",
    expected_daily_jobs=[],
    daily_run_max_age_hours=25,
    decision_window_days=14,
    filing_grace_days=2,
    stale_approval_days=7,
    backup_max_age_hours=25,
    token_warn_days=3,
    token_critical_days=30,
    coverage_since="",
    context_repo="",
    context_shims=[],
    context_focus_file="",
    context_focus_max_age_days=14,
    context_focus_max_bytes=25_000,
    context_forbidden_paths=[],
    context_unpushed_max_age_hours=48,
    context_monthly_review=False,  # evals opt in explicitly
    host_enabled=False,  # evals opt in explicitly
    host_data_path="/",
    host_disk_warn_free_gb=40,
    host_disk_critical_free_gb=20,
    host_timemachine_max_age_hours=36,
    host_expected_volumes=[],
    host_eviction_watch_paths=[],
    vendor_1099_enabled=False,  # evals opt in explicitly
)


def make_config(**overrides) -> AuditorTenantConfig:
    return replace(_BASE_CONFIG, **overrides)


WORKBOOK_COLUMNS: list[tuple[str, str]] = [
    ("Vendor", "vendor"),
    ("Invoice #", "invoice_number"),
    ("Amount", "amount_cents"),
    ("Status", "status"),
    ("Payment Date", "payment_date"),
    ("Check/Ref #", "check_ref"),
    ("QBO_Bill_ID", ""),
]


def write_workbook(path: Path, rows: list[dict], *, with_furniture: bool = True) -> None:
    """A delivered-sheet mimic for drift evals: header, data rows in the
    delivery contract's cell conventions, and (optionally) the view's own
    furniture — a subtotal line and an unprocessed section — which the book
    lens must read past. Realism against the ENGINE's actual renderer is
    proven separately by tests/unit/test_auditor_book_vs_engine_renderer.py."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "AP Ledger"
    ws.append([label for label, _ in WORKBOOK_COLUMNS])
    total = 0
    for row in rows:
        cells = []
        for _, field in WORKBOOK_COLUMNS:
            if not field:
                cells.append("")
            elif field == "amount_cents":
                cents = row.get("amount_cents")
                cells.append(f"${cents / 100:,.2f}" if cents is not None else "")
            else:
                cells.append(str(row.get(field, "") or ""))
        ws.append(cells)
        total += row.get("amount_cents") or 0
    if with_furniture:
        subtotal = [""] * len(WORKBOOK_COLUMNS)
        subtotal[0] = f"Pending total ({len(rows)})"
        subtotal[2] = f"${total / 100:,.2f}"
        ws.append(subtotal)
        divider = [""] * len(WORKBOOK_COLUMNS)
        divider[0] = "Unprocessed / needs identification (1)"
        ws.append(divider)
        unprocessed = [""] * len(WORKBOOK_COLUMNS)
        unprocessed[0] = "mystery-scan.pdf"
        unprocessed[3] = "needs identification"
        ws.append(unprocessed)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(path))


def make_context(
    ledger_root: Path,
    *,
    store_root: Path | None = None,
    now: str = NOW,
    tenants_dir: Path | None = None,
    **overrides,
) -> AuditContext:
    """Open a reader over a fixture ledger and wrap it for a lens. The caller
    owns the reader's lifetime via the context's ledger attribute."""
    return AuditContext(
        tenant=make_config(**overrides),
        ledger=LedgerReader.open(ledger_root),
        now=datetime.fromisoformat(now),
        store_root=store_root or (ledger_root.parent / ".auditor-store"),
        tenants_dir=tenants_dir,
    )
