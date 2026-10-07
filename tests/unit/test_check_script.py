"""``scripts/check.sh`` runs every gate CI's ``check`` job runs (2026-10-07).

A local pass that skips a gate CI runs is how a PR goes red on its first
push (vulture, 2026-10-06). Every command in the ``check`` job must appear
in the script verbatim, ``$RUNNER_TEMP`` read as the script's ``$TMP``.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = (REPO / "scripts" / "check.sh").read_text()
CI = (REPO / ".github" / "workflows" / "ci.yml").read_text()

# Steps that set the runner up rather than check anything.
SETUP = {"uv python install 3.12"}


def _check_job_commands() -> list[str]:
    job = re.search(r"^  check:\n(.*?)(?=^  \S)", CI, re.M | re.S)
    assert job, "ci.yml has a `check` job"
    commands: list[str] = []
    for match in re.finditer(r"^(\s+)run: (\|)?(.*)$", job.group(1), re.M):
        indent, block, first = match.groups()
        if not block:
            commands.append(first.strip())
            continue
        body = job.group(1)[match.end() :].split("\n")
        for line in body[1:]:
            if line.strip() and len(line) - len(line.lstrip()) <= len(indent):
                break
            commands.append(line.strip())
    return [c for c in commands if c and not c.startswith("#") and c not in SETUP]


def test_the_check_job_has_gates_to_compare():
    assert len(_check_job_commands()) >= 12


def test_every_ci_gate_runs_in_the_local_script():
    missing = [
        c
        for c in _check_job_commands()
        if c.replace("$RUNNER_TEMP", "$TMP").replace(" \\", "") not in SCRIPT
    ]
    assert missing == [], "scripts/check.sh does not run these CI commands"
