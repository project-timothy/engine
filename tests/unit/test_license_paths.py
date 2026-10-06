"""The licensing statements name files that exist (issue #5, 2026-10-06).

CONTRIBUTING.md once said the plug shapes in ``core/contracts/`` were
Apache-2.0 under their own LICENSE, and neither the directory nor that file
existed. A sentence that says which code is under which licence has to point
at real files, so every path CONTRIBUTING.md and LICENSE name must resolve.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# A markdown link target that is not a URL or an in-page anchor.
_LINK = re.compile(r"\]\(([^)#\s]+)\)")
# An inline code span with no spaces (a command like `uv run pytest` is not a path).
_CODE = re.compile(r"`([^`\s]+)`")
# A bare file name in plain text, the only way LICENSE can name one.
_FILE = re.compile(r"\b[\w./-]+\.(?:md|py|toml|txt)\b")


def _looks_like_path(token: str) -> bool:
    return "/" in token or bool(re.search(r"\.(md|py|toml|txt)$", token))


def _named_paths(path: Path) -> set[str]:
    text = path.read_text()
    found = {t for t in _LINK.findall(text) if "://" not in t}
    found |= {t for t in _CODE.findall(text) if _looks_like_path(t)}
    if path.suffix != ".md":
        found |= set(_FILE.findall(text))
    return found


def test_the_extractor_sees_the_paths_it_must_check():
    # Pins the extractor itself: a regex that silently matches nothing would
    # make the resolution test below pass vacuously.
    assert {"LICENSE", "CLAUDE.md", "SECURITY.md"} <= _named_paths(REPO / "CONTRIBUTING.md")
    assert "CONTRIBUTING.md" in _named_paths(REPO / "LICENSE")


def test_every_path_named_in_contributing_and_license_resolves():
    missing = [
        f"{doc}: {name}"
        for doc in ("CONTRIBUTING.md", "LICENSE")
        for name in sorted(_named_paths(REPO / doc))
        if not (REPO / name.rstrip("/")).exists()
    ]
    assert not missing, missing
