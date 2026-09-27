"""The Claude Code CLI auto-updates; the Python SDK is pinned. The pair must
stay compatible (docs/lessons.md, "Pin both halves of a pair").

On 2026-09-04 the CLI had updated itself to 2.1.260 overnight while the
engine pinned claude-agent-sdk 0.2.96. Every model call in the 08:00 run
failed with "Claude Code returned an error result: success" (the old SDK
misread the new CLI's exit and result stream); AP intake flagged two real
documents as transport errors and the expenses inbox job failed outright.
The #172 failure trace caught it the same morning. SDK 0.2.152 talks to
that CLI cleanly.

This test pins the floor: the SDK pin may move up, never back below the
version proven against the CLI that broke it. The SDK is the ``[claude]``
extra (row 7.15): the pin lives under ``[project.optional-dependencies]``,
and in an environment without the extra this test skips, naming it.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

pytest.importorskip(
    "claude_agent_sdk",
    reason="the Claude Agent SDK is not installed: the [claude] extra (uv sync --extra claude)",
)

SDK_FLOOR = (0, 2, 152)


def _pinned_sdk_version() -> tuple[int, ...]:
    data = tomllib.loads((Path(__file__).resolve().parents[2] / "pyproject.toml").read_text())
    for dep in data["project"]["optional-dependencies"]["claude"]:
        m = re.fullmatch(r"claude-agent-sdk==(\d+)\.(\d+)\.(\d+)", dep.strip())
        if m:
            return tuple(int(x) for x in m.groups())
    raise AssertionError(
        "claude-agent-sdk must be pinned with == under [project.optional-dependencies] "
        "claude in pyproject.toml (invariant 9)"
    )


def test_claude_agent_sdk_pin_never_falls_below_the_cli_proven_floor():
    assert _pinned_sdk_version() >= SDK_FLOOR, (
        f"claude-agent-sdk pin {_pinned_sdk_version()} is below {SDK_FLOOR}, the version "
        "proven against Claude Code 2.1.260 (incident 2026-09-04)"
    )
