"""FAILS BY DESIGN — the decision, not the build (phase 7 row 7.18).

Proposal: ``docs/proposals/2026-09-19-registry-drift-parks-approval-cards.md``
Candidate: c9954464 (lens 19, ``registry``/``registry-drift``: one INFO open
11 nights, the same items every night, in a file whose only reader reports it).

Two contracts, written before the code exists:

1. every drift item parks exactly one card keyed on the project and the kind
   of drift, and a DECIDED item never asks again — including when next week's
   report repeats it under a new filename. A file cannot remember an answer;
   that is the whole point of the card;
2. a project the accounting system claims to have created is recorded only
   when a readback shows it (the 2026-08-03 incident: the API accepted a
   field and silently ignored it).

Fixtures are neutral placeholders on purpose: this tree is hermetic and names
no tenant, project, or person.
"""

from __future__ import annotations

REPORT = """# Project registry drift

**Drift items:** 3

- P00_0001 Project One: needs_qbo_project=true but no QBO Project (present in the project drive).
- P00_0002 Project Two: tracked but no project-drive folder.
- P00_0003: appears in only one source. Review before adding to registry.
"""

# The same three items, a week later, in a file with a different name.
LATER_REPORT = REPORT.replace("**Drift items:** 3", "**Drift items:** 3  ")


def _lane():
    """The pure drift reader this row adds. Absent today: the report's only
    reader is the auditor's lens, which reports it and takes no act."""
    from core.agents.projects import drift

    return drift


class _FakeQbo:
    """Accepts the create and reports no project on readback, which is exactly
    the failure mode the readback exists to catch."""

    def __init__(self) -> None:
        self.creates: list[str] = []

    def create_project(self, name: str, parent: str) -> dict:
        self.creates.append(name)
        return {"Id": "900", "DisplayName": name}

    def find_project(self, name: str) -> dict | None:
        return None


def test_every_drift_item_parks_one_card_and_a_decided_item_never_reasks():
    lane = _lane()
    items = lane.parse_report(REPORT)
    assert [(i.pn, i.kind) for i in items] == [
        ("P00_0001", "qbo-project-missing"),
        ("P00_0002", "folder-missing"),
        ("P00_0003", "one-source"),
    ], "each bullet is classified into exactly one kind, and none is dropped"

    cards = lane.propose_cards(items, answered=frozenset())
    assert [c.key for c in cards] == [
        "P00_0001:qbo-project-missing",
        "P00_0002:folder-missing",
        "P00_0003:one-source",
    ], "one card per item, keyed on the project and the kind, never on the filename"
    assert {c.action_type for c in cards} == {"projects.registry_drift"}

    decided = frozenset(c.key for c in cards)
    assert lane.propose_cards(lane.parse_report(LATER_REPORT), answered=decided) == [], (
        "a decided item never asks again, approved or rejected, however the next "
        "week's report names its file"
    )


def test_a_created_project_is_recorded_only_when_the_readback_shows_it():
    lane = _lane()
    client = _FakeQbo()
    item = lane.parse_report(REPORT)[0]
    result = lane.execute_project_create(client, item, parent="Customer One")

    assert client.creates == ["P00_0001 Project One"], "the write is attempted once"
    assert result.verified is False
    assert result.events == [], (
        "an unverified readback records no created event: the API can accept an "
        "entity and silently ignore a field (incident 2026-08-03), so the readback "
        "is the proof"
    )
    assert "P00_0001" in result.anomaly
    assert result.registry_writes == (), "the engine never writes the registry file"
