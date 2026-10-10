"""``engine doctor <tenant>``: what this host is still missing (row 7.21).

An installed container has nobody watching a terminal. Every input it needs is
either present at boot or a scheduled job fails at 02:00 into a log file no
one reads. Doctor is the pre-flight for the whole install, in one command with
one exit code: 0 when nothing is owed, non-zero with one line per missing
item.

Two rules it never breaks:

* **A secret's VALUE never leaves this module.** ``tenant.toml`` names the
  environment variable; doctor reports set or unset and prints the NAME. The
  W-9 TIN discipline, extended to tokens.
* **Unconfigured is not missing.** A tenant with no mailbox and no accounting
  connection is a legal tenant, and a doctor that nagged about lanes nobody
  asked for would be muted the way any crying-wolf check is. Those land on a
  ``skip`` line and the exit code stays 0. Doctor fails only on something the
  tenant asked for and did not get.

Checks, in report order: the tenant file loads; every declared secret; the
encrypted secrets file and whether it covers what the tenant declares (row
7.22); every model tier that calls a provider; the accounting connection; the
mailbox; each folder the tenant names; the ledger and, when the nightly push
is scheduled, its remote; the dead man; the scheduler and every command its
crontab names; the eval results; and that no other tenant's credentials are
reachable by this OS user (private #359).
"""

from __future__ import annotations

import getpass
import os
import shutil
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .config import (
    LLM_DETERMINISTIC,
    TenantConfig,
    TenantNotFoundError,
    default_tenants_root,
    load_tenant,
    tenant_dir,
)
from .init import folders_for
from .kit import KIT_FILES, KitError, load_part
from .runner import resolve_ledger_root
from .schedule import entries
from .secrets import MISSING, OK, SECRETS_FILENAME, SKIP, SecretsProbe
from .secrets import probe as probe_secrets

IMAGE_ENV = "ENGINE_IMAGE"
"""Set by the container entrypoint to the image reference or digest it booted
from. Its presence is how a host says "my code came from an image, not a
checkout": ``scripts/run-preflight.sh`` reads it for the same reason."""

PING_BASE_ENV = "HC_PING_BASE"
LEDGER_ROOT_ENV = "ENGINE_LEDGER_ROOT"
FIXTURE_ADAPTER = "fixture"


@dataclass(frozen=True)
class Check:
    """One thing this host needs, and whether it has it."""

    name: str
    status: str
    detail: str

    def line(self) -> str:
        label = "MISSING" if self.status == MISSING else self.status
        return f"  {label:<7}  {self.name}: {self.detail}"


@dataclass(frozen=True)
class DoctorReport:
    tenant: str
    checks: list[Check]

    @property
    def missing(self) -> list[Check]:
        return [c for c in self.checks if c.status == MISSING]

    @property
    def ok(self) -> bool:
        return not self.missing

    def lines(self) -> list[str]:
        skipped = len([c for c in self.checks if c.status == SKIP])
        head = (
            f"doctor {self.tenant}: {len(self.checks)} checks, "
            f"{len(self.missing)} missing, {skipped} not configured"
        )
        return [head, *(c.line() for c in self.checks)]


def _writable(path: Path) -> bool:
    return os.access(path, os.W_OK | os.X_OK)


def _secret_checks(
    cfg: TenantConfig, env: dict[str, str], provided: tuple[str, ...] = ()
) -> list[Check]:
    """``[secrets]`` is a registry of NAMES, the same list ``secrets.ref``
    carries: a tenant declares the variables its adapters will want before any
    of them runs. So an unset one is reported and never fails the install.
    What fails is a lane that IS configured and short of what it needs: the
    tier keys below, the accounting token file, the mailbox. When an adapter
    starts resolving a logical name by itself, its own check goes beside those
    and says which lane is asking.

    ``provided`` is what the encrypted file carries (row 7.22), and it is here
    because of what a plain ``docker compose exec ... engine doctor`` looked
    like without it: every variable reported "not set on this host" on a box
    whose scheduled jobs had all of them. An exec does not inherit the
    entrypoint's environment; the file is the honest answer to where the value
    comes from."""
    checks = []
    for logical, variable in sorted(cfg.secrets.items()):
        if env.get(variable):
            detail = f"environment variable {variable} is set"
            status = OK
        elif variable in provided:
            detail = (
                f"environment variable {variable} comes from {SECRETS_FILENAME} "
                "(the scheduled jobs carry it; this command's own environment does not)"
            )
            status = OK
        else:
            detail = f"environment variable {variable} is declared, not set on this host"
            status = SKIP
        checks.append(Check(f"secret {logical}", status, detail))
    return checks


