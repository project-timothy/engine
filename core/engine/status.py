"""The read-only status page (phase 7 row 7.24, issue #233).

One self-contained HTML file answering the three questions an owner asks
between runs: what ran last and how did it end, what is waiting on me, and
what did the auditor find last night. It renders from what is already on
disk (the ledger's ``runs`` and ``job_records`` tables, the approval queue,
and the newest ``audit-*.md`` in the tenant's auditor report directory) and
it is served with ``tailscale serve`` or opened as a file. Nothing schedules
it and nothing serves it from here: this module renders a string.

**It is a reader.** The ledger is opened through a ``mode=ro`` URI
connection, never :meth:`core.ledger.Ledger.open`, which would create the
git repo and run migrations. So the page cannot write a run, an event, a
record, a card, or a commit, and it renders a ledger whose schema is behind
the code instead of migrating it (the columns it cannot find are simply not
shown). Decision:
``docs/decisions/2026-09-16-the-status-page-is-a-reader.md``.

The audit report is read as a FILE. ``core/`` imports nothing from
``auditor/`` (CI-enforced), so the report directory comes out of the
tenant's own ``[auditor].report_dir`` with this module's own parser, the
way ``engine init`` already reads it.
"""

from __future__ import annotations

import html
import json
import sqlite3
import subprocess
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from string import Template
from typing import Any

from ..ledger.ledger import DB_FILENAME
from ..llm.transcript import env_values, redact_text
from .clock import local_now
from .config import TenantConfig, default_tenants_root

STATUS_DIRNAME = "_status"
"""Where the page lands under the tenant's report tree, beside the auditor's
and the sweep's own folders."""

STATUS_FILENAME = "status.html"

REPORT_GLOB = "audit-*.md"
"""The auditor's only delivery surface is ``audit-<date>.md`` in its report
directory (auditor/report.py). Newest by name, then by mtime."""

NEW_HEADING = "## New"
"""The auditor titles the section "## New since the last report". Matching
the prefix keeps the page honest if the wording after it ever changes."""

RETRY_RECORD = "engine.retry"
"""Row 7.23's retry records live in ``job_records`` under this type. The
name is duplicated rather than imported from the runner: this module reads
a database, it never starts a run."""

MAX_VALUE_CHARS = 60
MAX_SUMMARY_CHARS = 180


class StatusPageError(ValueError):
    """A refusal: nothing could be rendered (and nothing was written)."""


# ---- reading -------------------------------------------------------------


def read_only_connection(ledger_root: str | Path) -> sqlite3.Connection:
    """A SQLite connection that cannot write. ``mode=ro`` refuses every
    mutation at the driver, which is the mechanism behind "no write path":
    a bug in this module raises instead of touching the ledger."""
    db = Path(ledger_root).expanduser() / DB_FILENAME
    if not db.is_file():
        raise StatusPageError(f"no ledger database at {db}")
    conn = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


@dataclass(frozen=True)
class JobRun:
    """The last run of one job, as the ledger recorded it."""

    agent: str
    job: str
    status: str
    shadow: bool
    run_key: str
    landed_utc: str
    summary: str
    retry: str = ""
    records: int = 0

    @property
    def name(self) -> str:
        return f"{self.agent}/{self.job}"

    @property
    def failed(self) -> bool:
        return self.status == "error"


@dataclass(frozen=True)
class Card:
    id: int
    action_type: str
    agent: str
    created_at: str
    summary: str


@dataclass
class CardGroup:
    action_type: str
    cards: list[Card] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.cards)

    @property
    def oldest(self) -> str:
        return min(card.created_at for card in self.cards)


