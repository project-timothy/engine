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
crontab names.
"""

from __future__ import annotations

import os
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .config import (
    LLM_DETERMINISTIC,
    TenantConfig,
    TenantNotFoundError,
    load_tenant,
    tenant_dir,
)
from .init import folders_for
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
    owed = [
        key
        for key in ("tenant_id", "keychain_service", "keychain_account", "landing_dir")
        if not getattr(cfg.mail, key)
    ]
    if owed:
        return Check("mailbox", MISSING, "[mail] is configured but empty: " + ", ".join(owed))
    return Check(
        "mailbox",
        OK,
        f"app {cfg.mail.client_id} with a token cache at {cfg.mail.keychain_service}"
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
    checks = [Check("ledger", OK, str(root))]
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
    # One probe, read by both: the per-secret lines say where each value comes
    # from, and the file's own two lines come after them (row 7.22).
    secrets = probe_secrets(slug, tenants_root=root, env=environment)
    checks += _secret_checks(cfg, environment, secrets.names)
    checks += _secrets_file_checks(cfg, environment, secrets)
    checks += _tier_checks(cfg, environment)
    checks.append(_qbo_check(cfg))
    checks.append(_mail_check(cfg))
    checks += _folder_checks(cfg, raw, create=create_folders)
    checks += _ledger_checks(cfg, slug, environment)
    checks.append(_dead_man_check(cfg, environment))
    checks += _scheduler_checks(cfg, environment, code)
    checks.append(_eval_gate_check(cfg))
    return DoctorReport(tenant=slug, checks=checks)


__all__ = ["Check", "DoctorReport", "TenantNotFoundError", "run_doctor"]