def _secrets_file_checks(
    cfg: TenantConfig, env: dict[str, str], result: SecretsProbe
) -> list[Check]:
    """The encrypted file beside ``tenant.toml`` (row 7.22), and whether every
    variable the tenant DECLARES comes from somewhere.

    Two lines, on purpose. The first is about the file: is the pair here, does
    it open, how many variables did it carry. The second is the question the
    first cannot answer on its own, and the one that actually costs a business
    an 08:00 run: a variable ``tenant.toml`` names that is in neither the
    environment nor the file. It is scoped to hosts that use this lane, so a
    host with no file keeps exactly the behaviour it had before this row: the
    per-secret lines above say which variables are set, and none of them
    fails the install."""
    checks = [Check("secrets file", result.status, result.detail)]
    if result.status != OK:
        # Two different silences, and saying the wrong one is a lie an
        # operator acts on: a host with no file at all, and a host whose file
        # is right there and would not open.
        detail = (
            f"no {SECRETS_FILENAME} on this host: the lines above say which "
            "declared variables are set"
            if result.status == SKIP
            else "the line above did not open the file, so nothing can be said about what it covers"
        )
        checks.append(Check("secrets coverage", SKIP, detail))
        return checks
    covered = set(result.names)
    owed = sorted(
        variable
        for variable in cfg.secrets.values()
        if variable not in covered and not env.get(variable)
    )
    if owed:
        checks.append(
            Check(
                "secrets coverage",
                MISSING,
                "declared in tenant.toml, in neither the environment nor the "
                "encrypted file: " + ", ".join(owed),
            )
        )
    else:
        checks.append(
            Check(
                "secrets coverage",
                OK,
                f"every variable [secrets] declares ({len(cfg.secrets)}) is provided",
            )
        )
    return checks


def _tier_checks(cfg: TenantConfig, env: dict[str, str]) -> list[Check]:
    """A tier that calls a provider is owed the key variable it names. The
    fixture adapter calls nobody, which is what keeps a fresh tenant green
    offline (row 7.19's first hour)."""
    checks = []
    for name, tier in sorted(cfg.llm.tiers.items()):
        if tier.adapter == FIXTURE_ADAPTER:
            checks.append(
                Check(f"model tier {name}", SKIP, "the fixture adapter calls no provider")
            )
            continue
        if not tier.api_key_env:
            checks.append(
                Check(
                    f"model tier {name}",
                    SKIP,
                    f"adapter {tier.adapter} with no api_key_env (a local endpoint)",
                )
            )
            continue
        present = bool(env.get(tier.api_key_env))
        checks.append(
            Check(
                f"model tier {name}",
                OK if present else MISSING,
                f"adapter {tier.adapter} needs {tier.api_key_env}, which is "
                + ("set" if present else "not set"),
            )
        )
    return checks


def _qbo_check(cfg: TenantConfig) -> Check:
    if not cfg.qbo.token_file:
        return Check(
            "accounting connection",
            SKIP,
            "[qbo].token_file is empty: no live connection on this host",
        )
    token = Path(cfg.qbo.token_file).expanduser()
    if not token.is_file():
        return Check("accounting connection", MISSING, f"[qbo].token_file {token} does not exist")
    if not os.access(token, os.R_OK):
        return Check("accounting connection", MISSING, f"[qbo].token_file {token} is not readable")
    return Check("accounting connection", OK, f"token file {token} is present")


