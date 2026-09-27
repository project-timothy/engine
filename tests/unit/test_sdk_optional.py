"""Row 7.15: the Claude Agent SDK is the optional extra ``[claude]``.

``pip install .[claude]`` (``uv sync --extra claude``) installs
``claude-agent-sdk``; the core imports it lazily inside the SDK adapters
only; the whole suite passes in an environment without it. Every site that
calls the SDK raises one typed error naming the extra when it is absent,
never a bare ImportError at call time.

Four of these tests stand in for the absent SDK by planting ``None`` in
``sys.modules`` (the interpreter then refuses the import with
``ModuleNotFoundError``); the walk-every-module test does the same in a
subprocess so nothing already imported in this process can mask a
module-level import. A Mac host keeps the SDK: the scheduled entry
scripts and the installer sync with the extra, and this file pins that so
the deploy clone never loses it at a fast-forward.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EXTRA = "claude"
PIN = re.compile(r"claude-agent-sdk==(\d+)\.(\d+)\.(\d+)")
NAMES_THE_EXTRA = "[claude]"


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _block_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``import claude_agent_sdk`` fail in this process, installed or not."""
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)


# -- packaging -------------------------------------------------------------------


def test_the_sdk_is_the_claude_extra_with_an_exact_pin():
    data = _pyproject()["project"]
    assert not [d for d in data["dependencies"] if d.startswith("claude-agent-sdk")], (
        "claude-agent-sdk is the [claude] extra, not a core dependency (row 7.15)"
    )
    extra = data["optional-dependencies"][EXTRA]
    assert len(extra) == 1 and PIN.fullmatch(extra[0].strip()), (
        f"[project.optional-dependencies] {EXTRA} must be exactly one == pin, got {extra}"
    )


def test_uv_lock_carries_the_sdk_under_the_extra_not_the_dependencies():
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    name = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["name"]
    [project] = [p for p in lock["package"] if p["name"] == name]
    assert "claude-agent-sdk" not in {d["name"] for d in project["dependencies"]}
    optional = project["optional-dependencies"][EXTRA]
    assert {d["name"] for d in optional} == {"claude-agent-sdk"}


# -- import-time independence -----------------------------------------------------

_WALK = """
import importlib, pkgutil, sys
sys.modules["claude_agent_sdk"] = None  # any import of it now raises ModuleNotFoundError
failures = []
def onerror(name):
    failures.append(f"{name}: {sys.exc_info()[1]!r}")
for pkg in ("core", "auditor"):
    root = importlib.import_module(pkg)
    for info in pkgutil.walk_packages(root.__path__, prefix=pkg + ".", onerror=onerror):
        try:
            importlib.import_module(info.name)
        except Exception as exc:
            failures.append(f"{info.name}: {exc!r}")
print("\\n".join(failures))
sys.exit(1 if failures else 0)
"""


def test_every_core_and_auditor_module_imports_with_the_sdk_absent():
    """``python -c "import core"`` and every module under core/ and auditor/
    load without the SDK: the import lives inside the function or class that
    uses it, at every site."""
    proc = subprocess.run(
        [sys.executable, "-c", _WALK], cwd=ROOT, capture_output=True, text=True, timeout=300
    )
    assert proc.returncode == 0, f"modules that need the SDK at import:\n{proc.stdout}{proc.stderr}"


def test_the_sdk_cli_pair_test_skips_naming_the_extra_when_the_sdk_is_absent():
    """The pin-floor test is about the installed pair; without the SDK it
    skips (never fails) and the reason says which extra to install."""
    script = (
        "import sys, pytest\n"
        "sys.modules['claude_agent_sdk'] = None\n"
        "sys.exit(pytest.main(['-q', '-rs', '-p', 'no:cacheprovider',"
        " 'tests/unit/test_sdk_cli_pair.py']))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True, timeout=300
    )
    out = proc.stdout + proc.stderr
    # 0 = ran and passed nothing else; 5 = pytest's "no tests collected" for a
    # module-level skip (in the full suite other files are collected, so 0).
    assert proc.returncode in (0, 5), out
    assert "1 skipped" in out and "passed" not in out, out
    assert NAMES_THE_EXTRA in out, f"the skip reason must name the extra:\n{out}"


# -- the call sites, one eval each -------------------------------------------------


