"""``engine init <slug> [--archetype A|B|C]`` (phase 7 row 7.19, issue #228).

A new tenant is rendered from ``tenants/_templates/``: ``tenant.toml`` from
the archetype knobs, ``vendors.toml``, ``secrets.ref``, a fresh migrated
git-initialized ledger, the data tree the daily loop expects (relative
paths under ``--data-root``), and a first ``auditor run --local-only``.
The demo tenant in the repo IS the rendered archetype A, so one source of
truth exists for what a tenant looks like.

Every path the generated config names is relative to the engine's working
directory (the data root sits beside the tenants root by default), so the
tests chdir into a temp directory and point ``ENGINE_TENANTS_ROOT`` at it.
"""

from __future__ import annotations

import json
import re
import sqlite3
import tomllib
from pathlib import Path

import pytest

from auditor.cli import main as auditor_main
from auditor.config import load_auditor_tenant
from core.engine.cli import main as engine_main
from core.engine.config import load_tenant
from core.engine.init import ARCHETYPES, InitError, init_tenant, render
from core.engine.runner import resolve_ledger_root, run
from core.evals.bleedthrough_lint import load_tokens, scan
from core.ledger.schema import MIGRATIONS

REPO = Path(__file__).resolve().parents[2]
DEMO_DIR = REPO / "tenants" / "demo"
TEMPLATES_DIR = REPO / "tenants" / "_templates"

# The demo tenant's three identity facts (pinned by other tests: the
# fiscal calendar in test_config, the bank format in test_bank_csv).
DEMO_RENDER = dict(
    legal_name="Demo Tenant Inc.",
    timezone="America/Chicago",
    fiscal_year_start=7,
)