def last_run_per_job(conn: sqlite3.Connection, tenant: str) -> list[JobRun]:
    """One row per job: its newest run, failed ones first, then newest first.

    The run row is what carries a status and a time; ``job_records`` carries
    what the job made durable and, since row 7.23, the retry chain. Both are
    read here and the second is joined onto the first by ``run_id``, because
    a FAILED run's key is ``<run key>:failed:<stamp>`` while the record keeps
    the original key.
    """
    rows = conn.execute(
        """
        SELECT r.id, r.agent, r.job, r.status, r.shadow, r.idempotency_key,
               r.created_at, r.summary
        FROM runs r
        JOIN (
            SELECT agent, job, MAX(id) AS id FROM runs WHERE tenant = ? GROUP BY agent, job
        ) newest ON newest.id = r.id
        """,
        (tenant,),
    ).fetchall()
    overlay = _records_by_run(conn, [int(row["id"]) for row in rows])
    runs = [
        JobRun(
            agent=str(row["agent"]),
            job=str(row["job"]),
            status=str(row["status"]),
            shadow=bool(row["shadow"]),
            run_key=str(row["idempotency_key"]),
            landed_utc=str(row["created_at"]),
            summary=str(row["summary"] or ""),
            retry=overlay.get(int(row["id"]), ("", 0))[0],
            records=overlay.get(int(row["id"]), ("", 0))[1],
        )
        for row in rows
    ]
    runs.sort(key=lambda r: (r.landed_utc, r.name), reverse=True)  # newest first
    runs.sort(key=lambda r: not r.failed)  # stable: the failed ones on top
    return runs


def _records_by_run(conn: sqlite3.Connection, run_ids: list[int]) -> dict[int, tuple[str, int]]:
    """``run_id -> (retry phrase, durable record count)``.

    A ledger older than migration 6 has no ``job_records`` at all and one
    older than migration 8 has no retry columns; both come back empty rather
    than raising, because a status page that crashes on an old ledger is
    worse than one that shows less.
    """
    if not run_ids or "job_records" not in _tables(conn):
        return {}
    has_retry = {"attempt", "retry_state", "retry_policy"} <= _columns(conn, "job_records")
    columns = "run_id, record_type"
    if has_retry:
        columns += ", attempt, retry_state, retry_policy"
    placeholders = ",".join("?" for _ in run_ids)
    rows = conn.execute(
        f"SELECT {columns} FROM job_records WHERE run_id IN ({placeholders}) ORDER BY id",
        run_ids,
    ).fetchall()
    out: dict[int, tuple[str, int]] = {}
    for row in rows:
        run_id = int(row["run_id"])
        phrase, count = out.get(run_id, ("", 0))
        if has_retry and str(row["record_type"]) == RETRY_RECORD:
            phrase = _retry_phrase(row)
        else:
            count += 1
        out[run_id] = (phrase, count)
    return out


def _retry_phrase(row: sqlite3.Row) -> str:
    attempt = int(row["attempt"] or 0)
    state = str(row["retry_state"] or "")
    budget = ""
    try:
        policy = json.loads(str(row["retry_policy"] or "") or "{}")
        if policy.get("max_attempts"):
            budget = f" of {int(policy['max_attempts'])}"
    except (ValueError, TypeError):
        budget = ""
    phrase = f"attempt {attempt}{budget}" if attempt else ""
    if state:
        phrase = f"{phrase}, {state}" if phrase else state
    return phrase


def pending_cards(
    conn: sqlite3.Connection, tenant: str, *, secret_values: list[tuple[str, str]] | None = None
) -> list[CardGroup]:
    """Pending approval cards grouped by card type, oldest group first.

    Every value passes the transcript's redaction (``core/llm/transcript``)
    before it reaches the page: the same rule the model transcripts follow,
    so an API key or a TIN-shaped string can never ride a page that may be
    served over a tailnet.
    """
    rows = conn.execute(
        "SELECT id, agent, action_type, params_json, created_at "
        "FROM approval_queue WHERE tenant = ? AND status = 'pending' ORDER BY id",
        (tenant,),
    ).fetchall()
    groups: dict[str, CardGroup] = {}
    for row in rows:
        action_type = str(row["action_type"])
        card = Card(
            id=int(row["id"]),
            action_type=action_type,
            agent=str(row["agent"]),
            created_at=str(row["created_at"]),
            summary=_card_summary(str(row["params_json"]), secret_values or []),
        )
        groups.setdefault(action_type, CardGroup(action_type)).cards.append(card)
    return sorted(groups.values(), key=lambda g: (g.oldest, g.action_type))


