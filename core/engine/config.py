"""Tenant configuration loader.

A tenant is pure configuration (architecture 3.3): ``tenants/<slug>/tenant.toml``
plus sibling registries. This module loads that TOML, validates it against a
pydantic schema, and resolves secret *references* from environment variables.
Secrets never live in the repo (invariant: secrets resolve at runtime from the
host environment). ``tenant.toml`` may only name a secret's environment
variable, never its value.

The loader is tenant-agnostic: it computes the tenants root relative to the
repository, or honours ``ENGINE_TENANTS_ROOT``, so ``core/`` carries no host
path of its own.
"""

from __future__ import annotations

import os
import tomllib
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

from ..adapters.bank_csv import BankCsvFormat

TENANTS_ROOT_ENV = "ENGINE_TENANTS_ROOT"


def default_tenants_root() -> Path:
    """Tenants directory, resolved relative to the repository root."""
    override = os.environ.get(TENANTS_ROOT_ENV)
    if override:
        return Path(override)
    # core/engine/config.py -> parents[2] is the repository root.
    return Path(__file__).resolve().parents[2] / "tenants"


class Identity(BaseModel):
    """Business identity. ``legal_name`` is the author/company stamped on any
    Office-format deliverable the engine generates (invariant 10)."""

    legal_name: str
    slug: str
    timezone: str = "America/New_York"


class Fiscal(BaseModel):
    year_start_month: int = Field(default=1, ge=1, le=12)


class Approval(BaseModel):
    """Human-gate thresholds. Below ``auto_file_under`` from a known vendor an
    invoice may auto-file; everything else queues (architecture 3.5)."""

    auto_file_under: float = Field(default=0.0, ge=0.0)
    currency: str = "USD"


class WorkbookColumn(BaseModel):
    """One column of the human-readable workbook view: a display ``label`` and
    the ledger ``field`` it renders. The labels are tenant vocabulary (they
    match the tenant's legacy sheet), which is why they live in config and not
    in core."""

    label: str
    field: str = ""


class ApSettings(BaseModel):
    """AP-domain settings: where documents land, where the legacy ledger is.

    ``landing_dir`` is the intake source. ``legacy_ledger_xlsx`` is read-only
    and used solely by the shadow parity diff. ``protected_paths`` lists the
    production surfaces the shadow path must never write (fidelity contract,
    section H); the write guard enforces it. ``filing_dir`` and
    ``workbook_path`` are where the engine files invoices and writes the
    human-readable workbook view; both pass the write guard.
    """

    landing_dir: str = ""
    legacy_ledger_xlsx: str = ""
    protected_paths: list[str] = Field(default_factory=list)
    # allowed_paths: explicit write carve-outs inside a protected root (the
    # engine's own book and filing dir after cutover). check_write permits
    # them; the runner's ledger-root refusal still ignores them.
    allowed_paths: list[str] = Field(default_factory=list)
    # filing_dir: where `apply` copies approved invoices; workbook_path: where
    # the workbook view is written. Pre-cutover both lived OUTSIDE
    # protected_paths; post-cutover they sit inside it and must be listed in
    # allowed_paths, or the guard refuses them.
    filing_dir: str = ""
    # Layout under filing_dir; "{month}" is the invoice's YYYY-MM (or _undated).
    # Tenants with a legacy tree set e.g. "{month}/_approved".
    filing_month_template: str = "{month}"
    workbook_path: str = ""
    workbook_columns: list[WorkbookColumn] = Field(default_factory=list)


class TimesheetsSettings(BaseModel):
    """Timesheets-domain settings: where submissions land (usually the same
    folder as AP intake) and where filed copies go (the tenant's Timesheets
    tree, month subfolders)."""

    landing_dir: str = ""
    filing_dir: str = ""