def _seat_ctx(tmp_path):
    """A tenant whose model jobs are served by the seat tier, which is the SDK
    adapter: exactly the live shape, so blocking the import reproduces a host
    that lost the extra."""
    from core.engine.config import TenantConfig
    from core.engine.contracts import JobContext
    from core.ledger import Ledger

    tenant = TenantConfig.model_validate(
        {
            "identity": {"legal_name": "SDK Optional Test Co", "slug": "demo"},
            "llm": {
                "tiers": {
                    "seat": {
                        "adapter": "claude_agent_sdk",
                        "model": "default",
                        "pricing": {"input_usd_per_mtok": "0", "output_usd_per_mtok": "0"},
                    }
                },
                "jobs": {
                    "invoice_extract": "seat",
                    "inbox_classify": "seat",
                    "scan_group": "seat",
                },
            },
        }
    )
    return JobContext(
        tenant=tenant,
        tenant_slug="demo",
        ledger=Ledger.open(tmp_path / "ledger"),
        agent="ap",
        job="intake",
        run_key="demo.ap.intake.k1",
    )


def test_ap_extraction_maps_a_missing_sdk_to_a_terminal_extraction_error(tmp_path, monkeypatch):
    """The seat tier with the extra missing: the document is an
    ``sdk_missing`` extraction failure, non-transient (a redial cannot install
    a package), and the message names the extra. Retargeted for row 7.10:
    ``ClaudeExtractor`` became ``GatewayExtractor`` over the SDK adapter, and
    the contract this pins is unchanged."""
    from core.agents.ap.extraction import ExtractionError, GatewayExtractor

    _block_sdk(monkeypatch)
    doc = tmp_path / "invoice.pdf"
    doc.write_bytes(b"%PDF-1.4\n%%EOF\n")
    with pytest.raises(ExtractionError) as info:
        GatewayExtractor(_seat_ctx(tmp_path), text_reader=lambda _p: "text").extract(doc)
    assert info.value.cause == "sdk_missing"
    assert info.value.transient is False
    assert NAMES_THE_EXTRA in str(info.value)


def test_ap_build_extractor_claude_never_redials_a_missing_sdk(tmp_path, monkeypatch):
    from core.agents.ap.extraction import (
        ExtractionError,
        GatewayExtractor,
        RetryingExtractor,
        build_extractor,
    )

    _block_sdk(monkeypatch)
    ctx = _seat_ctx(tmp_path)
    live = build_extractor("claude", ctx)
    assert isinstance(live, RetryingExtractor)
    doc = tmp_path / "invoice.pdf"
    doc.write_bytes(b"%PDF-1.4\n%%EOF\n")
    sleeps: list[float] = []
    inner = GatewayExtractor(ctx, text_reader=lambda _p: "text")
    with pytest.raises(ExtractionError) as info:
        RetryingExtractor(inner, sleeper=sleeps.append).extract(doc)
    assert info.value.cause == "sdk_missing"
    assert sleeps == [], "a terminal sdk_missing error must not be retried"


def test_expenses_inbox_classifier_raises_sdk_missing(tmp_path, monkeypatch):
    """The seat tier with the extra missing: classification fails naming the
    extra, and never quietly labels the photo. Retargeted for row 7.11:
    ``ClaudeInboxClassifier`` became ``GatewayInboxClassifier`` over the SDK
    adapter, so the typed error is the adapter's non-transient
    ``sdk_missing`` transport failure; the contract this pins (a missing
    package names the extra and stops the job) is unchanged."""
    from core.agents.expenses.inbox import GatewayInboxClassifier
    from core.llm import GatewayTransportError

    _block_sdk(monkeypatch)
    photo = tmp_path / "IMG_0001.jpg"
    photo.write_bytes(b"\xff\xd8\xff\xd9")
    with pytest.raises(GatewayTransportError) as info:
        GatewayInboxClassifier(_seat_ctx(tmp_path)).classify(photo)
    assert info.value.cause == "sdk_missing"
    assert info.value.transient is False
    assert NAMES_THE_EXTRA in str(info.value)


def test_expenses_scan_grouper_raises_sdk_missing_not_a_grouping_error(tmp_path, monkeypatch):
    """A missing package is a deployment fault, not a per-file grouping
    fault: it must not be swallowed into the held-file anomaly path.
    Retargeted for row 7.11 (``ClaudeGrouper`` became ``GatewayGrouper``
    over the SDK adapter); the contract is the same one, and the gateway's
    ``sdk_missing`` cause is what the grouper now refuses to convert."""
    from core.agents.expenses.scan_split import GatewayGrouper, GroupingError
    from core.llm import GatewayTransportError

    _block_sdk(monkeypatch)
    scan = tmp_path / "combined.pdf"
    scan.write_bytes(b"%PDF-1.4\n%%EOF\n")
    with pytest.raises(GatewayTransportError) as info:
        GatewayGrouper(_seat_ctx(tmp_path)).propose_groups(scan, 3)
    assert not isinstance(info.value, GroupingError)
    assert info.value.cause == "sdk_missing"
    assert NAMES_THE_EXTRA in str(info.value)


