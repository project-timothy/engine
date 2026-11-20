"""The yearly tax figures, read from ``core/tax/tables/<year>.toml`` (board
chunk c36). Each figure carries its value, the document it came from, where
in that document, and the day someone read it there; ``problems`` names any
figure missing one, and the test suite fails the build on it. A year with no
file is refused in plain words, never guessed."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from functools import cache
from pathlib import Path

TABLES = Path(__file__).resolve().parent / "tables"
STATUSES = ("single", "married_jointly")
"""The filing statuses Tim works out (the owner, 2026-11-15)."""
_GOVERNMENT = ("https://www.irs.gov/", "https://www.ssa.gov/")


class NoYear(KeyError):
    """Tim holds no tax figures for this year."""

    def __init__(self, year: str) -> None:
        super().__init__(year)
        self.year = year

    def __str__(self) -> str:
        return f"Tim doesn't hold {self.year}'s tax figures yet."


class NoFigure(KeyError):
    """The year's file has no such figure."""

    def __str__(self) -> str:
        return f"The tax figures have no {self.args[0]}."


@dataclass(frozen=True)
class Figure:
    name: str
    value: object
    source: str  # a key under [sources]
    address: str  # where in the source
    confirmed: str  # the day someone read it there, YYYY-MM-DD


@dataclass(frozen=True)
class Bracket:
    floor: int  # cents: where this rate starts
    up_to: int | None  # cents: where it stops; None at the top
    rate: Decimal
    base: int  # cents: the tax on everything below the floor, as printed


@dataclass(frozen=True)
class Year:
    year: str
    figures: dict[str, Figure]
    sources: dict[str, dict]

    def figure(self, name: str) -> Figure:
        """The named figure, or NoFigure."""
        if name not in self.figures:
            raise NoFigure(name)
        return self.figures[name]

    def cents(self, name: str) -> int:
        """A dollar figure, in cents."""
        return _cents(str(self.figure(name).value))

    def rate(self, name: str) -> Decimal:
        """A rate figure, as an exact decimal."""
        return Decimal(str(self.figure(name).value))

    def brackets(self, status: str) -> list[Bracket]:
        """The income tax table for a filing status, lowest rate first."""
        rows, floor = [], 0
        for up_to, rate, base in self.figure(f"brackets.{status}").value:  # type: ignore[attr-defined]
            top = _cents(up_to) if up_to else None
            rows.append(Bracket(floor, top, Decimal(rate), _cents(base)))
            floor = top or floor
        return rows

    def cited(self, *names: str) -> str:
        """Where the named figures came from, each source once, in order."""
        seen: list[str] = []
        for name in names:
            key = self.figure(name).source
            if key not in seen:
                seen.append(key)
        return "; ".join(f"{self.sources[k]['title']} ({self.sources[k]['url']})" for k in seen)


def _cents(dollars: str) -> int:
    return int(Decimal(dollars) * 100)


def problems(data: dict, today: date | None = None) -> list[str]:
    """What a year's table is missing, one line a problem, each starting
    with the figure or source it names: a source with no title or a
    non-government address; a figure with no value, no known source, no
    address, or no confirmed day (a real date, not in the future)."""
    today = today or date.today()
    sources = data.get("sources", {})
    out = []
    for key, s in sources.items():
        if not s.get("title"):
            out.append(f"{key}: no title")
        if not str(s.get("url", "")).startswith(_GOVERNMENT):
            out.append(f"{key}: its address is not an IRS or SSA page")
    for name, f in data.get("figures", {}).items():
        if f.get("value") in (None, ""):
            out.append(f"{name}: no value")
        if f.get("source") not in sources:
            out.append(f"{name}: no known source")
        if not f.get("address"):
            out.append(f"{name}: no address in its source")
        try:
            day = date.fromisoformat(str(f.get("confirmed", "")))
        except ValueError:
            out.append(f"{name}: no confirmed day")
            continue
        if day > today:
            out.append(f"{name}: confirmed in the future")
    return out


@cache
def year(y: str) -> Year:
    """The tax figures for year ``y``, or NoYear when Tim holds none."""
    path = TABLES / f"{y}.toml"
    if not y.isdigit() or not path.is_file():
        raise NoYear(y)
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    figures = {
        name: Figure(name, f["value"], f["source"], f["address"], f["confirmed"])
        for name, f in data["figures"].items()
    }
    return Year(str(data["year"]), figures, data["sources"])
