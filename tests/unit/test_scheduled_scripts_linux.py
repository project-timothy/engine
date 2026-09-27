"""The scheduled entry scripts run on Linux (phase 7 row 7.20).

Three scripts under ``scripts/`` are what the host's scheduler executes:
``engine-ap-daily.sh`` (08:00), ``auditor-nightly.sh`` (02:00) and
``ledger-backup.sh`` (23:00). They were written for one Mac and carried its
dialect: a zsh shebang, the zsh-only ``${0:A:h:h}`` path modifier, zsh's
``print -r --``, BSD ``date -v-10d``, a hardcoded Homebrew ``uv``, a
macOS home-directory fallback, and one tenant slug typed into every line. Phase 7
takes the engine to a Linux container (row 7.21), so each of those had to go
or become a branch.

This file is the row's acceptance: both daily scripts execute under ``bash``
against the **demo** tenant, in a scratch runtime clone, with only ``uv``
stubbed, and CI's ubuntu-latest runner is the Linux container that proves it.

It is also the row's safety net. The refactor may not change what the Mac
does at 08:00 and 02:00, so three tests pin the old behaviour against the new
code: the command log is byte-identical under zsh (which is what launchd
still invokes) and under bash; ``SINCE`` equals what ``date -v-10d`` has
always printed; and the repo path the script resolves equals what the zsh
``${0:A:h:h}`` modifier resolved.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from core.engine.config import load_tenant

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
ALL_SHELL = sorted(SCRIPTS.glob("*.sh")) + sorted((SCRIPTS / "lib").glob("*.sh"))

# The PATH launchd pins for the daily jobs (the plist templates under
# the first tenant's launchd plists), widened with a Linux runner's own bin dirs.
PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
LAUNCHD_PATH = "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_CONFIG_NOSYSTEM": "1",
}

# Every uv invocation is appended one argument per line, terminated by a lone
# "--", so an argument carrying spaces is still unambiguous on the way back.
UV_STUB = """#!/bin/sh
for a in "$@"; do printf '%s\\n' "$a" >> "$UV_LOG"; done
printf -- '--\\n' >> "$UV_LOG"
for a in "$@"; do
  if [ "$a" = "python" ]; then printf '%s\\n' "${STUB_STATEMENT_DIR:-}"; break; fi
done
exit 0
"""

CURL_STUB = """#!/bin/sh
for a in "$@"; do printf '%s\\n' "$a" >> "$CURL_LOG"; done
printf -- '--\\n' >> "$CURL_LOG"
exit 0
"""

STATEMENT_READ = (
    "from core.engine.config import load_tenant; print(load_tenant('demo').bank_csv.statement_dir)"
)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        env={"PATH": PATH, "HOME": "/nonexistent", **GIT_ENV},
    )
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result.stdout.strip()


@pytest.fixture
def runtime(tmp_path):
    """A bare origin plus a runtime clone carrying the real scheduled scripts
    at ``scripts/``, the layout the deploy clone has (each script locates its
    own repo from its own path). The freshness guard runs for real against
    this clone: clean, on main, fast-forwardable. Only ``uv`` is a stub."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "-b", "main", ".")

    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-b", "main", ".")
    (seed / "README.md").write_text("engine\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "seed")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "origin", "main")

    clone = tmp_path / "runtime"
    _git(tmp_path, "clone", "-b", "main", str(origin), str(clone))
    (clone / "scripts" / "lib").mkdir(parents=True)
    for path in ALL_SHELL:
        dest = clone / "scripts" / path.relative_to(SCRIPTS)
        shutil.copy(path, dest)
        dest.chmod(0o755)
    _git(clone, "add", "scripts")
    _git(clone, "commit", "-m", "track the scheduled scripts")
    _git(clone, "push", "origin", "main")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "uv").write_text(UV_STUB)
    (bin_dir / "uv").chmod(0o755)
    (bin_dir / "curl").write_text(CURL_STUB)
    (bin_dir / "curl").chmod(0o755)

    home = tmp_path / "home"
    home.mkdir()
    return clone, bin_dir, home


