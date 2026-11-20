"""Severity clocks on security findings (chunk 4, 2026-10-16).

Every open ``security`` issue carries one ``sev:`` label; its clock starts
the day that label went on and runs the window the charter names. The
arithmetic is pure and tested here; reading GitHub is a thin shell around it.
"""

from __future__ import annotations

import re
from datetime import date

from core.evals import security_charter
from core.evals import security_clocks as sc

TODAY = date(2026, 10, 16)


def _issue(number: int, *labels: str, on: str = "2026-10-16T12:00:00Z") -> dict:
    return {
        "number": number,
        "title": f"finding {number}",
        "labels": ["security", *labels],
        "labeled": {name: on for name in labels if name.startswith("sev:")},
    }


def test_the_windows_are_the_charters():
    text = (security_charter.repo_root() / security_charter.CHARTER).read_text()
    table = {m[1]: int(m[2]) for m in re.finditer(r"^\| `(sev:\w+)` \| (\d+) days \|", text, re.M)}
    assert table == sc.WINDOWS


def test_the_clock_starts_when_the_severity_label_goes_on():
    (clock,) = sc.clocks([_issue(7, "sev:high", on="2026-10-01T09:00:00Z")], TODAY)
    assert clock.severity == "sev:high"
    assert clock.started == date(2026, 10, 1)
    assert clock.due == date(2026, 10, 31)
    assert clock.days_left == 15 and not clock.overdue


def test_a_finding_past_its_window_is_overdue():
    (clock,) = sc.clocks([_issue(7, "sev:critical", on="2026-10-01T09:00:00Z")], TODAY)
    assert clock.overdue and clock.days_left == -8


def test_due_today_is_not_yet_overdue():
    (clock,) = sc.clocks([_issue(7, "sev:critical", on="2026-10-09T09:00:00Z")], TODAY)
    assert clock.days_left == 0 and not clock.overdue


def test_a_finding_with_no_severity_has_no_clock_and_is_named():
    (clock,) = sc.clocks([_issue(7)], TODAY)
    assert clock.severity is None and clock.due is None
    assert sc.summary([clock])["unrated"] == 1


def test_two_severities_take_the_tighter_window():
    (clock,) = sc.clocks([_issue(7, "sev:low", "sev:high")], TODAY)
    assert clock.severity == "sev:high"


def test_the_summary_counts_open_overdue_and_unrated():
    found = sc.clocks(
        [
            _issue(1, "sev:critical", on="2026-09-01T00:00:00Z"),
            _issue(2, "sev:medium"),
            _issue(3),
        ],
        TODAY,
    )
    assert sc.summary(found) == {"open": 3, "overdue": 1, "unrated": 1}


def test_the_report_puts_the_most_urgent_first():
    found = sc.clocks(
        [_issue(1, "sev:low"), _issue(2), _issue(3, "sev:critical", on="2026-09-01T00:00:00Z")],
        TODAY,
    )
    lines = sc.render(found)
    assert lines[0].startswith("#3") and "overdue" in lines[0]
    assert lines[1].startswith("#2") and "no severity" in lines[1]


def test_the_latest_labeled_event_is_the_start():
    events = [
        {"event": "labeled", "label": {"name": "sev:high"}, "created_at": "2026-10-01T00:00:00Z"},
        {"event": "unlabeled", "label": {"name": "sev:high"}, "created_at": "2026-10-02T00:00:00Z"},
        {"event": "labeled", "label": {"name": "sev:high"}, "created_at": "2026-10-05T00:00:00Z"},
        {"event": "labeled", "label": {"name": "security"}, "created_at": "2026-10-06T00:00:00Z"},
        {"event": "commented", "created_at": "2026-10-07T00:00:00Z"},
    ]
    assert sc.labeled_at(events) == {"sev:high": "2026-10-05T00:00:00Z"}