def _card_summary(params_json: str, secret_values: list[tuple[str, str]]) -> str:
    """One line per card: its parameters, redacted, then trimmed. Trimming
    runs AFTER redaction so a cut can never leave half a secret standing."""
    try:
        params: dict[str, Any] = json.loads(params_json)
    except ValueError:
        params = {}
    parts = []
    for key in sorted(params):
        value = redact_text(str(params[key]), secret_values)
        if len(value) > MAX_VALUE_CHARS:
            value = value[: MAX_VALUE_CHARS - 1] + "…"
        parts.append(f"{key}={value}")
    line = ", ".join(parts)
    if len(line) > MAX_SUMMARY_CHARS:
        line = line[: MAX_SUMMARY_CHARS - 1] + "…"
    return line


# ---- the audit report ----------------------------------------------------


def auditor_report_dir(slug: str, *, tenants_root: Path | None = None) -> str:
    """``[auditor].report_dir`` read out of the tenant's own file.

    ``core/`` imports nothing from ``auditor/``, so this module parses the
    one key it needs the way ``engine init`` does. A tenant with no
    ``[auditor]`` table simply has no report directory.
    """
    path = (tenants_root or default_tenants_root()) / slug / "tenant.toml"
    if not path.is_file():
        return ""
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    return str(raw.get("auditor", {}).get("report_dir", ""))


def newest_report(report_dir: str | Path | None) -> Path | None:
    if not report_dir:
        return None
    directory = Path(report_dir).expanduser()
    if not directory.is_dir():
        return None
    reports = sorted(p for p in directory.glob(REPORT_GLOB) if p.is_file())
    if not reports:
        return None
    return max(reports, key=lambda p: (p.name, p.stat().st_mtime))


def new_section(text: str) -> list[str]:
    """The lines of the report's NEW section, heading excluded. An empty
    list means the report has no such section (the auditor's all-clear
    report is one line and has none)."""
    lines = text.splitlines()
    out: list[str] = []
    inside = False
    for line in lines:
        if line.startswith("## "):
            if inside:
                break
            inside = line.startswith(NEW_HEADING)
            continue
        if inside:
            out.append(line)
    while out and not out[0].strip():
        out.pop(0)
    while out and not out[-1].strip():
        out.pop()
    return out


# ---- rendering -----------------------------------------------------------


def templates_dir() -> Path:
    return Path(__file__).resolve().parent / "templates"


def _engine_commit() -> str:
    """The code commit this page was rendered from, short. ``unknown`` off a
    git checkout: a status page never fails over its own footer."""
    repo = Path(__file__).resolve().parents[2]
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return proc.stdout.strip() or "unknown"


def default_out_path(tenant: TenantConfig) -> Path | None:
    """``<[close].report_dir>/_status/status.html``: the tenant's own report
    tree, beside the auditor's folder and the sweep's, never inside them.
    ``None`` when the tenant configures no report tree; the command then
    asks for ``--out``."""
    if not tenant.close.report_dir:
        return None
    return Path(tenant.close.report_dir).expanduser() / STATUS_DIRNAME / STATUS_FILENAME


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _age(created_at: str, now: datetime) -> str:
    try:
        stamp = datetime.fromisoformat(created_at)
    except ValueError:
        return "unknown age"
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    seconds = int((now - stamp).total_seconds())
    if seconds < 60:
        return "just now"
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size:
            count = seconds // size
            return f"{count} {unit}{'s' if count != 1 else ''}"
    return "just now"


def _runs_html(runs: list[JobRun], tz: str) -> str:
    if not runs:
        return '<p class="empty">No runs on this ledger yet.</p>'
    head = (
        "<table><thead><tr><th>Job</th><th>Status</th><th>Landed</th>"
        "<th>Run key</th><th>Job ledger</th></tr></thead><tbody>"
    )
    body = []
    for run in runs:
        marks = []
        if run.retry:
            marks.append(_e(run.retry))
        if run.records:
            marks.append(f"{run.records} record{'s' if run.records != 1 else ''}")
        shadow = ' <span class="tag">shadow</span>' if run.shadow else ""
        body.append(
            f'<tr data-job="{_e(run.name)}" data-status="{_e(run.status)}">'
            f'<td class="job">{_e(run.name)}{shadow}</td>'
            f'<td><span class="status {_e(run.status)}">{_e(run.status)}</span></td>'
            f"<td>{_e(_local(run.landed_utc, tz))}</td>"
            f'<td class="key">{_e(run.run_key)}</td>'
            f'<td class="marks">{" &middot; ".join(marks)}</td>'
            f"</tr>"
        )
        if run.summary:
            body.append(f'<tr class="summary"><td colspan="5">{_e(run.summary)}</td></tr>')
    return head + "".join(body) + "</tbody></table>"