def _env(runtime_fixture, tmp_path, **overrides) -> dict[str, str]:
    _, bin_dir, home = runtime_fixture
    env = {
        "PATH": f"{bin_dir}:{PATH}",
        "HOME": str(home),
        "UV_LOG": str(tmp_path / "uv.log"),
        "STUB_STATEMENT_DIR": str(tmp_path / "statements"),
        "ENGINE_LEDGER_ROOT": str(tmp_path / "ledger"),
        "AUDITOR_STORE_ROOT": str(tmp_path / "auditor"),
        # The tenant the host names (the engine assumes none).
        "ENGINE_TENANT": "acme",
        # hc-ping must never read the real host.env: it holds the ping key.
        "ENGINE_HOST_ENV": str(tmp_path / "absent.env"),
        **GIT_ENV,
    }
    env.update(overrides)
    return env


def _run(script: str, runtime_fixture, tmp_path, shell: str = "bash", **overrides):
    clone, _, _ = runtime_fixture
    return subprocess.run(
        [shell, str(clone / "scripts" / script)],
        capture_output=True,
        text=True,
        env=_env(runtime_fixture, tmp_path, **overrides),
    )


def _uv_calls(tmp_path, name: str = "uv.log") -> list[list[str]]:
    log = tmp_path / name
    if not log.exists():
        return []
    calls: list[list[str]] = []
    current: list[str] = []
    for line in log.read_text().splitlines():
        if line == "--":
            calls.append(current)
            current = []
        else:
            current.append(line)
    return calls


# The one environment every scheduled job asks uv for on this Mac: the
# [claude] extra AND the host dependency group, together, on every call
# (2026-09-17, issue #280). Anything narrower re-syncs the deploy venv and
# strips whatever the narrower set left out.
UNION = ["run", "--extra", "claude", "--group", "host"]


def _engine(*args: str, tenant: str = "demo") -> list[str]:
    return [*UNION, "engine", "run", tenant, *args]


def _since(days: int = 10) -> str:
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")


# ---- syntax ------------------------------------------------------------------


@pytest.mark.parametrize("script", [p.name for p in ALL_SHELL])
def test_every_scheduled_script_parses_under_bash(script):
    """A zsh-only construct in a scheduled script is a Linux outage the Mac
    never sees. ``bash -n`` is the cheapest gate against one creeping back."""
    path = next(p for p in ALL_SHELL if p.name == script)
    result = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_no_zsh_only_construct_survives_in_a_scheduled_script():
    """The three dialect words that cost this row: the ``:A`` path modifier,
    the ``:h`` head modifier, and zsh's ``print`` builtin, none of which bash
    has. ``date -v`` and the Homebrew ``uv`` path survive on purpose, as the
    BSD branch and the last-resort fallback."""
    offenders = []
    for path in ALL_SHELL:
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if "${0:A" in line or ":h:h}" in line or "print -r" in line:
                offenders.append(f"{path.name}:{n}: {line.strip()}")
    assert offenders == [], "zsh-only construct left in a scheduled script:\n" + "\n".join(
        offenders
    )


# ---- the acceptance: both daily scripts run under bash on the demo tenant ----


def test_engine_ap_daily_runs_under_bash_against_the_demo_tenant(runtime, tmp_path):
    result = _run("engine-ap-daily.sh", runtime, tmp_path, ENGINE_TENANT="demo")
    assert result.returncode == 0, result.stdout + result.stderr
    statements = str(tmp_path / "statements")
    assert _uv_calls(tmp_path) == [
        _engine("mail", "fetch"),
        _engine("ar", "remittance"),
        _engine("ap", "intake", "--param", f"since={_since()}"),
        _engine("ap", "apply"),
        _engine("ap", "qbo-push"),
        _engine("ap", "qbo-push-payments"),
        [*UNION, "python", "-c", STATEMENT_READ],
        _engine("ap", "reconcile", "--param", f"statement_dir={statements}"),
        _engine("ap", "sweep-cards"),
        _engine("ap", "workbook"),
        _engine("timesheets", "intake"),
        _engine("expenses", "inbox"),
        _engine("expenses", "intake"),
        _engine("expenses", "extract"),
        _engine("ap", "janitor", "--param", "days=10"),
    ]


def test_auditor_nightly_runs_under_bash_against_the_demo_tenant(runtime, tmp_path):
    result = _run("auditor-nightly.sh", runtime, tmp_path, ENGINE_TENANT="demo")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _uv_calls(tmp_path) == [[*UNION, "auditor", "run", "demo"]]


