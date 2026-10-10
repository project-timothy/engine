"""The tenant kit: shape, authority, brand, voice (docs/tenant-kit-design.md).

`engine init` renders the kit beside `tenant.toml`; this module loads it and
refuses a malformed file with a message that names the file and the fix.
Nothing in the daily loop reads the kit yet (issue #430 is the skeleton): the
loader exists so doctor can say today whether a tenant's kit would load, and
so the steps that start reading it (voice check, authority, brand) share one
set of rules.

A part that is absent is not an error. A tenant created before the kit has
none, and doctor reports it `skip`.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from ..authority import AuthorityError, parse_policy
from ..voice import SPELLINGS, VoiceError, validate_voice

FAMILIES = ("commercial", "nonprofit")
SIZES = ("solo", "small", "organization")
SHAPES = tuple(f"{family}-{size}" for family in FAMILIES for size in SIZES)
"""Two families on one backbone, three sizes each (design, "Shapes")."""

DEFAULT_SHAPE = "commercial-small"

VOICE_PRESETS = {
    "commercial": "plain-business",
    "nonprofit": "ministry-conservative-christian",
}
"""The worldview preset each family starts from; a tenant may tune or
replace it (design, section 2)."""

KIT_FILES = {
    "authority": "authority.toml",
    "brand": "kit/brand.toml",
    "voice": "kit/voice.toml",
}
"""Each kit part and its path under the tenant directory."""


class KitError(ValueError):
    """A kit file is present and malformed."""


@dataclass(frozen=True)
class Kit:
    authority: dict | None
    brand: dict | None
    voice: dict | None


def family_of(shape: str) -> str:
    return shape.split("-", 1)[0]


def _read(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise KitError(f"{path.name} is not valid TOML: {exc}") from exc


def check_authority(data: dict) -> None:
    """The whole policy must parse: the money rule, the safeguards, every
    permission, and every role a person or agent names (core/authority)."""
    try:
        parse_policy(data)
    except AuthorityError as exc:
        raise KitError(f"authority.toml: {exc}") from exc


def check_brand(data: dict) -> None:
    for table in ("colors", "fonts", "templates"):
        if table in data and not isinstance(data[table], dict):
            raise KitError(f"brand.toml [{table}] must be a table")


def check_voice(data: dict) -> None:
    """Every rule must be one `engine voice-check` can apply (core/voice)."""
    try:
        validate_voice(data)
    except VoiceError as exc:
        raise KitError(f"voice.toml: {exc}") from exc


CHECKS = {"authority": check_authority, "brand": check_brand, "voice": check_voice}


def load_part(tenant_dir: str | Path, part: str) -> dict | None:
    """One kit part, checked; ``None`` when the file is absent."""
    data = _read(Path(tenant_dir) / KIT_FILES[part])
    if data is not None:
        CHECKS[part](data)
    return data


def load_kit(tenant_dir: str | Path) -> Kit:
    """Every kit part under ``tenant_dir``. Raises ``KitError`` on the first
    malformed file; an absent part is ``None``."""
    return Kit(**{part: load_part(tenant_dir, part) for part in KIT_FILES})


__all__ = [
    "DEFAULT_SHAPE",
    "FAMILIES",
    "KIT_FILES",
    "SHAPES",
    "SIZES",
    "SPELLINGS",
    "VOICE_PRESETS",
    "Kit",
    "KitError",
    "check_authority",
    "check_brand",
    "check_voice",
    "family_of",
    "load_kit",
    "load_part",
]