class DeadlinesSettings(BaseModel):
    """The deadline calendar (core/agents/deadlines; the Reminder construct in
    docs/boundary-rules.md). ``file`` is the obligations file, empty meaning
    ``obligations.toml`` beside tenant.toml; no file means the lane is off.
    ``ics_path`` is where the calendar file is written (empty: none), inside
    a write-guard carve-out. ``lead_days`` are the default reminder windows."""

    file: str = ""
    ics_path: str = ""
    lead_days: list[int] = Field(default_factory=lambda: [90, 30, 7])
    # Direct calendar writes (deadlines/calendar): "" detects the calendar in
    # code (core/agents/deadlines/calendar_sync.py); "graph", "google", "ics"
    # or "off" decides it. calendar_account defaults to [mail].keychain_account.
    # unattended = ["calendar"] is the owner's one-time grant to write without
    # a card per change.
    calendar: str = ""
    calendar_account: str = ""
    unattended: list[str] = Field(default_factory=list)


class BriefSettings(BaseModel):
    """The weekly brief (core/agents/brief): Tim's one page a week. ``dir`` is
    where each week's page is written (a write-guard carve-out; empty, none).
    ``recipients`` get it by email after an approved card, or unattended when
    ``unattended`` names "send" (the tenant's own policy, boundary rule 4)."""

    dir: str = ""
    recipients: list[str] = Field(default_factory=list)
    unattended: list[str] = Field(default_factory=list)


class ProjectsSettings(BaseModel):
    """The project registry's drift cards (core/agents/projects, #325).
    ``registry_path`` is the registry file the tenant's own sync writes; its
    newest ``drift_glob`` sibling is the input. ``folder_root`` is where an
    approved folder-missing card creates the folder (a write-guard
    carve-out). Off until the owner turns ``drift_cards`` on."""

    registry_path: str = ""
    drift_glob: str = "project-registry-drift-*.md"
    folder_root: str = ""
    drift_cards: bool = False


class ExpensePerson(BaseModel):
    """One person whose receipts the expenses agent processes. ``role`` decides the
    accounting treatment: owner reports are owner reimbursements, employee
    reports are payroll-adjacent reimbursements; both take the report flow.
    A ``vendor`` person (issue #120, owner decision 2026-08-14) submits
    through the same drop tree but books to the accounting-system vendor
    record named by ``qbo_vendor``, with every line coded to the project's
    chart account via ``project_account_template`` — project costs, never
    overhead. A vendor who invoices through their own entity instead stays
    in the AP lane and is not a person here.

    ``default_channel`` overrides the tenant-level payment channel on this
    person's confirm cards; empty inherits ``[expenses].default_channel``."""

    name: str
    role: str = "owner"  # owner | employee | vendor
    qbo_vendor: str = ""  # vendor-role: accounting-system vendor display name
    default_channel: str = ""  # empty = the tenant default


class ExpensesSettings(BaseModel):
    """Expenses-domain settings (docs/expenses-design.md).

    ``drop_dir`` is the per-person drop tree (`<person>/<project>/`
    subfolders). ``filing_dir`` is the 03_Expenses tree where receipts,
    report packages, and `_filed/` land (month subfolders); it must sit
    inside a write-guard carve-out. ``category_accounts`` maps report
    categories to the chart's fully-qualified account names for the QBO
    split record; an unmapped category parks a card, never guesses."""

    drop_dir: str = ""
    filing_dir: str = ""
    # Issue #112: the Taildrop receipt inbox the classifier pre-stage scans
    # (label-only contract), and the person its proposals attribute to (the
    # phone's owner); both correctable per-card at approval.
    inbox_dir: str = ""
    inbox_person: str = ""
    persons: list[ExpensePerson] = Field(default_factory=list)
    category_accounts: dict[str, str] = Field(default_factory=dict)
    # Vendor-role account resolution (issue #120): the fully-qualified chart
    # account for a project, with ``{number}`` standing for the project code
    # minus its letter prefix (P26_2001 -> 26_2001). A project the template
    # cannot resolve to an existing account parks a card, never guesses.
    project_account_template: str = ""
    # Report/zip filename prefix — tenant vocabulary (the legacy report
    # convention), so it lives here and never in core.
    file_prefix: str = ""
    # The reimbursement channel named on confirm cards (tenant vocabulary).
    default_channel: str = "Zelle"
    # Duplicate-guard and safety-net matching window, in days.
    match_window_days: int = Field(default=14, ge=1, le=90)


