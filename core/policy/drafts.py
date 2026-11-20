"""A rule drafted from a new copy of a ministry's handbook (board item
t-rule-drafting, the owner's go of 2026-10-29).

The person's own AI reads the new copy and drafts each rule; this code holds
the line. A draft goes in only when its quote is in the new copy word for
word, it reads as a rule (``parse_layer``), and no lower layer reads looser
than a higher one (``check_stricter``). A draft is then a rule like any
other: carded on its own, in force only on its own yes. Naming the rule it
``replaces`` keeps that one counting until then. A plan is never drafted
this way: its words come whole from setup and take one yes.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import replace
from pathlib import Path

from . import (
    CAPS,
    NEEDS,
    PolicyError,
    Rule,
    Rulebook,
    check_stricter,
    find_quote,
    parse_layer,
)

ASKS = frozenset(NEEDS[1:])
"""What a draft may say a purchase needs, besides an approver's OK."""


def _toml(value: object) -> str:
    """A TOML value: a JSON string or list of strings is valid TOML too."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(value)


def _table(want: dict, base: Rule | None) -> dict:
    """The [[rule]] table a draft asks for, before an id. The rule it
    replaces lends whom and what kinds it covers when the draft doesn't say."""
    table: dict = {"says": " ".join(str(want.get("says") or "").split())}
    who = want.get("who") or (list(base.who) if base else None)
    kinds = want.get("kinds") or (list(base.kinds) if base else None)
    if who and who != ["*"]:
        table["who"] = [str(w) for w in who]
    if kinds and kinds != ["*"]:
        table["kinds"] = [str(k) for k in kinds]
    for key in ("over", *CAPS):
        if str(want.get(key) or "").strip():
            table[key] = str(want[key]).replace("$", "").replace(",", "").strip()
    needs: dict = {}
    for need in want.get("needs") or []:
        if need not in ASKS:
            raise PolicyError(f"a draft's needs are {', '.join(sorted(ASKS))}, not {need!r}")
        needs[need] = True
    if str(want.get("approver") or "").strip():
        needs["approver"] = str(want["approver"]).strip()
    if needs:
        table["needs"] = needs
    source: dict = {"quote": str(want.get("quote") or "").strip()}
    if str(want.get("section") or "").strip():
        source["section"] = str(want["section"]).strip()
    if want.get("page") not in (None, ""):
        try:
            source["page"] = int(want["page"])
        except (TypeError, ValueError):
            raise PolicyError("page is the page number in the new copy") from None
    table["source"] = source
    if base is not None:
        table["replaces"] = base.id
    return table


def _block(rule_id: str, table: dict) -> str:
    lines = ["", "[[rule]]", f"id = {_toml(rule_id)}"]
    for key, value in table.items():
        if isinstance(value, dict):
            inner = ", ".join(f"{k} = {_toml(v)}" for k, v in value.items())
            lines.append(f"{key} = {{ {inner} }}")
        else:
            lines.append(f"{key} = {_toml(value)}")
    return "\n".join(lines) + "\n"


def _new_id(table: dict, taken: set[str]) -> str:
    stem = table.get("replaces") or "-".join(
        re.sub(r"[^a-z0-9]+", " ", table["says"].casefold()).split()[:5]
    )
    n = 2
    while f"{stem}-{n}" in taken:
        n += 1
    return f"{stem}-{n}"


def _same(a: Rule, b: Rule) -> bool:
    return replace(a, id="") == replace(b, id="")


def draft(
    book: Rulebook,
    file: str,
    text: str,
    want: dict,
    pages: list[str],
    *,
    roles: set[str] | None = None,
    people: set[str] | None = None,
    kinds: list[str] | None = None,
) -> tuple[Rule, str]:
    """The drafted rule and the rules file's new text, or ``PolicyError``
    saying in plain words why not. ``text`` is the file as it reads now and
    ``pages`` the new copy's words. A draft already in the file comes back
    as it is, with the text unchanged."""
    layer = next((x for x in book.layers if x.file == file), None)
    if layer is None:
        raise PolicyError(f"{file} is not one of the ministry's rules files")
    if layer.plan:
        raise PolicyError(
            "A plan isn't drafted a rule at a time: its words come whole from setup and "
            "take one yes. Ask your ministry for a new plan instead."
        )
    replaces = str(want.get("replaces") or "").strip()
    base = next((r for r in layer.rules if r.id == replaces), None) if replaces else None
    if replaces and base is None:
        raise PolicyError(f"There's no rule {replaces!r} in {layer.document} to replace.")
    table = _table(want, base)
    if not table["says"]:
        raise PolicyError("Say the rule the way Tim tells a person about it.")
    if find_quote(table["source"]["quote"], pages) is None:
        raise PolicyError(
            f"That quote isn't word for word in the new copy of {layer.document}. Quote the "
            "sentence exactly as it reads there."
        )
    rule_id = _new_id(table, {r.id for r in layer.rules})
    new_text = text.rstrip("\n") + "\n" + _block(rule_id, table)
    try:
        data = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as exc:  # pragma: no cover  (the block is built, not typed)
        raise PolicyError(f"the draft didn't write as TOML: {exc}") from exc
    try:
        new_layer = parse_layer(data, file, roles=roles, people=people, kinds=kinds)
    except PolicyError as exc:
        raise PolicyError(str(exc).replace(f"{file} rule {rule_id}: ", "")) from None
    if new_layer.copy is not None and layer.copy is not None:
        new_layer = replace(new_layer, copy=replace(new_layer.copy, path=layer.copy.path))
    drafted = next(r for r in new_layer.rules if r.id == rule_id)
    already = next((r for r in layer.rules if _same(r, drafted)), None)
    if already is not None:
        return already, text
    check_stricter([new_layer if x.file == file else x for x in book.layers])
    return drafted, new_text


def rules_file(tenant_dir: str | Path, file: str) -> Path:
    """Where a rules file lives: directly under the ministry's rules/ folder."""
    folder = (Path(tenant_dir) / "rules").resolve()
    path = (folder / file).resolve()
    if path.parent != folder or path.suffix != ".toml":
        raise PolicyError(f"{file} is not a rules file")
    return path


__all__ = ["ASKS", "draft", "rules_file"]
