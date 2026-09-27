"""``engine init <slug> [--archetype A|B|C]``: a tenant's first hour.

Renders ``tenants/<slug>/`` (``tenant.toml``, ``vendors.toml``,
``secrets.ref``) from ``tenants/_templates/`` with the archetype's knobs
(``archetypes.toml``), creates the data tree the daily loop expects under a
data root beside the tenants root, opens a fresh ledger (migrated to the
current schema, git-initialized, one first commit), and runs the first
``auditor run <slug> --local-only`` as a subprocess, so ``core/`` keeps
importing nothing from ``auditor/`` and the owner sees the same command they
will run every night.

Every path written into ``tenant.toml`` is RELATIVE to the directory the
engine runs from (the data root is written as its path relative to the
current working directory). Nothing here names a business, a host, or an
absolute path; the templates pass the bleed-through lint in CI.

Substitution is ``string.Template``: a placeholder the template names and
the knobs do not supply is a hard error, never a silent blank.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from string import Template

from ..ledger import Ledger
from .config import TenantConfig, default_tenants_root, load_tenant
from .runner import resolve_ledger_root

ARCHETYPES = ("A", "B", "C")
"""The design archetypes (docs/archetypes.md): A project-coded technical
services, B construction subs and field-service trades, C agencies with
nonprofits as the alternate."""

SLUG_RE = re.compile(r"^[a-z0-9-]+$")
"""A slug is a directory name and an environment-variable prefix: lowercase
letters, digits, and hyphens only."""

TEMPLATE_FILES = {
    "tenant.toml": "tenant.toml.tmpl",
    "vendors.toml": "vendors.toml.tmpl",
    "secrets.ref": "secrets.ref.tmpl",
}
ARCHETYPES_FILE = "archetypes.toml"
DEFAULT_TIMEZONE = "America/New_York"


class InitError(ValueError):
    """A refusal: nothing was written."""


@dataclass
class InitResult:
    slug: str
    archetype: str
    tenant_dir: Path
    data_root: Path
    ledger_root: Path
    files: list[Path] = field(default_factory=list)
    folders: list[Path] = field(default_factory=list)
    audit_ran: bool = False
    audit_ok: bool = False
    audit_output: str = ""
    report_path: Path | None = None


def templates_dir() -> Path:
    """``tenants/_templates`` in THIS repository (never the tenants root,
    which ``ENGINE_TENANTS_ROOT`` may move elsewhere)."""
    return Path(__file__).resolve().parents[2] / "tenants" / "_templates"


def env_prefix(slug: str) -> str:
    """``acme-safety`` -> ``ACME_SAFETY``; a leading digit gets a ``T_`` so
    the result is always a legal environment-variable name."""
    prefix = slug.upper().replace("-", "_")
    return prefix if re.match(r"[A-Z]", prefix) else f"T_{prefix}"


def default_legal_name(slug: str) -> str:
    return " ".join(part.capitalize() for part in slug.split("-") if part) + " Inc."


def _validate(slug: str, archetype: str) -> None:
    if not slug or not SLUG_RE.match(slug):
        raise InitError(
            f"slug {slug!r} is not a tenant slug: use lowercase letters, digits, and hyphens only"
        )
    if archetype not in ARCHETYPES:
        raise InitError(f"archetype {archetype!r} is not one of {', '.join(ARCHETYPES)}")


def load_archetypes() -> dict[str, dict]:
    with (templates_dir() / ARCHETYPES_FILE).open("rb") as handle:
        return tomllib.load(handle)


def render(
    slug: str,
    archetype: str,
    *,
    legal_name: str = "",
    timezone: str = DEFAULT_TIMEZONE,
    fiscal_year_start: int = 1,
    data_root_rel: str,
) -> dict[str, str]:
    """The three tenant files as text, keyed by their file name.

    Pure: reads the templates, writes nothing. ``data_root_rel`` is the data
    root as the engine will see it from its working directory.
    """
    _validate(slug, archetype)
    knobs = load_archetypes()[archetype]
    categories = "\n".join(f'{cat} = "{acct}"' for cat, acct in knobs["category_accounts"].items())
    values = {
        "slug": slug,
        "legal_name": legal_name or default_legal_name(slug),
        "timezone": timezone,
        "fiscal_year_start": int(fiscal_year_start),
        "data_root": data_root_rel,
        "env_prefix": env_prefix(slug),
        "archetype": archetype,
        "archetype_name": knobs["name"],
        "unit_label": knobs["unit_label"],
        "unit_lower": str(knobs["unit_label"]).lower(),
        "project_account_template": knobs["project_account_template"],
        "category_accounts": categories,
        "archetype_notes": str(knobs["notes"]).strip("\n") + "\n",
    }
    out: dict[str, str] = {}
    for name, template_name in TEMPLATE_FILES.items():
        text = (templates_dir() / template_name).read_text(encoding="utf-8")
        out[name] = Template(text).substitute(values)
    return out


def folders_for(cfg: TenantConfig, raw: dict) -> list[str]:
    """The folder skeleton the daily loop and the auditor expect, read back
    from the rendered config so the tree can never drift from the file."""
    auditor = raw.get("auditor", {})
    candidates = [
        cfg.ap.landing_dir,
        cfg.ap.filing_dir,
        str(Path(cfg.ap.workbook_path).parent) if cfg.ap.workbook_path else "",
        cfg.timesheets.landing_dir,
        cfg.timesheets.filing_dir,
        cfg.expenses.drop_dir,
        cfg.expenses.inbox_dir,
        cfg.expenses.filing_dir,
        cfg.mail.landing_dir,
        cfg.bank_csv.statement_dir,
        cfg.w9.folder,
        cfg.close.report_dir,
        cfg.close.expenses_dir,
        str(auditor.get("report_dir", "")),
        str(auditor.get("triage", {}).get("notes_dir", "")),
    ]
    if cfg.expenses.drop_dir:
        candidates.extend(str(Path(cfg.expenses.drop_dir) / p.name) for p in cfg.expenses.persons)
    seen: list[str] = []
    for folder in candidates:
        if folder and folder not in seen:
            seen.append(folder)
    return seen


def _first_audit(
    slug: str, root: Path, ledger_dir: str | Path | None, store_dir: str | Path | None
) -> tuple[bool, str, Path | None]:
    """``auditor run <slug> --local-only`` as the owner would run it, from
    the same working directory, with the same ledger and store roots."""
    cmd = [sys.executable, "-m", "auditor.cli", "run", slug, "--tenants-dir", str(root)]
    if ledger_dir is not None:
        cmd += ["--ledger-dir", str(ledger_dir)]
    if store_dir is not None:
        cmd += ["--store-dir", str(store_dir)]
    cmd.append("--local-only")
    env = dict(os.environ)
    repo = str(Path(__file__).resolve().parents[2])
    env["PYTHONPATH"] = repo + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, check=False)
    output = proc.stdout + proc.stderr
    report: Path | None = None
    for line in proc.stdout.splitlines():
        if line.strip().startswith("report:"):
            report = Path(line.split("report:", 1)[1].strip())
    return proc.returncode == 0, output, report


def init_tenant(
    slug: str,
    *,
    archetype: str = "A",
    root: str | Path | None = None,
    data_root: str | Path | None = None,
    legal_name: str = "",
    timezone: str = DEFAULT_TIMEZONE,
    fiscal_year_start: int = 1,
    ledger_dir: str | Path | None = None,
    store_dir: str | Path | None = None,
    run_audit: bool = True,
) -> InitResult:
    """Create ``tenants/<slug>/``, its data tree, and its ledger; run the
    first audit. Refuses, touching nothing, when the slug is malformed, the
    archetype unknown, or the tenant directory or its ledger already exists.
    """
    _validate(slug, archetype)
    tenants_root = Path(root) if root is not None else default_tenants_root()
    tenant_dir = tenants_root / slug
    if tenant_dir.exists():
        raise InitError(f"tenant {slug!r} already exists at {tenant_dir}; nothing was written")
    ledger_root = resolve_ledger_root(slug, ledger_dir)
    if ledger_root.exists():
        raise InitError(
            f"a ledger for {slug!r} already exists at {ledger_root}; nothing was written"
        )
    data_dir = Path(data_root) if data_root is not None else tenants_root.parent / f"{slug}-data"
    data_root_rel = Path(os.path.relpath(data_dir, Path.cwd())).as_posix()

    files = render(
        slug,
        archetype,
        legal_name=legal_name,
        timezone=timezone,
        fiscal_year_start=fiscal_year_start,
        data_root_rel=data_root_rel,
    )
    tenant_dir.mkdir(parents=True)
    written: list[Path] = []
    for name, text in files.items():
        path = tenant_dir / name
        path.write_text(text, encoding="utf-8")
        written.append(path)

    # Prove the rendered file loads before building anything on it.
    cfg = load_tenant(slug, tenants_root=tenants_root)
    with (tenant_dir / "tenant.toml").open("rb") as handle:
        raw = tomllib.load(handle)
    folders: list[Path] = []
    for folder in folders_for(cfg, raw):
        path = Path(folder)
        path.mkdir(parents=True, exist_ok=True)
        folders.append(path)

    with Ledger.open(ledger_root) as ledger:
        ledger.commit(
            agent="engine",
            job="init",
            idempotency_key=f"init-{slug}",
            summary=f"ledger created for {slug} (archetype {archetype})",
        )

    result = InitResult(
        slug=slug,
        archetype=archetype,
        tenant_dir=tenant_dir,
        data_root=data_dir,
        ledger_root=ledger_root,
        files=written,
        folders=folders,
    )
    if run_audit:
        ok, output, report = _first_audit(slug, tenants_root, ledger_dir, store_dir)
        result.audit_ran = True
        result.audit_ok = ok
        result.audit_output = output
        result.report_path = report
    return result