def _mail_check(cfg: TenantConfig) -> Check:
    if not cfg.mail.client_id:
        return Check("mailbox", SKIP, "[mail].client_id is empty: the mail fetch is off")
    provider = cfg.mail.provider.strip().lower()
    # The seam (core/adapters/mail.py): each provider owes its own names, and
    # an empty provider owes the provider first. Neither is the default.
    per_provider = {
        "graph": ("tenant_id",),
        "gmail": ("client_secret_env",),
    }
    keys: tuple[str, ...] = ("keychain_service", "keychain_account", "landing_dir")
    owed: list[str] = []
    if provider not in per_provider:
        owed.append(
            "provider (graph or gmail)" if not provider else f"provider ({provider!r} is unknown)"
        )
    else:
        keys = per_provider[provider] + keys
    owed.extend(key for key in keys if not getattr(cfg.mail, key))
    if owed:
        return Check("mailbox", MISSING, "[mail] is configured but empty: " + ", ".join(owed))
    return Check(
        "mailbox",
        OK,
        f"{provider} app {cfg.mail.client_id} with its credential at {cfg.mail.keychain_service}"
        " (run `engine mail consent` once per host)",
    )


def _folder_checks(cfg: TenantConfig, raw: dict, *, create: bool = False) -> list[Check]:
    checks = []
    for folder in folders_for(cfg, raw):
        path = Path(folder).expanduser()
        if create and not path.exists():
            path.mkdir(parents=True)
            checks.append(Check(f"folder {folder}", OK, f"{path} created"))
        elif not path.is_dir():
            checks.append(Check(f"folder {folder}", MISSING, f"{path} does not exist"))
        elif not _writable(path):
            checks.append(Check(f"folder {folder}", MISSING, f"{path} is not writable"))
        else:
            checks.append(Check(f"folder {folder}", OK, str(path)))
    return checks


def _ledger_checks(cfg: TenantConfig, slug: str, env: dict[str, str]) -> list[Check]:
    root = resolve_ledger_root(slug, env.get(LEDGER_ROOT_ENV))
    if not root.is_dir():
        return [
            Check(
                "ledger", MISSING, f"{root} does not exist (run `engine init {slug}` on a new host)"
            )
        ]
    config = root / ".git" / "config"
    if not config.is_file():
        return [
            Check("ledger", MISSING, f"{root} is not a repository: the ledger is not backed up")
        ]
    checks = [Check("ledger", OK, str(root)), _ledger_size(root)]
    scheduled = cfg.host.schedule.ledger_backup
    if not scheduled:
        checks.append(
            Check("ledger remote", SKIP, "[host.schedule].ledger_backup is empty: no nightly push")
        )
    elif '[remote "' in config.read_text(encoding="utf-8", errors="replace"):
        checks.append(Check("ledger remote", OK, f"{root} has a remote to push to"))
    else:
        checks.append(
            Check(
                "ledger remote",
                MISSING,
                f"{root} has no remote, and [host.schedule].ledger_backup is "
                f"{scheduled!r}: that job fails every night until one is added",
            )
        )
    return checks


def _ledger_size(root: Path) -> Check:
    """How big the ledger repository is, and how much of it is unpacked
    (public #13). The database is committed on every run, so the repository
    grows with the run count; the 23:00 job packs it after the push. The
    commit count is there for a restore: a clone that checked out nothing
    reads 0. Informational; size alone fails nothing."""
    git_dir = root / ".git"
    total = sum(p.stat().st_size for p in git_dir.rglob("*") if p.is_file())
    loose = sum(
        p.stat().st_size
        for d in (git_dir / "objects").glob("[0-9a-f][0-9a-f]")
        for p in d.iterdir()
        if p.is_file()
    )
    count = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    commits = int(count) if count.isdigit() else 0
    mb = 1024 * 1024
    return Check(
        "ledger size",
        OK,
        f"{commits} commit{'' if commits == 1 else 's'}, {total / mb:.1f} MB in .git "
        f"({loose / mb:.1f} MB loose; the nightly push packs it)",
    )


