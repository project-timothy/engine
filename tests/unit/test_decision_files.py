"""The decision record's shape (2026-09-10): new decisions are one file each
under docs/decisions/, so two open PRs never collide on a shared table's last
line. (The frozen table of earlier decisions left with the first tenant's
private records at extraction gate 2.)"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DECISIONS = REPO / "docs" / "decisions"


def test_every_decision_file_has_the_three_header_lines():
    files = sorted(p for p in DECISIONS.glob("*.md") if p.name != "README.md")
    assert files, "docs/decisions/ holds at least the decision that created it"
    for path in files:
        assert re.match(r"^\d{4}-\d{2}-\d{2}-[a-z0-9-]+\.md$", path.name), path.name
        lines = path.read_text().splitlines()
        assert lines[0].startswith("# "), f"{path.name}: first line is the title"
        assert re.match(r"^Date: \d{4}-\d{2}-\d{2}$", lines[1]), f"{path.name}: line 2 is Date:"
        assert lines[1][6:] == path.name[:10], f"{path.name}: the Date matches the filename"
        assert re.match(
            r"^Type: (One-way door|Two-way door|Refines \d{4}-\d{2}-\d{2})", lines[2]
        ), f"{path.name}: line 3 is Type:"
        assert any(line.strip() for line in lines[3:]), f"{path.name}: has a body"