class QboSettings(BaseModel):
    """QuickBooks Online connection. ``token_file`` names a JSON file OUTSIDE
    the repo holding client credentials and the rotating refresh token; only
    the path lives in config (the secrets-never-in-repo rule). ``since_days``
    bounds how far back reconcile queries cleared transactions."""

    token_file: str = ""
    since_days: int = Field(default=30, ge=1, le=365)
    # Expected recurring non-AP payees (bank fees, payroll processors):
    # reconcile treats their clearings as out of scope instead of unknown
    # money, because the bank rules own their bookkeeping.
    reconcile_ignore_payees: list[str] = Field(default_factory=list)
    # A clearing that falls past every reconcile rule lands in out_of_scope.
    # That sink is right for cardswipe spend and wrong for real money: check
    # 3048 (a five-figure owner-side payment with no payee and no DocNumber on
    # the QBO row) sat in it silently from 2026-07-07 to 2026-09-14. At or
    # above this floor the clearing emits ap.reconcile.out_of_scope instead
    # of vanishing into a counter. 0 traces everything; the sink still
    # swallows anything below. Payees on reconcile_ignore_payees are never
    # traced at any amount -- that list is a decision, not a fall-through.
    reconcile_trace_floor_cents: int = Field(default=100_000, ge=0)
    # W2 (docs/w2-billpayment-design.md, phase 7 row 7.2): when true, the
    # ``ap/qbo-push-payments`` job records a BillPayment for every scheduled
    # check behind an approval card. A tenant setting, off by default: a
    # tenant may leave payment records to the bank feed and its own clicks.
    payment_records: bool = False
    # The hand-check lane (phase 7 row 7.4, issue #257). A cleared payment
    # nothing in the book explains, coded like contractor work, parks an
    # ``ap.record_direct_payment`` card proposing the payable. Off by default:
    # a tenant whose contractors all invoice through AP has no such lane.
    direct_payment_cards: bool = False
    # First-pass throttle, not a tax rule. The 1099-NEC obligation aggregates
    # across a year, so no single-payment floor is ever tax-correct; this
    # exists so switching the lane on does not dump every small payee-less
    # Travel and Meals charge into the queue on day one. Default $600 is the
    # reporting threshold, which makes it a defensible starting line. 0 cards
    # every candidate and takes the volume.
    direct_payment_floor_cents: int = Field(default=60_000, ge=0)
    # Registry cost types that describe work a person performs. THE
    # discriminator: freight and subcontract labor code to the same GL
    # account in this house style, and only the cost type separates them.
    direct_payment_cost_types: list[str] = Field(
        default_factory=lambda: ["Subcontractors", "Freelancers", "Reimbursement"]
    )
    # Account-name fragments that read as contractor work when the registry
    # knows no such payee and there is no cost type to read.
    direct_payment_account_patterns: list[str] = Field(
        default_factory=lambda: [
            "Project Expense",
            "Subcontractor",
            "Contract Labor",
            "Professional Fees",
        ]
    )
    # Registry ``payment_channel`` values whose checks are written by the
    # BANK, not by the tenant (issue #285). Their number does not exist at
    # scheduling, so the row is committed with a send date and no reference
    # and 7.3's check-number rule can never settle it from the statement.
    # Naming a channel here turns on the narrow exception in
    # ``reconcile.decide_statement``; empty (the default) leaves the tier
    # inert, so no tenant inherits it. The strings are the tenant's own
    # registry vocabulary, which the engine treats as opaque.
    bill_pay_channels: list[str] = Field(default_factory=list)


class QboSweepSettings(BaseModel):
    """The weekly bank-feed sweep (the tenant keeps its own sweep design).

    The sweep itself is a tenant-local browser lane under ``tenants/<slug>/
    host`` and imports no engine code; the rest of its ``[qbo_sweep]`` table
    (the accounts, the unattended grant, the deposit policy) is read there,
    not here. What the ENGINE needs from the table is where the sweep leaves
    its note, because the rows the sweep parks for the owner become approval
    cards (phase 7 row 7.4). Extra keys in the table are ignored, so a tenant
    that never runs a sweep configures nothing.
    """

    # The folder the sweep writes ``sweep-YYYY-MM-DD.md`` into. Empty (the
    # default) means no sweep runs here and the card lane reads nothing.
    note_dir: str = ""
    # Parked rows become ``qbo.sweep_parked`` cards. On where a sweep exists:
    # the note is the owner's own queue, and a card is a question, never a
    # write. A tenant that wants the note to stay the end of the road sets
    # this false and the job is a one-line noop.
    parked_cards: bool = True