def _dead_man_check(cfg: TenantConfig, env: dict[str, str]) -> Check:
    if not cfg.host.healthchecks:
        return Check(
            "dead-man pings",
            SKIP,
            "[host].healthchecks is false: this host is not watched off-box",
        )
    if not env.get(PING_BASE_ENV):
        return Check("dead-man pings", MISSING, f"{PING_BASE_ENV} is not set")
    return Check("dead-man pings", OK, f"{PING_BASE_ENV} is set (the key never appears here)")


def _scheduler_checks(cfg: TenantConfig, env: dict[str, str], repo: Path) -> list[Check]:
    """The scheduler itself, and every command its crontab will name. A line
    naming a script that is not on this host is a job that fails once a day,
    forever, in a file nobody opens."""
    image = env.get(IMAGE_ENV, "")
    checks = []
    if image:
        checks.append(Check("image", OK, f"running image {image}"))
        found = shutil.which("supercronic", path=env.get("PATH"))
        checks.append(
            Check("supercronic", OK, found)
            if found
            else Check("supercronic", MISSING, "no supercronic on PATH: nothing reads the crontab")
        )
    else:
        checks.append(
            Check(
                "supercronic",
                SKIP,
                f"{IMAGE_ENV} is unset: this host is a checkout with its own scheduler",
            )
        )
    for entry in entries(cfg, repo=repo, log_dir="/logs"):
        command = Path(entry.command.split()[0]).expanduser()
        if not command.is_file():
            checks.append(
                Check(
                    f"schedule {entry.name}",
                    MISSING,
                    f"{entry.cron} runs {command}, which is absent",
                )
            )
        elif not os.access(command, os.X_OK):
            checks.append(Check(f"schedule {entry.name}", MISSING, f"{command} is not executable"))
        else:
            checks.append(Check(f"schedule {entry.name}", OK, f"{entry.cron} {command}"))
    return checks


def _eval_gate_check(cfg: TenantConfig) -> Check:
    """Row 7.13's gate runs inside ``load_tenant``: a gated job pointed at a
    model with no green results file refuses the whole tenant, naming the
    ``engine evals run`` command that produces the evidence. So reaching this
    line means the gate passed, and the check states which jobs it covered.
    Imported here, not at module scope: ``core.llm`` reads ``core.engine``."""
    from ..llm.evals import eval_sets_root, gated_jobs

    gated = sorted(
        job
        for job in gated_jobs(eval_sets_root())
        if cfg.llm.jobs.get(job, LLM_DETERMINISTIC) != LLM_DETERMINISTIC
    )
    if not gated:
        return Check("eval results", SKIP, "no gated model job runs on a model here")
    return Check("eval results", OK, f"green results for {', '.join(gated)}")


def _credentials_held(cfg: TenantConfig, secrets: SecretsProbe) -> list[str]:
    """What this OS user can reach of the tenant's OWN credentials, by label.
    Environment variables are not on the list: a host's model key is shared by
    design, and a variable cannot say which tenant it belongs to. The mailbox
    counts once configured, because its token cache lives in this user's
    keychain whether or not consent has run yet."""
    held = []
    if cfg.qbo.token_file:
        token = Path(cfg.qbo.token_file).expanduser()
        if token.is_file() and os.access(token, os.R_OK):
            held.append("accounting token file")
    if cfg.mail.client_id and cfg.mail.keychain_service:
        held.append("mailbox token cache")
    if secrets.status == OK:
        held.append(SECRETS_FILENAME)
    return held


