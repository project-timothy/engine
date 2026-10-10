"""A time-zone answer in plain words, resolved to an IANA zone name.

The engine keeps instants in UTC and each tenant's IANA zone name, and
converts at the edge (core/engine/clock.py). A zone name, never a fixed
offset: an offset carries no daylight-saving rules. People don't answer in
zone names, though. Ruth said "nepal," Carol "Eastern," Daniel "CAT (UTC+2)"
(Tim walkthrough 1, 2026-10-09), and the engine stored each as typed until
the read server died on it. This module turns such an answer into a zone
name, or says in plain words what to ask next.

Countries and cities come from the IANA tables the host's tzdata installs
beside the zone files (``zone.tab`` and ``iso3166.tab`` on ``zoneinfo.TZPATH``;
the container installs tzdata). A host without them still takes exact zone
names and the everyday names below. The tables are read, never copied into
the repository: country and city names would trip the tenant-token lint. The
everyday names below (Eastern, CAT, GMT) are the readings people use; an
abbreviation that means different places to different people (IST is India,
Ireland, or Israel) is asked about instead of guessed.
"""

from __future__ import annotations

import re
import zoneinfo
from functools import cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

EVERYDAY = {
    "eastern": "America/New_York",
    "est": "America/New_York",
    "edt": "America/New_York",
    "et": "America/New_York",
    "central": "America/Chicago",
    "cst": "America/Chicago",
    "cdt": "America/Chicago",
    "ct": "America/Chicago",
    "mountain": "America/Denver",
    "mst": "America/Denver",
    "mdt": "America/Denver",
    "pacific": "America/Los_Angeles",
    "pst": "America/Los_Angeles",
    "pdt": "America/Los_Angeles",
    "pt": "America/Los_Angeles",
    "alaska": "America/Anchorage",
    "hawaii": "Pacific/Honolulu",
    "arizona": "America/Phoenix",
    "gmt": "UTC",
    "utc": "UTC",
    "z": "UTC",
    "zulu": "UTC",
    "cat": "Africa/Maputo",
    "central africa": "Africa/Maputo",
    "eat": "Africa/Nairobi",
    "east africa": "Africa/Nairobi",
    "wat": "Africa/Lagos",
    "west africa": "Africa/Lagos",
    "sast": "Africa/Johannesburg",
}
"""Everyday names for a zone. The North American abbreviations read the
North American way, as an American church or business means them."""

AMBIGUOUS = {"ist", "bst", "ast", "sst", "cet", "eet", "wet", "aest", "aedt"}
"""Abbreviations that name more than one place, or a zone whose summer and
winter names differ: asked about, never guessed."""

COUNTRY_ALIASES = {
    "us": "united states",
    "usa": "united states",
    "u.s.": "united states",
    "u.s.a.": "united states",
    "america": "united states",
    "united states of america": "united states",
    "uk": "britain (uk)",
    "united kingdom": "britain (uk)",
    "great britain": "britain (uk)",
    "britain": "britain (uk)",
    "england": "britain (uk)",
    "scotland": "britain (uk)",
    "wales": "britain (uk)",
    "burma": "myanmar (burma)",
    "myanmar": "myanmar (burma)",
    "swaziland": "eswatini (swaziland)",
    "eswatini": "eswatini (swaziland)",
    "south korea": "korea (south)",
    "north korea": "korea (north)",
    "drc": "congo (dem. rep.)",
    "democratic republic of the congo": "congo (dem. rep.)",
}

_OFFSET = re.compile(r"^(utc|gmt)?\s*[+\-\u2212]\s*\d{1,2}(:?\d{2})?$")
_TRAILING = re.compile(r"\s+(standard time|daylight time|time zone|timezone|time)$")


class ZoneAnswerError(ValueError):
    """An answer that names no single zone; the message is what to ask next."""


def _rows(path: Path) -> list[list[str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [line.split("\t") for line in lines if line and not line.startswith("#")]


@cache
def _data() -> dict:
    """Which zones each country has and which zone each city names, from the
    first TZPATH directory holding both IANA tables; empty when none does."""
    countries: dict[str, list[str]] = {}
    cities: dict[str, str] = {}
    for base in map(Path, zoneinfo.TZPATH):
        zone_tab, iso_tab = base / "zone.tab", base / "iso3166.tab"
        if not (zone_tab.is_file() and iso_tab.is_file()):
            continue
        names = {row[0]: row[1] for row in _rows(iso_tab) if len(row) > 1}
        for row in _rows(zone_tab):
            if len(row) < 3 or row[0] not in names:
                continue
            countries.setdefault(names[row[0]].casefold(), []).append(row[2])
            cities.setdefault(row[2].rsplit("/", 1)[-1].replace("_", " ").casefold(), row[2])
        break
    return {"countries": countries, "cities": cities}


def zone_names() -> set[str]:
    """Every zone name the resolver can return."""
    data = _data()
    zones = {z for zs in data["countries"].values() for z in zs}
    return zones | set(EVERYDAY.values()) | {"UTC"}


def _normal(text: str) -> str:
    text = re.sub(r"\(.*?\)", " ", text).casefold()
    text = re.sub(r"\s+", " ", text).strip(" .,")
    while (stripped := _TRAILING.sub("", text)) != text:
        text = stripped
    return text


def resolve_zone(said: str) -> str:
    """The IANA zone name ``said`` means, or ZoneAnswerError saying what to ask."""
    raw = str(said).strip()
    if not raw:
        raise ZoneAnswerError("Which city or country are you in?")
    by_case = {z.casefold(): z for z in zone_names()}
    if raw.casefold() in by_case:
        return by_case[raw.casefold()]
    if "/" in raw:
        try:
            ZoneInfo(raw)
            return raw
        except (ZoneInfoNotFoundError, ValueError):
            pass
    text = _normal(raw)
    if text in EVERYDAY:
        return EVERYDAY[text]
    if text in AMBIGUOUS:
        raise ZoneAnswerError(f"{raw!r} can mean more than one place. Which city are you near?")
    data = _data()
    country = COUNTRY_ALIASES.get(text, text)
    zones = data["countries"].get(country)
    if zones:
        if len(zones) == 1:
            return zones[0]
        raise ZoneAnswerError(f"{raw} has more than one time zone. Which city are you near?")
    if text in data["cities"]:
        return data["cities"][text]
    if _OFFSET.match(_normal(raw.replace("(", " ").replace(")", " "))):
        raise ZoneAnswerError(
            f"{raw!r} is an offset from UTC, which doesn't say when the clocks change. "
            "Which city or country are you in?"
        )
    raise ZoneAnswerError(
        f"I couldn't find a time zone for {raw!r}. Which city or country are you in?"
    )


__all__ = ["ZoneAnswerError", "resolve_zone", "zone_names"]