class CloseSettings(BaseModel):
    """Month-end close (docs/closer-design.md). ``report_dir`` is where
    CLOSE_PREFLIGHT.md and the close packet land (month subfolders); it must
    sit inside a write-guard carve-out. ``bank_account`` is the checking
    account's name in the accounting system. ``uncategorized_block_over``
    is the dollar total of uncategorized month activity above which the
    categorization sweep escalates WARN to BLOCK."""

    report_dir: str = ""
    bank_account: str = ""
    uncategorized_block_over: float = Field(default=500.0, ge=0.0)
    # Owner-shaped transactions: payee empty or matching one of these names.
    owner_names: list[str] = Field(default_factory=list)
    # A journal entry is payroll-shaped when any marker appears in its
    # DocNumber or note (case-insensitive). The payroll processor's name is
    # tenant vocabulary, so it lives here, never in core.
    payroll_markers: list[str] = Field(default_factory=lambda: ["payroll"])
    # Where loose receipts land pending an expense report (month subfolders).
    expenses_dir: str = ""
    # The issue-side invoice register (AR snapshot pairs its rows to PDFs).
    invoice_register_xlsx: str = ""
    # Who receives the sealed month's financial statements for review. The
    # send is an external write, so it always rides an approval card
    # (invariant 7); an empty list skips the send leg entirely.
    statements_recipients: list[str] = Field(default_factory=list)
    # After a live statements render, reveal the workbook in the OS file
    # browser (macOS ``open -R``). Ceremony convenience; headless-safe off.
    reveal_in_finder: bool = False


class MailSettings(BaseModel):
    """Mailbox fetch. Azure app ids are identifiers, not secrets; the actual
    credential is the MSAL cache in the OS keychain, named here and never in
    the repo. ``denied_senders`` is the privacy boundary: matching mail is
    counted and otherwise leaves no recorded trace."""

    client_id: str = ""
    tenant_id: str = ""
    scopes: list[str] = Field(default_factory=lambda: ["Mail.Read"])
    keychain_service: str = ""
    keychain_account: str = ""
    landing_dir: str = ""
    since_days: int = Field(default=3, ge=1, le=60)
    allowed_extensions: list[str] = Field(default_factory=list)  # empty = built-in default
    denied_senders: list[str] = Field(default_factory=list)
    max_bytes: int = Field(default=30 * 1024 * 1024, ge=1)
    # Recognized credit-card charge receipts (sender substring match, same
    # semantics as denied_senders) route to the expenses tree's _cc_charges/
    # instead of the AP landing folder — filed paper awaiting statement
    # reconciliation, never a nightly not-landed flag (expenses design decision 3).
    cc_charge_senders: list[str] = Field(default_factory=list)


class ArSettings(BaseModel):
    """Accounts receivable: the remittance-advice lane (issue #282).

    A customer's remittance advice is body-only mail, so the AP attachment
    feed can never see it. ``remittance_subject`` is the gate every message
    passes before its body is read at all; ``remittance_senders`` narrows it
    further and is EMPTY by default on purpose, so the first advice from a
    new customer is read the day it arrives rather than the day somebody
    remembers to add the address.

    ``deposit_markers`` and ``clearing_window_days`` govern the bank half:
    a statement deposit whose amount equals a recorded remittance closes the
    loop. Amount equality is the test; markers narrow it when a tenant wants
    the payer's own wording on the bank line to count too.
    """

    remittance_senders: list[str] = Field(default_factory=list)  # empty = any sender
    remittance_subject: str = "Remittance Advice"  # empty turns the lane off
    since_days: int = Field(default=14, ge=1, le=365)
    # The mail folder the listing reads. EMPTY means the whole mailbox, every
    # folder included, which is the default because an advice is routinely
    # filed out of the inbox before the next morning's run.
    mail_folder: str = ""
    deposit_markers: list[str] = Field(default_factory=list)  # empty = any description
    clearing_window_days: int = Field(default=30, ge=1, le=365)
    # Where the lane's daily note lands, under an _ar/ subfolder. Empty falls
    # back to [close].report_dir; empty there writes no note.
    report_dir: str = ""


