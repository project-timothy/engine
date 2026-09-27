"""Every CI action is pinned to a full commit SHA (invariant 9, applied to CI).

A tag like ``@v6`` can be repointed by whoever controls the action's
repository, and the job that runs it holds this repository's secrets. A SHA
cannot move. The trailing comment names the tag the SHA was taken from, so an
upgrade is a reviewed diff of both.
"""

from __future__ import annotations

import re
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
USES = re.compile(r"^\s*(?:-\s*)?uses:\s*(\S+)(.*)$")
PINNED = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")


def _uses():
    for wf in sorted(WORKFLOWS.glob("*.y*ml")):
        for n, line in enumerate(wf.read_text().splitlines(), 1):
            m = USES.match(line)
            if m and not m.group(1).startswith("./"):
                yield wf.name, n, m.group(1), m.group(2)


def test_there_are_actions_to_check():
    assert list(_uses())


def test_every_action_is_pinned_to_a_commit_sha_with_its_tag_named():
    bad = [
        f"{wf}:{n} {ref}"
        for wf, n, ref, rest in _uses()
        if not PINNED.match(ref) or not re.search(r"#\s*v\d", rest)
    ]
    assert not bad, "pin by SHA with a '# vN' comment: " + ", ".join(bad)
