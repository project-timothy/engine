"""A person's own setup, after the ministry's (chunk 10; the owner's design
of 2026-10-15: "no codes, no addresses: Face ID, then one question per
screen").

The ministry's onboarding (``core/onboarding``) sets up the books and names
the people; this sets up one person, in the same shape:

1. ``offered(facts, offer)``: the steps this person is offered, in order:
   the baseline that fits them, then what the ministry's ``[setup] offer``
   adds.
2. ``next_step(answers, steps)``: the step the next screen is about, and
   whether it asks ("ask") or waits to see the step work ("check"). Sign-up
   walks only ``at_sign_up(answers, steps)``: a step marked
   ``at_sign_up = false`` waits until the person asks for it.
3. ``SetupBook.record(person, step, said)``: "yes" (only ever after the
   person's own Face ID, core/tools/setup_page.py), "later" (not now, or
   move on), or "again" (bring a step back).
4. ``SetupBook.saw(person, step)``: Tim saw the step work. A step is done
   only then: the hello's link was tapped, the receipt page opened from
   the home screen, the phone's tracker reached the box.

The answers are settings (``setup.json`` in the tenant folder, readable by
the box alone), never the ledger, so there is no migration. The words a
person reads live in ``person.toml``. Standard library only.
"""

from __future__ import annotations

import json
import threading
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from . import OnboardingError

STEPS_FILE = Path(__file__).resolve().parent / "person.toml"
FILE = "setup.json"
SAID = ("yes", "later", "again")
WORDS = ("name", "ask", "help", "yes", "later", "waiting", "done")


@dataclass(frozen=True)
class Step:
    id: str
    name: str
    ask: str
    help: str
    yes: str
    later: str
    waiting: str
    done: str
    baseline: bool = False
    at_sign_up: bool = True
    face_id: bool = True
    points_heading: str = ""
    points: tuple[str, ...] = ()


@cache
def load_steps() -> tuple[Step, ...]:
    with STEPS_FILE.open("rb") as handle:
        data = tomllib.load(handle)
    return tuple(
        Step(
            **{k: s[k] for k in ("id", *WORDS)},
            baseline=bool(s.get("baseline", False)),
            at_sign_up=bool(s.get("at_sign_up", True)),
            face_id=bool(s.get("face_id", True)),
            points_heading=str(s.get("points_heading", "")),
            points=tuple(str(p) for p in s.get("points", ())),
        )
        for s in data["step"]
    )


@cache
def _voice_words() -> dict[str, dict[str, Any]]:
    """Each step's ``[step.voice]`` table: the words, and whether it's
    offered to everyone, where Tim's own voice is on (card T2)."""
    with STEPS_FILE.open("rb") as handle:
        data = tomllib.load(handle)
    return {
        str(s["id"]): dict(s["voice"]) for s in data["step"] if isinstance(s.get("voice"), dict)
    }


def _step(step: str) -> Step:
    for s in load_steps():
        if s.id == step:
            return s
    raise OnboardingError(f"no setup step {step!r}")


@dataclass(frozen=True)
class Facts:
    """What decides whether a baseline step fits a person: the address the
    ministry gave them (authority.toml), whether they file receipts, and
    whether an expense plan waits on their yes to adopt it (screens chunk 3)."""

    email: str = ""
    files_expenses: bool = False
    adopts_plan: bool = False


_FITS = {
    "daily_email": lambda f: bool(f.email),
    "home_screen": lambda f: f.files_expenses,
    "mileage": lambda f: f.files_expenses,  # drives become their expense lines
    "role": lambda f: f.adopts_plan,
    "plan": lambda f: f.adopts_plan,
}


def offered(facts: Facts, offer: Iterable[str], *, voice: bool = False) -> tuple[str, ...]:
    """The steps this person is offered, in the order they're asked: each
    that fits them, and is baseline or one the ministry offers. With Tim's
    own voice on, a step whose ``[step.voice]`` says ``baseline`` is offered
    to everyone (talking to Tim, card T2)."""
    wanted = set(offer)

    def base(s: Step) -> bool:
        return s.baseline or (voice and bool(_voice_words().get(s.id, {}).get("baseline")))

    return tuple(
        s.id
        for s in load_steps()
        if _FITS.get(s.id, lambda f: True)(facts) and (base(s) or s.id in wanted)
    )