def test_ledger_backup_runs_under_bash(runtime, tmp_path):
    """The 23:00 push. No engine code runs, but the script carried the same
    zsh dialect and the container runs it too."""
    ledger_origin = tmp_path / "ledger-origin.git"
    ledger_origin.mkdir()
    _git(ledger_origin, "init", "--bare", "-b", "main", ".")
    ledger = tmp_path / "ledger" / "demo"
    ledger.mkdir(parents=True)
    _git(ledger, "init", "-b", "main", ".")
    (ledger / "ledger.jsonl").write_text("{}\n")
    _git(ledger, "add", ".")
    _git(ledger, "commit", "-m", "row")
    _git(ledger, "remote", "add", "origin", str(ledger_origin))
    result = _run("ledger-backup.sh", runtime, tmp_path, ENGINE_TENANT="demo")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _git(ledger_origin, "rev-parse", "main") == _git(ledger, "rev-parse", "HEAD")


def test_a_statement_dir_the_config_cannot_name_fails_the_run_loudly(runtime, tmp_path):
    """The statement tier is the clearing evidence (row 7.4). An empty answer
    from the config read is rc 3 and a non-zero run, not a silent skip."""
    result = _run(
        "engine-ap-daily.sh", runtime, tmp_path, ENGINE_TENANT="demo", STUB_STATEMENT_DIR=""
    )
    assert result.returncode == 1
    assert "statement tier: could not resolve" in result.stdout
    reconcile = [c for c in _uv_calls(tmp_path) if "reconcile" in c]
    assert reconcile == [_engine("ap", "reconcile")]


# ---- the Mac's schedule does not change --------------------------------------


@pytest.mark.parametrize(
    "script, missing",
    [
        ("auditor-nightly.sh", "ENGINE_TENANT"),
        ("engine-ap-daily.sh", "ENGINE_TENANT"),
        ("engine-jobs-resume.sh", "ENGINE_TENANT"),
        ("ledger-backup.sh", "ENGINE_TENANT"),
        ("host-heartbeat.sh", "ENGINE_TENANT"),
        ("auditor-nightly.sh", "ENGINE_LEDGER_ROOT"),
        ("engine-ap-daily.sh", "AUDITOR_STORE_ROOT"),
        ("ledger-backup.sh", "ENGINE_LEDGER_ROOT"),
    ],
)
def test_a_script_the_host_did_not_name_a_tenant_for_refuses(runtime, tmp_path, script, missing):
    """The engine carries no tenant of its own (extraction gate 2): a script
    run without the tenant or its state home named stops before any job, exit
    78, naming the variable, and never guesses one."""
    env = _env(runtime, tmp_path)
    env.pop(missing)
    clone, _, _ = runtime
    result = subprocess.run(
        ["bash", str(clone / "scripts" / script)], capture_output=True, text=True, env=env
    )
    assert result.returncode == 78, result.stdout + result.stderr
    assert missing in result.stderr
    assert _uv_calls(tmp_path) == []


