"""Security review 2026-10-03 (#390, LOW): a secrets file is data, never code.

The entrypoint SOURCED /data/secrets.env (shell, executed) and exported every
name the decrypted sops file carried, PATH and LD_PRELOAD included. Both need
write access to the volume, and both now go through one parser that runs
nothing and refuses a name that would steer the process instead of handing it
a secret.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parents[2] / "host" / "secrets-env.sh"


def _parse(lines: str, *, plain: bool) -> tuple[dict[str, str], str]:
    script = f"""
        set -eu
        . "{LIB}"
        while IFS= read -r line || [ -n "$line" ]; do
          export_secret_line "$line" {"plain" if plain else "sops"}
          printf '%s %s\\n' "$SECRET_RESULT" "$SECRET_NAME" >&2
        done
        env
    """
    r = subprocess.run(
        ["bash", "-c", script],
        input=lines,
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], "HOME": "/nonexistent"},
        timeout=30,
        check=True,
    )
    env = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
    return env, r.stderr


def test_a_plain_file_is_parsed_never_executed(tmp_path):
    marker = tmp_path / "ran"
    env, _ = _parse(
        f"# comment\n\nexport API_KEY='abc def'\nTOKEN=\"x$HOME\"\n$(touch {marker})\nOTHER=v=w\n",
        plain=True,
    )
    assert env["API_KEY"] == "abc def"
    assert env["TOKEN"] == "x$HOME"  # no expansion
    assert env["OTHER"] == "v=w"
    assert not marker.exists()


@pytest.mark.parametrize(
    "name",
    [
        "PATH",
        "LD_PRELOAD",
        "DYLD_INSERT_LIBRARIES",
        "PYTHONPATH",
        "BASH_ENV",
        "GIT_SSH_COMMAND",
        "UV_INDEX_URL",
        "SOPS_AGE_KEY_FILE",
        "ENGINE_LEDGER_ROOT",
        "AUDITOR_STORE_ROOT",
        "1BAD",
        "BAD-NAME",
    ],
)
@pytest.mark.parametrize("plain", [True, False])
def test_a_name_that_steers_the_process_is_refused(name, plain):
    env, log = _parse(f"{name}=/tmp/evil\n", plain=plain)
    assert env.get(name) != "/tmp/evil"
    assert f"refused {name}" in log


def test_secret_names_pass_from_sops_verbatim():
    env, log = _parse(
        "DEMO_QBO_CLIENT_SECRET=ci-canary 7f3a\nENGINE_GATEWAY_KEY='k'\n", plain=False
    )
    assert env["DEMO_QBO_CLIENT_SECRET"] == "ci-canary 7f3a"
    assert env["ENGINE_GATEWAY_KEY"] == "'k'"  # sops output is verbatim, never unquoted
    assert "exported DEMO_QBO_CLIENT_SECRET" in log