class W9Settings(BaseModel):
    """W-9 intake routing (docs/w9-1099-design.md build 3). ``folder`` is the
    consolidated vendor W-9 folder an approved card's execution copies into;
    it must sit inside a write-guard carve-out. Empty disables the lane. The
    engine never writes the vendor registry and never extracts a TIN — the
    approved card emits the registry diff as an event, nothing more."""

    folder: str = ""


CRON_FIELD_BOUNDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
"""minute, hour, day of month, month, day of week. Five fields, Vixie order.
``supercronic`` accepts second-resolution six and seven field forms too; the
renderer stays at five so a crontab a person reads means what cron has always
meant (docs/decisions/2026-09-16-the-crontab-is-rendered-from-the-tenant.md)."""


def cron_field_error(field: str, low: int, high: int) -> str:
    """The reason ``field`` is not a legal cron field, or ``""``."""
    for part in field.split(","):
        step = "1"
        if "/" in part:
            part, _, step = part.partition("/")
            if not step.isdigit() or int(step) < 1:
                return f"step {step!r} is not a positive integer"
        if part == "*":
            continue
        start, sep, end = part.partition("-")
        for value in (start, end) if sep else (start,):
            if not value.isdigit():
                return f"{value!r} is not a number, a range, or *"
            if not low <= int(value) <= high:
                return f"{value} is outside {low}-{high}"
        if sep and int(end) < int(start):
            return f"range {part!r} runs backwards"
    return ""


def cron_error(expression: str) -> str:
    """The reason ``expression`` is not a five-field cron schedule, or ``""``.

    Validation lives here, at config load, because the alternative is a typo
    discovered by a job that never fired: cron has no error channel, and a
    schedule nobody watches is exactly what this row automates.
    """
    fields = expression.split()
    if len(fields) != 5:
        return f"{expression!r} has {len(fields)} fields; a cron schedule has 5 (m h dom mon dow)"
    for field, (low, high) in zip(fields, CRON_FIELD_BOUNDS, strict=True):
        reason = cron_field_error(field, low, high)
        if reason:
            return f"{expression!r}: field {field!r}: {reason}"
    return ""


class HostSchedule(BaseModel):
    """The container's crontab, as data (phase 7 row 7.21).

    One string per entry, a five-field cron expression, and the expression IS
    the on switch: empty renders no line. One knob per entry cannot disagree
    with itself the way a time plus an ``enabled`` boolean can.

    The Mac's schedule is its launchd plists and it does not read this table
    (docs/decisions/2026-09-16-the-crontab-is-rendered-from-the-tenant.md).
    """

    engine: str = "0 8 * * *"
    auditor: str = "0 2 * * *"
    ledger_backup: str = "0 23 * * *"
    heartbeat: str = "*/30 * * * *"
    retries: str = "*/15 * * * *"
    # The build lane (row 7.6) is the host's own program, not one this repo
    # ships, so it is off by default and naming a time without naming the
    # command is a config error rather than an empty crontab line.
    build: str = ""
    build_command: str = ""

    @model_validator(mode="after")
    def _schedules_are_cron(self) -> HostSchedule:
        for name in ("engine", "auditor", "ledger_backup", "heartbeat", "retries", "build"):
            expression = getattr(self, name)
            if not expression:
                continue
            reason = cron_error(expression)
            if reason:
                raise ValueError(f"[host.schedule].{name}: {reason}")
        if self.build and not self.build_command:
            raise ValueError(
                "[host.schedule].build names a time but [host.schedule].build_command is "
                "empty: say what the build lane runs, or leave build empty to keep it off"
            )
        return self


