"""Whose account a statement is, as its file name says it (board
t-account-from-name, the owner's walkthrough of 2026-10-16).

Ruth had to say which statement was the ministry's and which was hers by
their dates, when the file names already said it ("Ministry checking", "My
own checking"). Code reads the name for words that say whose it is, the
lists in ``statement_names.toml``, and the ministry's own name; Tim asks one
question, and her yes settles them all. A name that says neither, or both,
says nothing, and Tim asks as before. Boundary classification
(docs/boundary-rules.md): **code**. No model reads the name, and the name
itself never reaches her AI: it is her phone's words, never Tim's.
"""

from __future__ import annotations

import re
import tomllib
from functools import cache
from pathlib import Path

from ..agents.expenses import statements

WORDS_FILE = Path(__file__).with_name("statement_names.toml")
MOST = 200
"""Characters of a file name Tim reads; the rest is never looked at."""


@cache
def _file() -> dict:
    return tomllib.loads(WORDS_FILE.read_text(encoding="utf-8"))


WORDS: dict[str, str] = dict(_file()["ask"])


def _words(text: str) -> list[str]:
    """A name's words, lower case: "MyChecking_2026-08.csv" is my, checking, csv."""
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text[:MOST]).replace("'", "").replace("’", "")
    return re.findall(r"[a-z]+", text.casefold())


def _without_ministry(words: list[str], ministry: str) -> tuple[list[str], bool]:
    """The name's words with the ministry's own name taken out, and whether
    it was there: its first two words or more, in order (all of it when it
    is one word)."""
    plain = set(_file()["names"]["plain"])
    own = [w for w in _words(ministry) if w not in plain]
    if not own:
        return words, False
    least = min(2, len(own))
    for length in range(len(own), least - 1, -1):
        run = own[:length]
        for i in range(len(words) - length + 1):
            if words[i : i + length] == run:
                return words[:i] + words[i + length :], True
    return words, False


def side_of(name: str, ministry: str) -> str:
    """``statements.MINISTRY`` or ``statements.OWN`` when the file's name
    says whose account it is, else "" (it says neither, or both)."""
    names = _file()["names"]
    words, named = _without_ministry(_words(str(name or "")), ministry)
    ministry_side = named or bool(set(names["ministry"]) & set(words))
    own_side = bool(set(names["own"]) & set(words))
    if ministry_side == own_side:
        return ""
    return statements.MINISTRY if ministry_side else statements.OWN


def ask(ministry: int, own: int) -> str:
    """The one question for statements whose names say it: ``ministry`` of
    them the ministry's, ``own`` of them the person's. "" for none."""
    if ministry and own:
        return WORDS["both"]
    if ministry:
        return WORDS["ministry_one" if ministry == 1 else "ministry_many"]
    if own:
        return WORDS["own_one" if own == 1 else "own_many"]
    return ""


__all__ = ["MOST", "WORDS", "WORDS_FILE", "ask", "side_of"]
