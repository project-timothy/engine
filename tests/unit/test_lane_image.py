"""The agent lane's container: Dockerfile.lane, compose.lane.yaml, and the
egress proxy (issue #359, container half; the 2026-10-07 decision).

A lane session writes code and runs it. On the host it ran as the owner, with
the owner's home, keychain and open egress (#353). In this container it sees
only what is mounted, reaches only the hosts the proxy allows, and holds only
the environment the compose file names (#386's environment half). Every
assertion below is one of those three walls, read from the files that build
them; the walls themselves are proven on a Docker host (the PR records the
run), which the unit suite does not assume.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO / "Dockerfile.lane"
COMPOSE = REPO / "compose.lane.yaml"
SQUID = REPO / "host" / "lane" / "squid.conf"
UV_SHIM = REPO / "host" / "lane" / "uv"
ENGINE_DOCKERFILE = REPO / "Dockerfile"

ALLOWED_HOSTS = {"github.com", "api.github.com", "api.anthropic.com"}

# What a lane must never be handed: the owner's tokens, keychain, host config
# and the live ledger (the design note on #359).
NEVER_MOUNTED = (
    ".qbo-tokens",
    ".outlook-mcp-tokens",
    "Library/Keychains",
    ".config/",
    ".ledger",
    ".ssh",
    "docker.sock",
)


def _dockerfile() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def _compose() -> str:
    return COMPOSE.read_text(encoding="utf-8")


def _service(name: str) -> str:
    """One service's block of compose.lane.yaml, by indentation."""
    text = _compose()
    m = re.search(rf"^  {name}:\n((?:    .*\n|\s*\n)+)", text, re.M)
    assert m, f"no service {name} in {COMPOSE.name}"
    return m.group(1)


# ---- the image ----------------------------------------------------------------


def test_the_lane_image_installs_the_claude_extra_and_the_dev_tools():
    """The session drives Claude Code through the SDK (whose wheel carries the
    CLI, hash-pinned by uv.lock) and runs pytest and ruff (the dev group)."""
    text = _dockerfile()
    m = re.search(r"uv sync ([^\n\\]*(?:\\\n[^\n\\]*)*)", text)
    assert m, "no uv sync in Dockerfile.lane"
    sync = m.group(1)
    assert "--locked" in sync
    assert "--extra claude" in sync
    assert "--no-dev" not in sync, "the session runs pytest and ruff"
    assert "--no-install-project" in sync, "the code comes from the mounted clone"


def test_the_lane_image_carries_no_engine_code():
    """The clone the wrapper mounts is the code under review; an image copy of
    the engine would shadow it for `import core`."""
    copies = re.findall(r"^COPY (?!--from=)(.+)$", _dockerfile(), re.M)
    for line in copies:
        sources = line.split()[:-1]
        assert all(
            s in ("pyproject.toml", "uv.lock", "host/lane/uv", "--chmod=0755") for s in sources
        ), line


def test_the_engine_image_stays_sdk_free():
    """Row 7.26: the product image installs no [claude] extra. The lane image
    is a separate file so that never changes."""
    assert "--extra claude" not in ENGINE_DOCKERFILE.read_text(encoding="utf-8")


def test_the_lane_runs_as_uid_10001_never_root():
    text = _dockerfile()
    users = re.findall(r"^USER\s+(\S+)", text, re.M)
    assert users and users[-1] not in ("root", "0"), users
    assert "--uid 10001" in text


def test_the_lane_never_updates_claude_code_or_phones_home():
    text = _dockerfile()
    assert "DISABLE_AUTOUPDATER=1" in text
    assert "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1" in text


def test_every_uv_run_uses_the_image_venv_offline():
    """The runner hands a model's tool calls PATH and HOME only (reduced_env),
    so `uv run pytest` would otherwise build a venv from PyPI, which the proxy
    refuses. The shim on PATH names the image's venv and forbids a sync."""
    shim = UV_SHIM.read_text(encoding="utf-8")
    for name in ("UV_PROJECT_ENVIRONMENT=", "UV_NO_SYNC=1", "UV_OFFLINE=1"):
        assert name in shim, name
    assert shim.rstrip().endswith('exec /usr/local/libexec/uv "$@"')
    text = _dockerfile()
    assert "/usr/local/libexec/uv" in text
    assert re.search(r"^COPY .*host/lane/uv /usr/local/bin/uv$", text, re.M)