def _one_tenant_check(
    slug: str,
    cfg: TenantConfig,
    secrets: SecretsProbe,
    root: Path | None,
    env: dict[str, str],
) -> Check:
    """One credentialed tenant per OS user (private #359). Nothing isolates
    one tenant's run from another's ledger and tokens when both run as the
    same user, so a second tenant whose credentials this user can reach is a
    missing item naming both. A neighbour that does not load cannot run
    either and is not counted. Labels only: no path or value is printed."""
    name = "one tenant per user"
    held = _credentials_held(cfg, secrets)
    if not held:
        return Check(name, SKIP, "this tenant holds no credentials on this host")
    tenants_root = root if root is not None else default_tenants_root()
    neighbours = []
    for directory in sorted(tenants_root.iterdir()) if tenants_root.is_dir() else []:
        other = directory.name
        if other == slug or not (directory / "tenant.toml").is_file():
            continue
        try:
            other_cfg = load_tenant(other, tenants_root=root, check_evals=False)
        except (ValueError, OSError):
            continue
        other_held = _credentials_held(other_cfg, probe_secrets(other, tenants_root=root, env=env))
        if other_held:
            neighbours.append(f"{other} ({', '.join(other_held)})")
    if not neighbours:
        return Check(name, OK, f"the only tenant holding credentials under {tenants_root}")
    return Check(
        name,
        MISSING,
        f"{slug} ({', '.join(held)}) shares OS user {getpass.getuser()} with "
        f"{'; '.join(neighbours)}: nothing isolates one tenant's run from the "
        "other's ledger and tokens. Run each tenant as its own OS user or in its "
        "own container (docs/install.md)",
    )


def _document_checks(cfg: TenantConfig) -> list[Check]:
    """Which model tiers may receive a document (#358). Without
    ``[llm].document_tiers`` any tier may: a SKIP, the default. With it, each
    allowed tier is listed with what its provider retains, and a document job
    routed to a tier off the list is MISSING, found here instead of at the
    first refused call. Imported here: ``core.llm`` reads ``core.engine``."""
    from ..llm.policy import DOCUMENT_JOB_TYPES

    llm = cfg.llm
    if llm.document_tiers is None:
        return [
            Check(
                "documents", SKIP, "no [llm].document_tiers: any model tier may receive documents"
            )
        ]
    retention = {True: "zero data retention", False: "retains data", None: "retention not stated"}
    listed = [
        f"{name} ({llm.tiers[name].adapter}, {retention[llm.tiers[name].zero_data_retention]})"
        for name in llm.document_tiers
    ]
    checks = [
        Check(
            "documents", OK, "only " + ("; ".join(listed) or "no tier") + " may receive documents"
        )
    ]
    default = llm.jobs.get("default")
    for job in sorted(DOCUMENT_JOB_TYPES):
        tier = llm.jobs.get(job, default)
        if tier is None or tier == LLM_DETERMINISTIC or tier in llm.document_tiers:
            continue
        checks.append(
            Check(
                f"documents {job}",
                MISSING,
                f"routes to tier {tier!r}, which [llm].document_tiers does not allow; "
                "every call would be refused",
            )
        )
    return checks


def _books_check(cfg: TenantConfig) -> Check:
    """[books] (docs/tenant-kit-design.md, section 4). Nothing reads it yet,
    so an unset entity is `skip`; the section itself already loaded or the
    tenant config line above would say so."""
    books = cfg.books
    if books.entity is None:
        return Check("books", SKIP, "no [books].entity yet: the onboarding agent asks")
    filed = f", files {books.tax_return}" if books.tax_return else ", files no Form 990"
    code = books.cost_object
    pattern = f" ({code.pattern})" if code.pattern else " (no pattern yet)"
    return Check("books", OK, f"{books.system}, {books.entity}{filed}; {code.label} codes{pattern}")


