""" "Keep what you flagged" keeps the lines Tim flagged (board t-keep-flagged,
the owner's walkthrough of 2026-10-16).

Tim already flags the likely ministry lines on a person's own account, but
she still named each one. Now "keep what you flagged" (plus any she adds,
like the supermarket) keeps those and drops the rest in one sentence. Code
decides which lines those are: only the ones Tim showed her as flagged on
that statement. A line never shown that way can't be kept by it.

Daniel (made up, as in test_door_my_words) runs Living Water alone.
"""

from __future__ import annotations

from core.agents.expenses import statements as st
from tests.unit.test_bank_catch_up import _daniel, _events, _everything
from tests.unit.test_catch_up import _send
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_statements import OWN_CSV


def _walked(solo):  # noqa: F811
    door, desk, token = _daniel(solo)
    _send(solo, desk, token, OWN_CSV.encode())
    [held] = door.call("bank_statement", {})["statements"]
    walked = door.call("bank_statement", {"statement": held["statement"], "account": "own"})
    return door, held["statement"], walked


def test_keep_what_you_flagged_keeps_only_the_flagged_lines_and_what_she_adds(solo):  # noqa: F811
    door, statement, walked = _walked(solo)
    assert "flagged true" in walked["say"]
    flagged = {r["words"] for r in walked["rows"] if r["looks"]}
    assert flagged >= {"TAXI KATHMANDU", "REMITLY"}
    supermarket = next(
        r["row"] for r in walked["rows"] if r["words"] == "BHATBHATENI SUPERMARKET KATHMANDU"
    )
    kept = door.call(
        "keep_own_lines",
        {
            "statement": statement,
            "flagged": True,
            "rows": [supermarket],
            "said": "keep what you flagged, and the supermarket too",
        },
    )
    assert kept["status"] == "kept", kept
    words = {e["words"] for e in _events(solo, st.LINE_EVENT)}
    assert words == flagged | {"BHATBHATENI SUPERMARKET KATHMANDU"}
    assert kept["lines"] == len(words)
    box = _everything(solo)
    assert "NETFLIX" not in box and "PAYROLL" not in box


def test_keep_what_you_flagged_alone_needs_no_rows(solo):  # noqa: F811
    door, statement, walked = _walked(solo)
    flagged = {r["words"] for r in walked["rows"] if r["looks"]}
    kept = door.call(
        "keep_own_lines", {"statement": statement, "flagged": True, "said": "keep what you flagged"}
    )
    assert kept["status"] == "kept", kept
    assert {e["words"] for e in _events(solo, st.LINE_EVENT)} == flagged


def test_nothing_is_kept_by_flags_she_was_never_shown(solo):  # noqa: F811
    door, desk, token = _daniel(solo)
    _send(solo, desk, token, OWN_CSV.encode())
    [held] = door.call("bank_statement", {})["statements"]
    kept = door.call(
        "keep_own_lines",
        {"statement": held["statement"], "flagged": True, "said": "keep what you flagged"},
    )
    assert kept["status"] == "which", kept
    assert "bank_statement" in kept["say"]
    assert _events(solo, st.LINE_EVENT) == []
    # Still waiting, for her picks.
    assert door.call("bank_statement", {})["statements"]


def test_a_line_never_flagged_is_never_kept_by_the_flags(solo):  # noqa: F811
    door, statement, _walked_ = _walked(solo)
    door.call(
        "keep_own_lines", {"statement": statement, "flagged": True, "said": "keep what you flagged"}
    )
    words = {e["words"] for e in _events(solo, st.LINE_EVENT)}
    assert "NETFLIX.COM" not in words and "BHATBHATENI SUPERMARKET KATHMANDU" not in words


def test_she_still_names_each_line_when_she_likes(solo):  # noqa: F811
    door, statement, walked = _walked(solo)
    taxi = next(r["row"] for r in walked["rows"] if r["words"] == "TAXI KATHMANDU")
    kept = door.call("keep_own_lines", {"statement": statement, "rows": [taxi], "said": "taxi"})
    assert kept["status"] == "kept" and kept["lines"] == 1


def test_neither_rows_nor_flags_keeps_nothing(solo):  # noqa: F811
    door, statement, _walked_ = _walked(solo)
    said = door.call("keep_own_lines", {"statement": statement, "said": "hmm"})
    assert said["status"] == "which"
    assert _events(solo, st.LINE_EVENT) == []