def test_the_shim_parses_under_sh():
    result = subprocess.run(["sh", "-n", str(UV_SHIM)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


# ---- egress --------------------------------------------------------------------


def test_the_lane_sits_only_on_an_internal_network():
    """No route out but the proxy: the lane's one network is internal."""
    text = _compose()
    assert re.search(r"^  lane-internal:\n    internal: true", text, re.M), text
    lane = _service("lane")
    nets = re.search(r"    networks:\n((?:      - .*\n)+)", lane)
    assert nets and nets.group(1).split() == ["-", "lane-internal"], lane
    egress = _service("egress")
    assert "lane-internal" in egress and "lane-egress" in egress


def test_the_lane_reaches_out_only_through_the_proxy():
    lane = _service("lane")
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        assert f"- {name}=http://egress:3128" in lane, name


def test_the_proxy_allows_exactly_the_named_hosts_and_logs_the_rest():
    text = SQUID.read_text(encoding="utf-8")
    acl = re.search(r"^acl lane_allowed dstdomain (.+)$", text, re.M)
    assert acl and set(acl.group(1).split()) == ALLOWED_HOSTS
    rules = re.findall(r"^http_access .+$", text, re.M)
    assert rules[-1] == "http_access deny all", rules
    assert "http_access deny CONNECT !SSL_ports" in rules
    assert re.search(r"^access_log stdio:/var/log/squid/access.log", text, re.M), (
        "denials must be visible: the wrapper copies this log out after the run"
    )


def test_the_proxy_config_is_mounted_read_only():
    assert "./host/lane/squid.conf:/etc/squid/squid.conf:ro" in _service("egress")


# ---- mounts and environment -----------------------------------------------------


def test_the_lane_mounts_only_the_named_paths():
    lane = _service("lane")
    vols = re.search(r"    volumes:\n((?:      (?:- |#).*\n)+)", lane)
    assert vols, lane
    mounts = re.findall(r"^      - .+:(/work/\w+)(?::(ro))?$", vols.group(1), re.M)
    assert [t for t, _ in mounts] == [
        "/work/engine",
        "/work/tenant",
        "/work/in",
        "/work/ledger",
        "/work/out",
    ]
    modes = dict(mounts)
    assert modes["/work/in"] == "ro" and modes["/work/ledger"] == "ro"
    assert modes["/work/engine"] == "" and modes["/work/out"] == ""


@pytest.mark.parametrize("path", NEVER_MOUNTED)
def test_the_owner_files_are_never_mounted(path):
    assert path not in _compose()


def test_the_lane_environment_is_only_what_compose_names():
    """#386's environment half: no env_file, so nothing reaches the session
    the file does not name. The two secrets are names passed through from
    the wrapper's environment, never values."""
    lane = _service("lane")
    assert "env_file" not in lane
    assert re.search(r"^      - CLAUDE_CODE_OAUTH_TOKEN$", lane, re.M)
    assert re.search(r"^      - GH_TOKEN$", lane, re.M)
    for shape in ("sk-ant", "github_pat_", "ghp_"):
        assert shape not in _compose(), shape


def test_the_lane_holds_no_privilege():
    lane = _service("lane")
    assert "cap_drop:\n      - ALL" in lane
    assert "no-new-privileges:true" in lane
    assert "privileged" not in lane


def test_every_image_in_the_lane_compose_is_pinned_by_digest():
    for ref in re.findall(r"^    image: (\S+)", _compose(), re.M):
        if ref.startswith("${"):
            continue  # the locally built lane image, named by the wrapper
        assert re.search(r"@sha256:[0-9a-f]{64}$", ref), f"{ref} is pinned by tag only"


@pytest.mark.skipif(shutil.which("docker") is None, reason="no docker on this host")
def test_the_lane_compose_file_is_valid():
    result = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE), "config", "-q"],
        capture_output=True,
        text=True,
        cwd=REPO,
        env={
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANE_ENGINE": "/tmp",
            "LANE_TENANT": "/tmp",
            "LANE_IN": "/tmp",
            "LANE_LEDGER": "/tmp",
            "LANE_OUT": "/tmp",
        },
    )
    assert result.returncode == 0, result.stderr