def _kit_checks(cfg: TenantConfig, directory: Path) -> list[Check]:
    """The tenant kit (docs/tenant-kit-design.md). Nothing reads it yet, so an
    absent part is `skip`; a part that is present must load, because a broken
    file should be found now and not on the day a lane starts reading it."""
    shape = cfg.identity.shape
    checks = [
        Check("kit shape", OK, shape)
        if shape
        else Check(
            "kit shape",
            SKIP,
            "no [identity].shape: this tenant predates the kit (`engine init` renders one)",
        )
    ]
    for part, name in KIT_FILES.items():
        if not (directory / name).is_file():
            checks.append(Check(f"kit {part}", SKIP, f"no {name}: nothing reads it yet"))
            continue
        try:
            load_part(directory, part)
        except KitError as exc:
            checks.append(Check(f"kit {part}", MISSING, str(exc)))
            continue
        detail = f"{name} loads"
        if part == "authority" and not (load_part(directory, part) or {}).get("people"):
            detail += (
                "; no one under [people] yet, so every card waits (`engine onboard` names them)"
            )
        if part == "authority" and cfg.approval.auto_file_under > 0:
            # Read by no code; under authority.toml an agent grant says it (#435).
            detail += (
                f"; [approval].auto_file_under = {cfg.approval.auto_file_under:g} does nothing "
                "here (a grant like approve:ap.invoice<=N on the intake agent's role says it)"
            )
        checks.append(Check(f"kit {part}", OK, detail))
    return checks


def _clock_check(cfg: TenantConfig) -> Check:
    """The tenant's zone must be a real IANA name: every local date the engine
    computes goes through it, and the read server dies on start without one
    (Tim walkthrough 1, 2026-10-09: onboarding stored "nepal" as typed)."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    tz = cfg.identity.timezone
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        return Check(
            "time zone",
            MISSING,
            f"[identity].timezone = {tz!r} is not a time zone name; use one such as "
            "America/New_York or Asia/Kathmandu",
        )
    return Check("time zone", OK, tz)


def run_doctor(
    slug: str,
    *,
    tenants_root: str | Path | None = None,
    env: dict[str, str] | None = None,
    repo: str | Path | None = None,
    create_folders: bool = False,
) -> DoctorReport:
    """Every check for ``slug`` on this host. Reads; writes nothing, except
    that ``create_folders`` makes each configured folder that does not exist
    yet (a plain checkout of the demo has none, issue #3). It never touches
    a path that exists, folder or not."""
    environment = dict(os.environ if env is None else env)
    root = Path(tenants_root) if tenants_root is not None else None
    code = Path(repo) if repo is not None else Path(__file__).resolve().parents[2]
    path = tenant_dir(slug, tenants_root=root) / "tenant.toml"
    try:
        cfg = load_tenant(slug, tenants_root=root)
    except TenantNotFoundError:
        raise
    except ValueError as exc:
        # The file is there and does not load: a bad value, or row 7.13's eval
        # gate refusing a tier with no green results. That is one missing item
        # with a message that names the fix, not a stack trace and not exit 2.
        return DoctorReport(
            tenant=slug,
            checks=[Check("tenant config", MISSING, f"{path} does not load: {exc}")],
        )
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    checks = [Check("tenant config", OK, f"{path} loads ({cfg.identity.legal_name})")]
    checks.append(_clock_check(cfg))
    # One probe, read by both: the per-secret lines say where each value comes
    # from, and the file's own two lines come after them (row 7.22).
    secrets = probe_secrets(slug, tenants_root=root, env=environment)
    checks += _secret_checks(cfg, environment, secrets.names)
    checks += _secrets_file_checks(cfg, environment, secrets)
    checks += _tier_checks(cfg, environment)
    checks += _document_checks(cfg)
    checks += _kit_checks(cfg, path.parent)
    checks.append(_books_check(cfg))
    checks.append(_qbo_check(cfg))
    checks.append(_mail_check(cfg))
    checks += _folder_checks(cfg, raw, create=create_folders)
    checks += _ledger_checks(cfg, slug, environment)
    checks.append(_dead_man_check(cfg, environment))
    checks += _scheduler_checks(cfg, environment, code)
    checks.append(_eval_gate_check(cfg))
    checks.append(_one_tenant_check(slug, cfg, secrets, root, environment))
    return DoctorReport(tenant=slug, checks=checks)


__all__ = ["Check", "DoctorReport", "TenantNotFoundError", "run_doctor"]