# The keys an archetype changes (docs/archetypes.md;
# docs/decisions/2026-09-15-engine-init-templates.md). Anything else that
# differs across A, B, and C is drift.
ARCHETYPE_DIFF_KEYS = {
    "identity.archetype",
    "ap.workbook_columns",
    "expenses.project_account_template",
    "expenses.category_accounts",
}


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A working directory with a tenants root beside a data root, and the
    engine's tenant lookup pointed at it (jobs read vendors.toml through the
    tenants root, not through ``run(tenants_root=...)``)."""
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "tenants"
    monkeypatch.setenv("ENGINE_TENANTS_ROOT", str(root))
    monkeypatch.setenv("AUDITOR_TENANTS_DIR", str(root))
    monkeypatch.setenv("ENGINE_LEDGER_ROOT", str(tmp_path / "ledger"))
    monkeypatch.setenv("AUDITOR_STORE_ROOT", str(tmp_path / "store"))
    monkeypatch.delenv("ENGINE_EXTRACTOR", raising=False)
    return tmp_path, root


def _init(root: Path, slug: str = "acme", archetype: str = "A", **kw):
    return init_tenant(slug, archetype=archetype, root=root, run_audit=False, **kw)


# Tables compared as one value: the whole map is the archetype's chart style.
LEAF_TABLES = {"expenses.category_accounts"}


def _flatten(data: dict, prefix: str = "") -> dict[str, object]:
    flat: dict[str, object] = {}
    for key, value in data.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict) and dotted not in LEAF_TABLES:
            flat.update(_flatten(value, f"{dotted}."))
        else:
            flat[dotted] = json.dumps(value, sort_keys=True, default=str)
    return flat


def _daily_loop(slug: str, ledger_dir: Path, evidence: Path) -> list[tuple[str, str, str]]:
    """The offline daily loop on fixtures, in the 08:00 order: the fixture
    extractor for every model seat and a replay file for the accounting
    system, exactly as the suite drives the demo tenant."""
    params = {
        ("ap", "intake"): {"extractor": "fixture"},
        ("ap", "reconcile"): {"evidence_file": str(evidence)},
        ("expenses", "extract"): {"extractor": "fixture"},
        ("ap", "janitor"): {"days": "10"},
    }
    expected = load_auditor_tenant(slug).expected_daily_jobs
    out = []
    for agent, job in expected:
        result = run(slug, agent, job, params=params.get((agent, job), {}), ledger_dir=ledger_dir)
        out.append(
            (
                agent,
                job,
                result.status + ("" if result.status != "error" else f": {result.summary}"),
            )
        )
    return out


# ---- the generated tenant loads, runs, and audits -----------------------------


def test_generated_tenant_loads_and_names_its_archetype(world):
    _tmp, root = world
    result = _init(root)
    cfg = load_tenant("acme", tenants_root=root)
    assert cfg.identity.slug == "acme"
    assert result.tenant_dir == root / "acme"
    raw = tomllib.loads((root / "acme" / "tenant.toml").read_text())
    assert raw["identity"]["archetype"] == "A"
    assert cfg.llm.tiers and all(t.adapter == "fixture" for t in cfg.llm.tiers.values())


def test_full_daily_loop_completes_green_on_fixtures(world):
    tmp, root = world
    _init(root)
    evidence = tmp / "evidence.json"
    evidence.write_text("[]")
    statuses = _daily_loop("acme", tmp / "ledger", evidence)
    assert statuses, "the generated [auditor].expected_daily_jobs is not empty"
    bad = [s for s in statuses if s[2] not in ("ok", "noop")]
    assert bad == [], bad
    assert [(a, j) for a, j, _ in statuses] == [
        ("ap", "intake"),
        ("ap", "apply"),
        ("ap", "qbo-push"),
        ("ap", "reconcile"),
        ("ap", "workbook"),
        ("timesheets", "intake"),
        ("expenses", "inbox"),
        ("expenses", "intake"),
        ("expenses", "extract"),
        ("ap", "janitor"),
        ("deadlines", "scan"),
        ("deadlines", "calendar"),
        ("brief", "weekly"),
    ]


def test_auditor_local_only_completes_green_after_the_loop(world, capsys):
    tmp, root = world
    _init(root)
    evidence = tmp / "evidence.json"
    evidence.write_text("[]")
    _daily_loop("acme", tmp / "ledger", evidence)
    code = auditor_main(["run", "acme", "--local-only"])
    out = capsys.readouterr()
    assert code == 0, out.err
    assert "lens error" not in out.err
    report_dir = Path(load_auditor_tenant("acme").report_dir)
    assert not report_dir.is_absolute()
    assert list(report_dir.glob("audit-*.md")), "the report lands where [auditor].report_dir says"


def test_first_audit_runs_and_the_cli_prints_the_report_path(world, capsys):
    tmp, root = world
    code = engine_main(["init", "acme", "--archetype", "B"])
    out = capsys.readouterr().out
    assert code == 0
    report_dir = tmp / "acme-data" / "reports" / "_auditor"
    reports = list(report_dir.glob("audit-*.md"))
    assert len(reports) == 1, "init runs one first audit"
    assert str(reports[0]) in out or str(reports[0].relative_to(tmp)) in out
    assert "tenants/acme" in out.replace(str(tmp) + "/", "")


# ---- the ledger and the data tree ---------------------------------------------


def test_ledger_is_fresh_migrated_and_git_initialized(world):
    tmp, root = world
    _init(root)
    ledger_root = resolve_ledger_root("acme", tmp / "ledger")
    assert (ledger_root / ".git").is_dir()
    conn = sqlite3.connect(ledger_root / "ledger.sqlite3")
    versions = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    conn.close()
    assert versions == {v for v, _ in MIGRATIONS}, "migrated to the current version"
    import subprocess

    head = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"], cwd=ledger_root, capture_output=True, text=True
    )
    assert head.returncode == 0, "the ledger repo has its first commit"


def test_data_tree_is_the_folder_skeleton_the_config_names(world):
    tmp, root = world
    _init(root)
    cfg = load_tenant("acme", tenants_root=root)
    aud = load_auditor_tenant("acme")
    folders = [
        cfg.ap.landing_dir,
        cfg.ap.filing_dir,
        str(Path(cfg.ap.workbook_path).parent),
        cfg.timesheets.landing_dir,
        cfg.timesheets.filing_dir,
        cfg.expenses.drop_dir,
        cfg.expenses.inbox_dir,
        cfg.expenses.filing_dir,
        cfg.close.report_dir,
        cfg.close.expenses_dir,
        cfg.w9.folder,
        aud.report_dir,
        aud.triage_notes_dir,
    ]
    for folder in folders:
        assert folder, "every path the daily loop reads is configured"
        assert not Path(folder).is_absolute(), folder
        assert Path(folder).is_dir(), folder
        assert (tmp / "acme-data") in Path(folder).resolve().parents or Path(folder).resolve() == (
            tmp / "acme-data"
        ), f"{folder} lives under the data root"
    for person in cfg.expenses.persons:
        assert (Path(cfg.expenses.drop_dir) / person.name).is_dir()


def test_data_root_flag_moves_the_tree_and_paths_stay_relative(world):
    tmp, root = world
    _init(root, data_root=tmp / "elsewhere" / "acme")
    cfg = load_tenant("acme", tenants_root=root)
    assert cfg.ap.landing_dir.startswith("elsewhere/acme/")
    assert Path(cfg.ap.landing_dir).is_dir()


# ---- refusals ----------------------------------------------------------------


def test_existing_slug_refuses_without_touching_anything(world):
    tmp, root = world
    (root / "acme").mkdir(parents=True)
    marker = root / "acme" / "keep.txt"
    marker.write_text("mine")
    with pytest.raises(InitError, match="exists"):
        _init(root)
    assert sorted(p.name for p in (root / "acme").iterdir()) == ["keep.txt"]
    assert marker.read_text() == "mine"
    assert not (tmp / "acme-data").exists()
    assert not (tmp / "ledger").exists()


@pytest.mark.parametrize("slug", ["Acme", "acme_1", "acme/x", "acme.co", "", "_templates", "a b"])
def test_slug_outside_the_alphabet_refuses(world, slug):
    tmp, root = world
    with pytest.raises(InitError):
        _init(root, slug=slug)
    assert not root.exists() or not list(root.iterdir())
    assert not list(p for p in tmp.iterdir() if p.name.endswith("-data"))


def test_unknown_archetype_refuses(world):
    _tmp, root = world
    with pytest.raises(InitError, match="archetype"):
        _init(root, archetype="D")
    assert not (root / "acme").exists()


def test_cli_refusal_exits_two(world, capsys):
    code = engine_main(["init", "Bad Slug"])
    assert code == 2
    assert "slug" in capsys.readouterr().err


# ---- secrets, paths, bleed-through ---------------------------------------------


def test_secrets_ref_names_environment_variables_only(world):
    _tmp, root = world
    _init(root)
    lines = [
        line.strip()
        for line in (root / "acme" / "secrets.ref").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert lines, "secrets.ref lists the secrets the tenant declares"
    for line in lines:
        assert re.fullmatch(r"[a-z_]+\s*->\s*[A-Z][A-Z0-9_]*", line), line
        assert "=" not in line
    cfg = load_tenant("acme", tenants_root=root)
    assert cfg.secrets, "tenant.toml [secrets] names at least one secret"
    for logical, env_var in cfg.secrets.items():
        assert re.fullmatch(r"[A-Z][A-Z0-9_]*", env_var), (logical, env_var)
        assert env_var.startswith("ACME_")
        assert f"{logical}" in "\n".join(lines)


def test_generated_config_holds_no_absolute_path(world):
    _tmp, root = world
    _init(root)
    text = (root / "acme" / "tenant.toml").read_text()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert not re.search(r'"(/|~|[A-Za-z]:\\)', stripped), line
    flat = _flatten(tomllib.loads(text))
    for key, value in flat.items():
        mac_home = "/Users/"  # bleedthrough: allow (asserts absence)
        assert mac_home not in value and "/home/" not in value, key


def _tokens_in(path: Path) -> list[str]:
    low = path.read_text(encoding="utf-8").lower()
    return [t for t in load_tokens() if t.lower() in low]


def test_generated_files_and_templates_pass_the_bleedthrough_lint(world):
    _tmp, root = world
    for archetype in ARCHETYPES:
        _init(root, slug=f"t-{archetype.lower()}", archetype=archetype)
    assert scan(root=root) == []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        assert _tokens_in(path) == [], path
    assert scan(root=TEMPLATES_DIR) == []
    for path in sorted(p for p in TEMPLATES_DIR.rglob("*") if p.is_file()):
        assert _tokens_in(path) == [], path


def test_the_lint_scans_the_templates_by_default():
    from core.evals.bleedthrough_lint import default_roots

    assert TEMPLATES_DIR in default_roots()


# ---- the archetypes ---------------------------------------------------------------


def test_each_archetype_renders_and_loads(world):
    _tmp, root = world
    for archetype in ARCHETYPES:
        slug = f"t-{archetype.lower()}"
        _init(root, slug=slug, archetype=archetype)
        cfg = load_tenant(slug, tenants_root=root)
        assert cfg.identity.slug == slug
        assert load_auditor_tenant(slug).expected_daily_jobs


def test_archetypes_differ_only_where_the_prd_says():
    rendered = {a: render("acme", a, data_root_rel="acme-data", **DEMO_RENDER) for a in ARCHETYPES}
    flats = {a: _flatten(tomllib.loads(files["tenant.toml"])) for a, files in rendered.items()}
    keys = set().union(*(f.keys() for f in flats.values()))
    differing = {k for k in keys if len({f.get(k) for f in flats.values()}) > 1}
    assert differing == ARCHETYPE_DIFF_KEYS
    # B carries the job vocabulary; A and C the project vocabulary.
    labels = {
        a: [c["label"] for c in tomllib.loads(rendered[a]["tenant.toml"])["ap"]["workbook_columns"]]
        for a in ARCHETYPES
    }
    assert "Job" in labels["B"] and "Project" in labels["A"] and "Project" in labels["C"]
    assert (
        "{number}"
        in tomllib.loads(rendered["B"]["tenant.toml"])["expenses"]["project_account_template"]
    )
    for name in ("vendors.toml", "secrets.ref"):
        assert len({files[name] for files in rendered.values()}) == 1, f"{name} is archetype-free"


# ---- the demo tenant is the rendered archetype A ----------------------------------


def test_demo_tenant_is_the_rendered_archetype_a(tmp_path, monkeypatch):
    """Regenerate with, from the repo root:
    ``uv run engine init demo --archetype A --legal-name "Demo Tenant Inc."
    --timezone America/Chicago --fiscal-year-start 7 --root /tmp/t --no-audit``
    and copy the rendered files (the kit included) over ``tenants/demo/``."""
    monkeypatch.chdir(REPO)
    files = render("demo", "A", data_root_rel="demo-data", **DEMO_RENDER)
    assert set(files) == {
        "tenant.toml",
        "vendors.toml",
        "secrets.ref",
        "obligations.toml",
        "authority.toml",
        "kit/brand.toml",
        "kit/voice.toml",
    }
    for name, content in files.items():
        assert (DEMO_DIR / name).read_text(encoding="utf-8") == content, (
            f"tenants/demo/{name} drifted from the rendered archetype A; regenerate it"
        )
    committed = sorted(
        p.relative_to(DEMO_DIR).as_posix()
        for p in DEMO_DIR.rglob("*")
        if p.is_file() and not p.name.startswith(".")
    )
    assert committed == sorted(files), "the demo carries only what the template renders"


def test_init_into_a_temp_root_reproduces_the_demo_byte_for_byte(world):
    tmp, root = world
    result = _init(root, slug="demo", **DEMO_RENDER)
    for path in result.files:
        name = path.relative_to(result.tenant_dir).as_posix()
        assert path.read_bytes() == (DEMO_DIR / name).read_bytes(), name