def test_sdk_runner_seam_raises_sdk_missing_and_the_runner_still_skips(tmp_path, monkeypatch):
    from core.llm.adapters import claude_sdk
    from core.llm.sdk import SdkMissing

    _block_sdk(monkeypatch)
    with pytest.raises(SdkMissing) as info:
        claude_sdk.sdk()
    assert NAMES_THE_EXTRA in str(info.value)
    assert isinstance(info.value, ModuleNotFoundError), (
        "SdkMissing must stay a ModuleNotFoundError so the runner's SKIPPED precondition holds"
    )


def test_auditor_advisory_drafter_has_its_own_sdk_missing(monkeypatch):
    """The auditor imports nothing from core (independence lint): its
    drafter carries its own two-line lazy import and error, and the runner's
    existing fallback catches it as any other exception."""
    from auditor.advisory import draft
    from core.llm.sdk import SdkMissing as CoreSdkMissing

    _block_sdk(monkeypatch)
    with pytest.raises(draft.SdkMissing) as info:
        draft.draft_counsel({"aging": {"open_payables": 0}})
    assert NAMES_THE_EXTRA in str(info.value)
    assert issubclass(draft.SdkMissing, ModuleNotFoundError)
    assert draft.SdkMissing is not CoreSdkMissing


# -- CI and a Mac host keep their environments --------------------------------------


def test_ci_runs_the_suite_with_and_without_the_extra():
    """Retargeted twice, intent unchanged: exactly two FULL-SUITE runs, one per
    environment (with the claude extra, and without it).

    Row 7.22 moved the count off `uv run pytest` appearing twice, because the
    container job runs pytest a third time on ONE file (the sops cases this Mac
    skips). The 2026-09-18 code-quality pass then wrapped the with-extra run in
    `coverage run` so the coverage floor is enforced without paying for a third
    four-minute suite, so a full-suite run is now spelled either way."""
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    syncs = [line.strip() for line in ci.splitlines() if "uv sync" in line]
    assert "run: uv sync --locked --extra claude" in syncs, syncs
    assert "run: uv sync --locked" in syncs, f"a job must sync WITHOUT the extra: {syncs}"
    whole_suite = [
        line.strip()
        for line in ci.splitlines()
        if ("uv run pytest" in line or "uv run coverage run -m pytest" in line)
        and "tests/" not in line
        and "core/" not in line
    ]
    assert len(whole_suite) == 2, f"both jobs run the full suite: {whole_suite}"
    assert ci.count("uv run ruff check") == 1, "the lints run once, not per job"


@pytest.mark.parametrize(
    "script",
    ["scripts/engine-ap-daily.sh", "scripts/auditor-nightly.sh", "scripts/engine-jobs-resume.sh"],
    ids=["engine", "auditor", "retries"],
)
def test_scheduled_entry_scripts_sync_with_the_extra(script):
    """``uv run`` syncs the deploy clone's environment before every job;
    without ``--extra claude`` a fresh environment would come up without the
    SDK and the 08:00 run would flag every document ``sdk_missing``.

    Retargeted by row 7.21, intent unchanged. The flag moved out of the job
    lines and into ``uv_run`` (scripts/lib/uv-run.sh), because the container
    installs without the extra and must not ask uv to resolve it. So this
    pins the two halves that reproduce the old string: every job line goes
    through the wrapper, and the wrapper's default is ``claude``. What the
    Mac's argv actually comes out as, under zsh and bash, with the knob set
    and unset, is proven in
    tests/unit/test_scheduled_scripts_linux.py::test_the_mac_default_extra_is_still_the_claude_extra
    and ::test_the_knob_names_the_extra_because_zsh_would_not_split_a_flag_string.
    """
    body = (ROOT / script).read_text(encoding="utf-8")
    runs = [line for line in body.splitlines() if re.search(r"^\s*(uv_run|\S+=\$\(uv_run)\b", line)]
    assert runs, f"{script}: no uv_run lines found"
    bare = [line for line in body.splitlines() if re.search(r'"\$UV" run\b', line)]
    assert not bare, f"{script}: a uv run that bypasses uv_run:\n" + "\n".join(bare)
    assert '. "$REPO/scripts/lib/uv-run.sh"' in body or (
        'source "$REPO/scripts/lib/uv-run.sh"' in body
    ), f"{script}: does not source the uv_run wrapper"


def test_the_uv_run_wrapper_defaults_to_the_claude_extra():
    """The one line that decides what the Mac syncs at 02:00 and 08:00."""
    lib = (ROOT / "scripts/lib/uv-run.sh").read_text(encoding="utf-8")
    assert 'else UV_EXTRA="claude"; fi' in lib
    assert '"$UV" run --extra "$UV_EXTRA" "$@"' in lib