def at_sign_up(answers: dict, steps: Iterable[str]) -> tuple[str, ...]:
    """The offered steps sign-up walks through: each meant for sign-up, and
    any other once the person asked for it or said yes to it (the sign-up
    goal, 2026-10-27: mileage and the calendar wait until they're wanted)."""
    return tuple(
        step
        for step in steps
        if _step(step).at_sign_up
        or answers.get(step, {}).get("wanted")
        or answers.get(step, {}).get("yes")
    )


def next_step(answers: dict, steps: Iterable[str]) -> tuple[str, str] | None:
    """(step, "ask" | "check") for the next screen, or None when every
    offered step is done or set aside for now."""
    for step in steps:
        held = answers.get(step, {})
        if held.get("seen") or held.get("later"):
            continue
        return step, "check" if held.get("yes") or not _step(step).face_id else "ask"
    return None


def words(step: str, *, email: str = "", who: str = "", voice: bool = False) -> dict[str, Any]:
    """The step's words for the screen, with the person's address in, and
    the name of whoever the ministry's expense plan covers. With Tim's own
    voice on, the step's ``[step.voice]`` words stand in for its own, and
    any word only that table has (``talk``, ``also``, ``open``) comes too."""
    s = _step(step)

    def fill(text: str) -> str:
        return text.replace("{email}", email).replace("{who}", who)

    said: dict[str, Any] = {k: fill(getattr(s, k)) for k in WORDS}
    said["points_heading"] = fill(s.points_heading)
    said["points"] = [fill(p) for p in s.points]
    if voice:
        said.update(
            {k: fill(v) for k, v in _voice_words().get(step, {}).items() if isinstance(v, str)}
        )
    return said


class PeopleFile:
    """Settings kept by person in one JSON file in the tenant folder, read
    whole and written whole, mode 0600 (setup answers; what Tim has
    explained, core/tools/first_time.py). No ledger table: a lost file
    costs a question or an explanation asked again, never a dollar."""

    def __init__(self, directory: str | Path, name: str) -> None:
        self.path = Path(directory) / name
        self._lock = threading.Lock()

    def _read(self) -> dict:
        try:
            held = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return held if isinstance(held, dict) else {}

    def _write(self, held: dict) -> None:
        part = self.path.with_name(self.path.name + ".part")
        part.write_text(json.dumps(held, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        part.chmod(0o600)
        part.replace(self.path)


class SetupBook(PeopleFile):
    """Every person's setup answers, in ``setup.json`` in the tenant folder."""

    def __init__(self, directory: str | Path) -> None:
        super().__init__(directory, FILE)

    def answers(self, person: str) -> dict:
        held = self._read().get(person, {})
        return held if isinstance(held, dict) else {}

    def record(self, person: str, step: str, said: str, *, now: str) -> dict:
        """The person's answer to one step; their answers after it."""
        _step(step)
        if said not in SAID:
            raise OnboardingError(f"{step}: {said!r} is not yes, later or again")
        with self._lock:
            held = self._read()
            mine = held.setdefault(person, {})
            entry = dict(mine.get(step, {}))
            if said == "yes":
                entry["yes"] = now
                entry.pop("later", None)
            elif said == "later":
                entry["later"] = now
            else:
                entry.pop("later", None)
                entry["wanted"] = now  # asked for, so sign-up walks it now
            mine[step] = entry
            self._write(held)
            return dict(mine)

    def saw(self, person: str, step: str, *, now: str) -> bool:
        """Tim saw ``step`` work for ``person``: True the first time. A step
        the person never said yes to is never seen, but for one whose button
        is the yes (``face_id = false``): there, working is the yes."""
        with self._lock:
            held = self._read()
            if not _step(step).face_id:
                held.setdefault(person, {}).setdefault(step, {})
            entry = held.get(person, {}).get(step)
            if not isinstance(entry, dict) or entry.get("seen"):
                return False
            if not entry.get("yes") and _step(step).face_id:
                return False
            entry["seen"] = now
            self._write(held)
            return True

    def proved(self, person: str, step: str, *, how: str, now: str) -> None:
        """Tim saw ``step`` work without asking (``how``: a welcome mailed to
        that inbox and opened from it; the sign-up goal, 2026-10-27): done,
        and never one of the person's screens."""
        _step(step)
        with self._lock:
            held = self._read()
            entry = held.setdefault(person, {}).setdefault(step, {})
            entry.setdefault("yes", now)
            entry.setdefault("seen", now)
            entry.setdefault("proved", how)
            entry.pop("later", None)
            self._write(held)


__all__ = [
    "FILE",
    "Facts",
    "PeopleFile",
    "SetupBook",
    "Step",
    "at_sign_up",
    "load_steps",
    "next_step",
    "offered",
    "words",
]