def _local(stamp: str, tz: str) -> str:
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return stamp
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return local_now(tz, at=parsed).strftime("%Y-%m-%d %H:%M %Z")


def _cards_html(groups: list[CardGroup], now: datetime) -> str:
    if not groups:
        return '<p class="empty">No cards pending: the queue is empty.</p>'
    blocks = []
    for group in groups:
        rows = "".join(
            f'<li data-card-id="{card.id}">'
            f'<span class="cardno">#{card.id}</span> {_e(card.summary)}</li>'
            for card in group.cards
        )
        blocks.append(
            f'<div class="group" data-card-type="{_e(group.action_type)}" '
            f'data-count="{group.count}">'
            f"<h3>{_e(group.action_type)} "
            f'<span class="count">{group.count}</span> '
            f'<span class="age">oldest {_e(_age(group.oldest, now))}</span></h3>'
            f"<ul>{rows}</ul></div>"
        )
    return "".join(blocks)


def _audit_html(report_dir: str | Path | None) -> str:
    report = newest_report(report_dir)
    if report is None:
        where = f" in {report_dir}" if report_dir else " (no [auditor].report_dir configured)"
        return f'<p class="empty">No audit report found{_e(where)}.</p>'
    lines = new_section(report.read_text(encoding="utf-8"))
    source = f'<p class="source">{_e(report.name)} &middot; {_e(report.parent)}</p>'
    if not lines:
        return (
            source + f'<p class="empty">{_e(report.name)} has no NEW section '
            "(an all-clear night writes none).</p>"
        )
    items = "".join(f"<li>{_e(line.lstrip('- ').strip())}</li>" for line in lines if line.strip())
    return source + f"<ul>{items}</ul>"


def render_status(
    tenant: TenantConfig,
    *,
    ledger_root: str | Path,
    report_dir: str | Path | None = None,
    at: datetime | None = None,
    engine_commit: str | None = None,
) -> str:
    """The whole page as one HTML string. Reads; never writes.

    ``report_dir`` is the auditor's report directory; the caller resolves it
    (``auditor_report_dir``) so this function reads exactly what it is told
    and a test can point it at a fixture.
    """
    now = at or datetime.now(UTC)
    tz = tenant.identity.timezone
    conn = read_only_connection(ledger_root)
    try:
        runs = last_run_per_job(conn, tenant.identity.slug)
        groups = pending_cards(
            conn, tenant.identity.slug, secret_values=env_values(tenant.secrets.values())
        )
    finally:
        conn.close()
    pending_total = sum(group.count for group in groups)
    failed = sum(1 for run in runs if run.failed)
    template = Template((templates_dir() / "status_page.html.tmpl").read_text(encoding="utf-8"))
    return template.substitute(
        title=_e(f"{tenant.identity.legal_name}: engine status"),
        headline=_e(
            f"{len(runs)} job(s) on the ledger, {failed} failed last run, "
            f"{pending_total} card(s) pending"
        ),
        runs=_runs_html(runs, tz),
        cards=_cards_html(groups, now),
        audit=_audit_html(report_dir),
        tenant=_e(tenant.identity.slug),
        commit=_e(engine_commit if engine_commit is not None else _engine_commit()),
        rendered=_e(local_now(tz, at=now).strftime("%Y-%m-%d %H:%M %Z")),
        ledger=_e(Path(ledger_root).expanduser()),
    )


def write_status(page: str, out: str | Path) -> Path:
    """Write the rendered page. The ONE write this module makes, and it is
    to the page's own file, never to the ledger."""
    target = Path(out).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(page, encoding="utf-8")
    return target