class HostSettings(BaseModel):
    """What the host this tenant runs on is expected to provide (row 7.21).

    Read by ``engine schedule`` (which renders the crontab) and ``engine
    doctor`` (which reports what is missing). ``healthchecks`` says this host
    is expected to ping the off-box dead man, so doctor treats a missing
    ``HC_PING_BASE`` as an install that is not finished rather than a host
    that chose not to be watched.
    """

    healthchecks: bool = False
    schedule: HostSchedule = Field(default_factory=HostSchedule)


LLM_DETERMINISTIC = "deterministic"
"""The reserved tier name: no model may be called for this job. Resolving
such a job through the policy raises a typed error naming the job."""

LLM_ADAPTERS = ("fixture", "anthropic_messages", "openai_compat", "claude_agent_sdk")
"""Adapter names a tier may carry. The first three are ``complete()``
adapters under ``core/llm/adapters``. ``claude_agent_sdk`` is describe-only
in row 7.9: it names what a tenant runs today (the Claude Agent SDK under a
seat) so the table can state reality before the call sites move (rows 7.10
to 7.12); the policy refuses to build it (row 7.15 decides whether an SDK
``complete()`` adapter exists)."""


class LlmPricing(BaseModel):
    """List price per million tokens. Decimals, never floats: a TOML float
    such as ``0.25`` is read through its text, so ``0.25`` stays ``0.25``.
    Required on every tier so ``usd`` is never unknown on a policy call."""

    input_usd_per_mtok: Decimal = Field(ge=0)
    output_usd_per_mtok: Decimal = Field(ge=0)


class LlmTier(BaseModel):
    """One model tier (docs/model-seam-design.md, "The policy table"):
    which adapter speaks to it, the provider's model id passed through
    untouched, an optional endpoint, the NAME of the environment variable
    holding the key (never the value), the price, and the tiers to walk on
    a transient transport failure (other tier names, in order)."""

    adapter: str
    model: str
    base_url: str = ""
    api_key_env: str = ""
    pricing: LlmPricing
    fallback: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _known_adapter(self) -> LlmTier:
        if self.adapter not in LLM_ADAPTERS:
            raise ValueError(f"adapter {self.adapter!r} is not one of {', '.join(LLM_ADAPTERS)}")
        return self


class LlmBudget(BaseModel):
    """The tenant's monthly model spend cap in USD. ``None`` is no cap.
    The policy refuses a call once the month's recorded spend reaches it
    (an ``llm.budget`` anomaly on the run, a typed error the job catches)."""

    monthly_usd: Decimal | None = Field(default=None, ge=0)


class LlmSettings(BaseModel):
    """The ``[llm]`` policy tables (phase 7 row 7.9). ``tiers`` names the
    models; ``jobs`` maps a job type to a tier name or to ``deterministic``;
    ``jobs.default`` is the tier for any job type not listed (absent, an
    unlisted job is refused). Every reference is checked at load, so a
    typo in a tier name fails config validation naming the job, never a
    call at 08:00. Model ids and key variable names live here and only
    here; nothing under ``core/`` carries a model name."""

    tiers: dict[str, LlmTier] = Field(default_factory=dict)
    jobs: dict[str, str] = Field(default_factory=dict)
    budget: LlmBudget = Field(default_factory=LlmBudget)

    @model_validator(mode="after")
    def _references_resolve(self) -> LlmSettings:
        if LLM_DETERMINISTIC in self.tiers:
            raise ValueError(
                f"[llm.tiers] may not name a tier {LLM_DETERMINISTIC!r}: that word is "
                "reserved for jobs no model may serve"
            )
        for name, tier in self.tiers.items():
            for other in tier.fallback:
                if other == name:
                    raise ValueError(f"[llm.tiers].{name} lists itself as a fallback")
                if other not in self.tiers:
                    raise ValueError(
                        f"[llm.tiers].{name} falls back to {other!r}, which is not a tier"
                    )
        for job, tier in self.jobs.items():
            if tier != LLM_DETERMINISTIC and tier not in self.tiers:
                raise ValueError(
                    f"[llm.jobs].{job} names tier {tier!r}, which is not in [llm.tiers] "
                    f"(tiers: {', '.join(sorted(self.tiers)) or 'none'}; "
                    f"or {LLM_DETERMINISTIC!r})"
                )
        return self


