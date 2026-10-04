"""The session transcript: one JSONL file per run, redacted before it is
written (``docs/runner-design.md``, "Transcripts").

Both runners write the same event vocabulary: ``run_start``, ``prompt``,
``turn``, ``tool_call``, ``tool_result``, ``refused``, ``run_end``. A
``turn`` event's ``raw`` is what a replay feeds back to the fixture adapter
(:func:`turns_of`), and ``run_end`` carries the result minus the transcript
path (:func:`note_of` reads the note out of it for the skill harness).

:func:`redact` runs on every event before it lands. It lives in
``core.redact`` now (phase 7 row 7.22), imported here under the names this
module has always exported, because the runner applies the same rules to
every ``JobOutput`` and one redactor is the point: the value of every
environment variable the caller names becomes ``<redacted:VAR>``, named token
families become ``<redacted:key>``, TIN shapes become ``<redacted:tin>``, and
a whole value that is one padded base64 blob becomes ``<redacted:token>``.
The W-9 lane's invariant is the model: a TIN read in memory never reaches a
log.
"""

from __future__ import annotations

import datetime as _dt
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..redact import env_values, redact, redact_text

__all__ = [
    "Transcript",
    "commands_of",
    "env_values",
    "note_of",
    "read_events",
    "redact",
    "redact_text",
    "turns_of",
]


def _jsonable(obj: Any) -> Any:
    """Paths, Decimals, dataclasses: whatever ``json`` cannot take, as text."""
    return json.loads(json.dumps(obj, default=str))


class Transcript:
    """Append-only JSONL writer. Every event passes :func:`redact` first."""

    def __init__(self, path: Path, *, env_names: Iterable[str] = ()) -> None:
        self.path = Path(path)
        self._values = env_values(env_names)

    def redact(self, obj: Any) -> Any:
        return redact(_jsonable(obj), env_values=self._values)

    def write(self, event: str, payload: dict[str, Any] | None = None) -> None:
        line = {"event": event, "at": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")}
        line.update(self.redact(payload or {}))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, sort_keys=True) + "\n")


def read_events(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        if raw.strip():
            out.append(json.loads(raw))
    return out


def turns_of(path: Path) -> list[str]:
    """The recorded model output of every ``turn`` event, in order: the list
    a ``FixtureAdapter`` replays."""
    return [e["raw"] for e in read_events(path) if e.get("event") == "turn"]


def note_of(path: Path) -> str:
    """The note the session ended with (``run_end``); empty when the run
    never reached one."""
    for event in reversed(read_events(path)):
        if event.get("event") == "run_end":
            return str(event.get("result", {}).get("note", ""))
    return ""


def commands_of(path: Path) -> list[list[str]]:
    """Every argv the session asked to run (allowed or refused), for a
    banned-string sweep over the whole session."""
    out: list[list[str]] = []
    for event in read_events(path):
        if event.get("event") in ("tool_call", "refused"):
            argv = event.get("argv")
            if isinstance(argv, list):
                out.append([str(a) for a in argv])
    return out
