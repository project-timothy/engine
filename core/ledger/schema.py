"""SQLite schema for the ledger, expressed as ordered migrations.

The ledger is truth (engine invariant 1). Phase 1 keeps the schema minimal:
``runs``, ``events``, ``approval_queue``, plus the ``schema_migrations``
bookkeeping table. Later phases append migrations to this list; they never
edit an already-applied migration.

Every business-meaningful table carries an ``idempotency_key UNIQUE`` column.
That column is how invariant 3 ("every ledger write carries an idempotency
key; every job is safe to re-run") is enforced at the storage layer rather
than trusted to caller discipline.
"""

from __future__ import annotations

# Each migration is (version, [sql_statements]). Applied in ascending version
# order; a version already recorded in schema_migrations is skipped. Append
# new migrations; do not mutate shipped ones.
MIGRATIONS: list[tuple[int, list[str]]] = [
    (
        1,
        [
            """
            CREATE TABLE IF NOT EXISTS runs (
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
            CREATE TABLE IF NOT EXISTS events (
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
            CREATE TABLE IF NOT EXISTS approval_queue (
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
            "CREATE INDEX IF NOT EXISTS idx_runs_tenant ON runs(tenant, agent, job)",
            "CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id)",
            "CREATE INDEX IF NOT EXISTS idx_approval_status ON approval_queue(tenant, status)",
        ],
    ),
    (
        2,
        # AP vertical slice (Phase 2). Money is INTEGER cents, never float.
        # `shadow` marks rows written by shadow-mode runs. It is PROVENANCE, not
        # a visibility gate: AP read consumers see the whole book regardless of
        # the tag (a 2026-07-01 decision). The shadow-mode safety boundary is
        # the write guard (external writes are refused in shadow); the isolation
        # safety test still asserts a shadow run tags its rows shadow=1.
        # ap_status_history is the engine's AuditTrail analog: one row per
        # status flip, joined on invoice_id, append-only.
        [
            """
            CREATE TABLE IF NOT EXISTS ap_invoices (
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
                updated_at       TEXT    NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS ap_status_history (
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
            "CREATE INDEX IF NOT EXISTS idx_ap_invoices_tenant "
            "ON ap_invoices(tenant, vendor, invoice_number)",
            "CREATE INDEX IF NOT EXISTS idx_ap_invoices_status ON ap_invoices(tenant, status)",
            "CREATE INDEX IF NOT EXISTS idx_ap_history_invoice ON ap_status_history(invoice_id)",
        ],
    ),
    (
        3,
        # QBO write side W1 (docs/qbo-write-side-design.md, approved
        # 2026-07-17): ids of engine-created accounting records live on the
        # row, giving duplicate-proof re-runs and letting the read side
        # filter out engine-authored records as clearing evidence.
        [
            "ALTER TABLE ap_invoices ADD COLUMN qbo_bill_id TEXT",
            "ALTER TABLE ap_invoices ADD COLUMN qbo_payment_id TEXT",
        ],
    ),
    (
        4,
        # Expense vertical (docs/expenses-design.md §3; the flagged
        # one-way door, owner-approved 2026-08-04). Purely additive: an
        # expense report is a header row plus its receipt-backed lines.
        # Status walks Open -> Reimbursed-Recorded (engine wrote the QBO
        # split record) -> Reimbursed (bank-CSV clearing evidence, W2
        # Option B). Money is INTEGER cents, never float.
        [
            """
            CREATE TABLE IF NOT EXISTS expense_report (
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
                updated_at          TEXT    NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS expense_line (
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
            "CREATE INDEX IF NOT EXISTS idx_expense_report_status "
            "ON expense_report(tenant, status)",
            "CREATE INDEX IF NOT EXISTS idx_expense_report_person "
            "ON expense_report(tenant, person, month)",
            "CREATE INDEX IF NOT EXISTS idx_expense_line_report ON expense_line(report_id)",
        ],
    ),
    (
        5,
        # Issue #115 gap 2 (flagged one-way door, in the PR for owner
        # review): the reimbursement's actual instrument (a check number)
        # had nowhere to live — the Purchase DocNumber carries the person's
        # requested memo by design, so the bank-feed row had nothing tying
        # it to the report. Purely additive; existing rows read ''.
        [
            "ALTER TABLE expense_report ADD COLUMN instrument_ref TEXT NOT NULL DEFAULT ''",
        ],
    ),
    (
        6,
        # Job-time durable records (honesty audit #170, finding 03-F9; owner
        # go 2026-09-10). Events persist only after a job returns, so a job
        # that moves a file and then dies leaves the move with no record.
        # A job record is written and committed THE MOMENT the job calls
        # for it (record-then-move); the runner links it to the run row
        # when the run lands (success or FAILED), so ``run_id`` is NULL
        # only for a run that never landed at all. Purely additive; flagged
        # as a one-way door in the PR (a new ledger table).
        [
            """
            CREATE TABLE IF NOT EXISTS job_records (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                idempotency_key  TEXT    NOT NULL UNIQUE,
                run_key          TEXT    NOT NULL,
                run_id           INTEGER REFERENCES runs(id),
                tenant           TEXT    NOT NULL,
                agent            TEXT    NOT NULL,
                job              TEXT    NOT NULL,
                record_type      TEXT    NOT NULL,
                payload_json     TEXT    NOT NULL,
                created_at       TEXT    NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_job_records_run_key ON job_records(run_key)",
            "CREATE INDEX IF NOT EXISTS idx_job_records_type ON job_records(tenant, record_type)",
        ],
    ),
    (
        7,
        # Model-call telemetry (phase 7 row 7.9, docs/model-seam-design.md;
        # decision docs/decisions/2026-09-12-llm-policy-table-shape.md). One
        # row per gateway call made through the tenant policy: which job
        # asked, under which run key, which tier and model answered, the
        # tokens, the price (a decimal STRING, never a float), the
        # validation retries, and how it ended (`status`: ok,
        # validation_failed, transport_failed, or budget_refused, the last
        # with no call made). The monthly budget reads the tenant's rows for
        # the current month; the runner sums a run's rows onto its result.
        # An observation, not a business write: no idempotency key (each
        # call is its own event). Purely additive; flagged as a one-way door
        # in the PR (a new ledger table).
        [
            """
            CREATE TABLE IF NOT EXISTS llm_calls (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant           TEXT    NOT NULL,
                job_type         TEXT    NOT NULL,
                run_key          TEXT    NOT NULL DEFAULT '',
                tier             TEXT    NOT NULL DEFAULT '',
                adapter          TEXT    NOT NULL,
                model            TEXT    NOT NULL,
                provider_model   TEXT,
                tokens_in        INTEGER NOT NULL DEFAULT 0,
                tokens_out       INTEGER NOT NULL DEFAULT 0,
                usd              TEXT    NOT NULL DEFAULT '0',
                retries          INTEGER NOT NULL DEFAULT 0,
                latency_ms       INTEGER NOT NULL DEFAULT 0,
                status           TEXT    NOT NULL DEFAULT 'ok',
                detail           TEXT    NOT NULL DEFAULT '',
                created_at       TEXT    NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_llm_calls_run_key ON llm_calls(run_key)",
            "CREATE INDEX IF NOT EXISTS idx_llm_calls_month ON llm_calls(tenant, created_at)",
        ],
    ),
    (
        8,
        # Cross-run retries (phase 7 row 7.23, issue #232; decision
        # docs/decisions/2026-09-12-retry-policy-on-the-handler.md). A job
        # may declare a bounded retry policy; the runner records every
        # attempt of such a job as a job record of type ``engine.retry``:
        # the policy that governs it (json), the attempt number, when the
        # next attempt is due (ISO-8601 UTC, NULL when none), a link to the
        # previous attempt's record, and the state (scheduled, resumed,
        # succeeded, exhausted). ``engine jobs resume`` executes the
        # scheduled records whose time has come. A plain job record
        # (``ctx.record_now``) reads NULL / 0 / '' here and means what it
        # always did. Purely additive; flagged as a one-way door in the PR
        # (a ledger schema change).
        [
            "ALTER TABLE job_records ADD COLUMN retry_policy TEXT",
            "ALTER TABLE job_records ADD COLUMN attempt INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE job_records ADD COLUMN next_attempt_at TEXT",
            "ALTER TABLE job_records ADD COLUMN parent_record_id INTEGER "
            "REFERENCES job_records(id)",
            "ALTER TABLE job_records ADD COLUMN retry_state TEXT NOT NULL DEFAULT ''",
            "CREATE INDEX IF NOT EXISTS idx_job_records_retry "
            "ON job_records(tenant, retry_state, next_attempt_at)",
        ],
    ),
]