class TenantConfig(BaseModel):
    identity: Identity
    fiscal: Fiscal = Field(default_factory=Fiscal)
    approval: Approval = Field(default_factory=Approval)
    ap: ApSettings = Field(default_factory=ApSettings)
    timesheets: TimesheetsSettings = Field(default_factory=TimesheetsSettings)
    expenses: ExpensesSettings = Field(default_factory=ExpensesSettings)
    bank_csv: BankCsvFormat = Field(default_factory=BankCsvFormat)
    qbo: QboSettings = Field(default_factory=QboSettings)
    qbo_sweep: QboSweepSettings = Field(default_factory=QboSweepSettings)
    mail: MailSettings = Field(default_factory=MailSettings)
    ar: ArSettings = Field(default_factory=ArSettings)
    close: CloseSettings = Field(default_factory=CloseSettings)
    w9: W9Settings = Field(default_factory=W9Settings)
    deadlines: DeadlinesSettings = Field(default_factory=DeadlinesSettings)
    brief: BriefSettings = Field(default_factory=BriefSettings)
    projects: ProjectsSettings = Field(default_factory=ProjectsSettings)
    llm: LlmSettings = Field(default_factory=LlmSettings)
    host: HostSettings = Field(default_factory=HostSettings)
    # logical secret name -> environment variable name (never a value)
    secrets: dict[str, str] = Field(default_factory=dict)

    def resolve_secret(self, logical_name: str) -> str:
        """Resolve a secret reference to its value from the environment.

        Raises if the tenant did not declare the secret, or if the named
        environment variable is unset. Nothing is ever read from a repo file.
        """
        if logical_name not in self.secrets:
            raise KeyError(f"tenant does not declare a secret named {logical_name!r}")
        env_var = self.secrets[logical_name]
        try:
            return os.environ[env_var]
        except KeyError as exc:
            raise KeyError(
                f"secret {logical_name!r} references unset environment variable {env_var!r}"
            ) from exc


class TenantNotFoundError(FileNotFoundError):
    pass


class MissingFolderError(FileNotFoundError):
    """A folder the tenant config names does not exist on this host.

    The runner's failure summary names ``engine doctor <tenant>`` for this
    one, because doctor already checks every configured folder (issue #6).
    """


def load_tenant(
    slug: str, *, tenants_root: Path | None = None, check_evals: bool = True
) -> TenantConfig:
    """Load and validate ``tenants/<slug>/tenant.toml``.

    ``check_evals`` runs the eval gate (row 7.13): a ``[llm.jobs]``
    assignment pointing a GATED job at a model with no green
    ``core/llm/eval_sets/<job>/results/<model_id>.json`` is refused here,
    naming the command that produces the evidence. ``engine evals run``
    itself passes ``check_evals=False``, because the command that PRODUCES
    the evidence has to be able to read the file that lacks it; nothing else
    in the engine turns the gate off.
    """
    root = tenants_root or default_tenants_root()
    path = root / slug / "tenant.toml"
    if not path.exists():
        known = (
            sorted(d.name for d in root.iterdir() if (d / "tenant.toml").is_file())
            if root.is_dir()
            else []
        )
        raise TenantNotFoundError(
            f"no tenant config at {path}; known tenants: {', '.join(known) or 'none'}; "
            f"create it with `engine init {slug}`"
        )
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    config = TenantConfig.model_validate(data)
    if config.identity.slug != slug:
        raise ValueError(
            f"tenant.toml slug {config.identity.slug!r} does not match directory {slug!r}"
        )
    if check_evals:
        # Imported here, not at module scope: core.llm reads this module.
        from core.llm.evals import check_eval_gate

        check_eval_gate(config.llm, tenant=slug)
    return config


def tenant_dir(slug: str, *, tenants_root: Path | None = None) -> Path:
    return (tenants_root or default_tenants_root()) / slug