def test_the_dead_man_pings_the_same_slugs_it_always_has(runtime, tmp_path):
    """The off-box watchdog (healthchecks.io) keys on the slug string. The
    tenant name moved into a variable in this row, so the slug it builds has
    to come out exactly as it was typed before: a renamed check goes silent
    and nobody is told."""
    curl_log = tmp_path / "curl.log"
    result = _run(
        "auditor-nightly.sh",
        runtime,
        tmp_path,
        HC_PING_BASE="https://hc-ping.example/key/",
        CURL_LOG=str(curl_log),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    urls = [call[-1] for call in _uv_calls(tmp_path, "curl.log")]
    assert urls == [
        "https://hc-ping.example/key/acme-auditor-nightly/start",
        "https://hc-ping.example/key/acme-auditor-nightly",
    ]


@pytest.mark.skipif(sys.platform != "darwin", reason="the Mac's own schedule")
@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh not installed")
def test_zsh_and_bash_issue_the_identical_command_log(runtime, tmp_path):
    """launchd runs ``/bin/zsh <script>`` and keeps doing so after this row;
    the Linux container runs the same file under bash. The two shells must
    issue the same commands, argument for argument, or the refactor moved
    the Mac."""
    logs = {}
    for shell in ("zsh", "bash"):
        name = f"uv-{shell}.log"
        result = _run(
            "engine-ap-daily.sh", runtime, tmp_path, shell=shell, UV_LOG=str(tmp_path / name)
        )
        assert result.returncode == 0, result.stdout + result.stderr
        logs[shell] = (tmp_path / name).read_text()
    assert logs["zsh"] == logs["bash"]
    assert "acme" in logs["zsh"]


@pytest.mark.skipif(sys.platform != "darwin", reason="BSD date is the thing being matched")
def test_since_matches_the_bsd_date_the_mac_has_always_computed(runtime, tmp_path):
    """The old line was ``SINCE=$(date -v-10d +%Y-%m-%d)``. Whatever replaces
    it prints that exact string on this machine."""
    bsd = subprocess.run(
        ["date", "-v-10d", "+%Y-%m-%d"], capture_output=True, text=True, env={"PATH": PATH}
    )
    assert bsd.returncode == 0, bsd.stderr
    expected = bsd.stdout.strip()
    result = _run("engine-ap-daily.sh", runtime, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    intake = next(c for c in _uv_calls(tmp_path) if "intake" in c and "ap" in c)
    assert f"since={expected}" in intake
    assert f"(since={expected})" in result.stdout


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh not installed")
def test_repo_resolution_matches_the_zsh_modifier_the_scripts_used(runtime, tmp_path):
    """``REPO="${0:A:h:h}"`` is what the scripts used to resolve. Two probes
    in the same directory, run under zsh: the retired modifier and the
    portable replacement must name the same repo."""
    clone, _, _ = runtime
    probes = clone / "scripts"
    (probes / "old-probe.sh").write_text('print -r -- "${0:A:h:h}"\n')
    (probes / "new-probe.sh").write_text(
        'set -u\nSELF="${BASH_SOURCE[0]:-$0}"\n'
        'printf \'%s\\n\' "$(cd "$(dirname "$SELF")/.." && pwd)"\n'
    )
    out = {}
    for probe in ("old-probe.sh", "new-probe.sh"):
        result = subprocess.run(
            ["zsh", str(probes / probe)], capture_output=True, text=True, env={"PATH": PATH}
        )
        assert result.returncode == 0, result.stderr
        out[probe] = result.stdout.strip()
    assert out["old-probe.sh"] == out["new-probe.sh"] == str(clone)


@pytest.mark.skipif(sys.platform != "darwin", reason="the Homebrew uv is a macOS path")
def test_the_launchd_path_still_resolves_the_homebrew_uv():
    """``UV=/opt/homebrew/bin/uv`` was typed into both scripts; they resolve
    it off PATH now. Under the PATH the plists pin, that is the same binary,
    so the 08:00 run does not change interpreter."""
    if not Path("/opt/homebrew/bin/uv").exists():
        pytest.skip("no Homebrew uv on this host")
    assert shutil.which("uv", path=LAUNCHD_PATH) == "/opt/homebrew/bin/uv"


def test_the_scripts_take_uv_from_the_path_not_from_a_typed_in_location(runtime, tmp_path):
    """The stub uv is found by PATH alone; nothing in the environment names
    it. A hardcoded /opt/homebrew path would have run the real binary here
    (or, on Linux, nothing at all)."""
    result = _run("auditor-nightly.sh", runtime, tmp_path, ENGINE_TENANT="demo")
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(_uv_calls(tmp_path)) == 1


# ---- the Linux tenant never calls a macOS binary ------------------------------


def test_the_demo_tenant_never_reveals_a_file_in_a_file_browser():
    """``[close].reveal_in_finder`` gates the one ``open -R`` in the engine.
    A tenant that does not name it defaults to false, on Linux and
    everywhere else."""
    assert load_tenant("demo").close.reveal_in_finder is False


def test_the_tenant_template_ships_no_file_browser_reveal():
    """``engine init`` renders this template (row 7.19); a tenant born on
    Linux must not be born asking for Finder. The template documents the
    knob in a comment, so only live lines count."""
    template = (REPO_ROOT / "tenants" / "_templates" / "tenant.toml.tmpl").read_text()
    live = [
        line
        for line in template.splitlines()
        if "reveal_in_finder" in line and not line.lstrip().startswith("#")
    ]
    assert live == []


# ---- the container's knobs (row 7.21) ----------------------------------------
#
# The image installs WITHOUT the [claude] extra and WITHOUT the host group:
# API keys only, no Claude Code, no Max seat, no browser (row 7.26's exit
# criterion). `uv run --extra claude` in that image asks uv to add a package
# the locked sync deliberately left out, which is a resolve at 08:00 against a
# network the box may not have; `--group host` would do the same for
# Playwright. ENGINE_UV_EXTRA and ENGINE_UV_GROUP are the knobs
# (scripts/lib/uv-run.sh); every assertion below exists to prove the Mac's
# default did not move while they were added.


def test_the_mac_default_environment_is_the_claude_extra_and_the_host_group(runtime, tmp_path):
    """No ENGINE_UV_EXTRA and no ENGINE_UV_GROUP in the environment (launchd
    sets neither) = the one set every scheduled job asks for, in the same
    position on every call (issue #280)."""
    result = _run("engine-ap-daily.sh", runtime, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert all(call[:5] == UNION for call in _uv_calls(tmp_path))


def test_every_scheduled_script_asks_uv_for_the_identical_environment(runtime, tmp_path):
    """The bug this row closes (2026-09-17, issue #280): the 02:00 and 08:00
    scripts asked for `--extra claude` alone, so uv synced the deploy venv to
    exactly that and REMOVED the host group. Thursday's sweep then found no
    Playwright and the headless run failed with no note. Every uv call in
    every scheduled script must ask for the same set, or one of them is
    stripping what another installed."""
    for script in ("engine-ap-daily.sh", "auditor-nightly.sh", "engine-jobs-resume.sh"):
        log = f"uv-{script}.log"
        result = _run(script, runtime, tmp_path, UV_LOG=str(tmp_path / log))
        assert result.returncode == 0, result.stdout + result.stderr
        calls = _uv_calls(tmp_path, log)
        assert calls, f"{script}: no uv call logged"
        sets = {tuple(call[:5]) for call in calls}
        assert sets == {tuple(UNION)}, f"{script} asks for more than one environment: {sets}"


def test_an_empty_extra_and_group_drop_both_flags_entirely(runtime, tmp_path):
    """The container's values. An empty string is not "unset": it means this
    host installed no extras and no optional groups, so uv is asked for
    neither."""
    result = _run(
        "engine-ap-daily.sh",
        runtime,
        tmp_path,
        ENGINE_TENANT="demo",
        ENGINE_UV_EXTRA="",
        ENGINE_UV_GROUP="",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    calls = _uv_calls(tmp_path)
    assert calls[0] == ["run", "engine", "run", "demo", "mail", "fetch"]
    assert not any("--extra" in call or "--group" in call for call in calls)


def test_an_empty_extra_alone_still_carries_the_group(runtime, tmp_path):
    """The two knobs are independent: a host with no extras may still have
    installed an optional group."""
    result = _run("auditor-nightly.sh", runtime, tmp_path, ENGINE_TENANT="demo", ENGINE_UV_EXTRA="")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _uv_calls(tmp_path) == [["run", "--group", "host", "auditor", "run", "demo"]]


def test_an_empty_group_alone_still_carries_the_extra(runtime, tmp_path):
    result = _run("auditor-nightly.sh", runtime, tmp_path, ENGINE_TENANT="demo", ENGINE_UV_GROUP="")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _uv_calls(tmp_path) == [["run", "--extra", "claude", "auditor", "run", "demo"]]


def test_engine_uv_extra_passes_through_the_extra_it_names(runtime, tmp_path):
    result = _run(
        "auditor-nightly.sh",
        runtime,
        tmp_path,
        ENGINE_TENANT="demo",
        ENGINE_UV_EXTRA="host",
        ENGINE_UV_GROUP="",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert _uv_calls(tmp_path) == [["run", "--extra", "host", "auditor", "run", "demo"]]


def test_engine_uv_group_passes_through_the_group_it_names(runtime, tmp_path):
    result = _run(
        "auditor-nightly.sh",
        runtime,
        tmp_path,
        ENGINE_TENANT="demo",
        ENGINE_UV_EXTRA="",
        ENGINE_UV_GROUP="dev",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert _uv_calls(tmp_path) == [["run", "--group", "dev", "auditor", "run", "demo"]]


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh not installed")
def test_the_knobs_name_the_set_because_zsh_would_not_split_a_flag_string(runtime, tmp_path):
    """The trap these knobs were reshaped around, pinned so it cannot come
    back. zsh does not word-split an unquoted parameter expansion, so
    `uv run $FLAGS` hands "--extra claude" to uv as ONE argument under the
    shell launchd actually invokes. Both shells must issue the same argv, in
    all four combinations of the two knobs."""
    combinations = (
        ("default", {}),
        ("none", {"ENGINE_UV_EXTRA": "", "ENGINE_UV_GROUP": ""}),
        ("extra-only", {"ENGINE_UV_GROUP": ""}),
        ("group-only", {"ENGINE_UV_EXTRA": ""}),
    )
    logs = {}
    for shell in ("zsh", "bash"):
        for label, knobs in combinations:
            name = f"uv-{shell}-{label}.log"
            result = _run(
                "auditor-nightly.sh",
                runtime,
                tmp_path,
                shell=shell,
                UV_LOG=str(tmp_path / name),
                **knobs,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            logs[(shell, label)] = (tmp_path / name).read_text()
    for label, _ in combinations:
        assert logs[("zsh", label)] == logs[("bash", label)], label
    assert _uv_calls(tmp_path, "uv-zsh-default.log") == [[*UNION, "auditor", "run", "acme"]]
    assert _uv_calls(tmp_path, "uv-zsh-none.log") == [["run", "auditor", "run", "acme"]]
    assert _uv_calls(tmp_path, "uv-zsh-extra-only.log") == [
        ["run", "--extra", "claude", "auditor", "run", "acme"]
    ]
    assert _uv_calls(tmp_path, "uv-zsh-group-only.log") == [
        ["run", "--group", "host", "auditor", "run", "acme"]
    ]


def test_the_auditor_keeps_the_claude_extra_by_default(runtime, tmp_path):
    result = _run("auditor-nightly.sh", runtime, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _uv_calls(tmp_path) == [[*UNION, "auditor", "run", "acme"]]


# ---- the two scripts the crontab adds ----------------------------------------


def test_the_heartbeat_pings_this_tenants_check_and_runs_no_engine_code(runtime, tmp_path):
    """The dead-man must not depend on the thing it watches (the launchd
    plist's own words): shell and curl, no uv, no engine, no ledger."""
    result = _run(
        "host-heartbeat.sh",
        runtime,
        tmp_path,
        ENGINE_TENANT="demo",
        HC_PING_BASE="https://hc-ping.example/key/",
        CURL_LOG=str(tmp_path / "curl.log"),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    urls = [call[-1] for call in _uv_calls(tmp_path, "curl.log")]
    assert urls == ["https://hc-ping.example/key/demo-host-heartbeat"]
    assert _uv_calls(tmp_path) == []


def test_the_heartbeat_is_a_silent_noop_with_no_ping_base(runtime, tmp_path):
    result = _run("host-heartbeat.sh", runtime, tmp_path, ENGINE_TENANT="demo")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (tmp_path / "curl.log").exists()


def test_the_retries_script_runs_the_resume_command(runtime, tmp_path):
    """Row 7.23 shipped ``engine jobs resume`` and named the cadence; this
    row is the line that runs it."""
    result = _run("engine-jobs-resume.sh", runtime, tmp_path, ENGINE_TENANT="demo")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _uv_calls(tmp_path) == [[*UNION, "engine", "jobs", "resume", "demo"]]


# ---- the auditor watches every stage the schedule runs -----------------------
#
# Two hand-maintained lists have to agree, and nothing made them: the stages
# `engine-ap-daily.sh` executes at 08:00, and `[auditor].expected_daily_jobs`,
# the ONLY list the heartbeat lens iterates (auditor/lenses/heartbeat.py,
# check_daily_runs). A stage in the script but absent from that list has no
# "never ran" check and no "last run errored" check — it can die every morning
# and no report ever says so, because the lens never asks about it.
#
# Three drifted in unwatched and stayed that way: ap/qbo-push-payments (#249,
# 2026-09-12), ap/sweep-cards (#298) and ar/remittance (#300, both 2026-09-17).
# Two of the three are write-side. Found by the 2026-09-22 triage.


def _daily_stages(tmp_path) -> set[tuple[str, str]]:
    """``(agent, job)`` for every ``engine run <tenant> <agent> <job>`` the
    script issued, read back off the uv stub's log."""
    stages: set[tuple[str, str]] = set()
    for call in _uv_calls(tmp_path):
        if "engine" not in call:
            continue
        head = call.index("engine")
        # [...uv flags..., "engine", "run", <tenant>, <agent>, <job>, ...]
        if call[head + 1 : head + 2] != ["run"] or len(call) < head + 5:
            continue
        stages.add((call[head + 3], call[head + 4]))
    return stages


def test_the_crontab_commands_are_the_scripts_that_exist(runtime, tmp_path):
    """Every command the renderer puts in the crontab must be an executable
    file in this repo: a crontab line naming a missing script is a job that
    fails once a day forever."""
    from core.engine.schedule import entries

    cfg = load_tenant("demo")
    for entry in entries(cfg, repo=str(REPO_ROOT), log_dir="/logs"):
        script = Path(entry.command)
        assert script.is_file(), f"{entry.name}: {script} does not exist"
        assert script.stat().st_mode & 0o111, f"{entry.name}: {script} is not executable"
