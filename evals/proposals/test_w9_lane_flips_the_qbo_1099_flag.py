"""FAILS BY DESIGN — the decision, not the build (phase 7 row 7.18).

Proposal: ``docs/proposals/2026-09-18-w9-lane-flips-the-qbo-1099-flag.md``
Candidate: 1fd15cf3 (lens 19, ``vendor-1099``/``flag-missing``: 7 taxpayers in
60 days, every one cleared by the same owner click in the accounting UI).

Two contracts, written before the code exists:

1. a taxpayer due a 1099-NEC whose matched vendor record carries the flag off
   parks exactly one card, and nothing else does;
2. a write whose readback does not show the flag on is NEVER recorded as a
   flip (the 2026-08-03 incident: the API accepted a Preferences update and
   silently ignored it, and a no-op write had "verified" it beforehand).

Fixtures are neutral placeholders on purpose: this tree is hermetic and names
no tenant, vendor, or person.
"""

from __future__ import annotations

FLOOR_CENTS = 60_000  # the $600 1099-NEC reporting floor

# taxpayer -> the registry picture the rule reads (vendors.toml + Paid rows)
TAXPAYERS = [
    # due: over the floor, W-9 on file, no exempt classification
    {
        "taxpayer": "Contractor One",
        "names": ("Contractor One", "contractor one llc"),
        "paid_cents": 940_000,
        "classification": "",
        "w9": True,
    },
    # exempt: the registry says corporation, so no 1099-NEC is due
    {
        "taxpayer": "Supplier Two",
        "names": ("Supplier Two",),
        "paid_cents": 2_700_000,
        "classification": "c-corp",
        "w9": True,
    },
    # under the floor
    {
        "taxpayer": "Contractor Three",
        "names": ("Contractor Three",),
        "paid_cents": 42_000,
        "classification": "",
        "w9": True,
    },
    # due, and the flag is already on: nothing to ask
    {
        "taxpayer": "Contractor Four",
        "names": ("Contractor Four",),
        "paid_cents": 150_000,
        "classification": "individual/sole-prop",
        "w9": True,
    },
]

# the accounting system's vendor list, as the read side returns it
QBO_VENDORS = [
    {"id": "101", "display_name": "Contractor One LLC", "vendor_1099": False},
    {"id": "102", "display_name": "Supplier Two", "vendor_1099": False},
    {"id": "103", "display_name": "Contractor Three", "vendor_1099": False},
    {"id": "104", "display_name": "Contractor Four", "vendor_1099": True},
]


def _lane():
    """The pure proposer this row adds. Absent today: the flip is a UI act."""
    from core.agents.ap import vendor_1099_flags

    return vendor_1099_flags


class _FakeQbo:
    """Accepts the update and reports the flag still off, which is exactly the
    failure mode the readback exists to catch."""

    def __init__(self) -> None:
        self.updates: list[str] = []

    def update_vendor_1099(self, vendor_id: str, value: bool) -> None:
        self.updates.append(vendor_id)

    def read_vendor(self, vendor_id: str) -> dict:
        return {"id": vendor_id, "display_name": "Contractor One LLC", "vendor_1099": False}


def test_a_1099_due_vendor_with_the_qbo_flag_off_parks_one_card():
    lane = _lane()
    proposals = lane.propose_flag_flips(
        TAXPAYERS, QBO_VENDORS, floor_cents=FLOOR_CENTS, tax_year=2026
    )
    assert [p.taxpayer for p in proposals] == ["Contractor One"], (
        "only a taxpayer due a 1099-NEC whose matched vendor carries the flag off is asked "
        "about: an exempt classification, a total under the floor, and a flag already on "
        "each produce nothing"
    )
    only = proposals[0]
    assert only.vendor_ids == ("101",)  # matched on the ledger alias, exact, never fuzzy
    assert only.action_type == "ap.qbo_vendor_1099_flag"
    assert only.paid_cents == 940_000


def test_an_unverified_readback_never_records_the_flip():
    lane = _lane()
    client = _FakeQbo()
    result = lane.execute_flag_flip(client, vendor_id="101", taxpayer="Contractor One")
    assert client.updates == ["101"], "the write is attempted once"
    assert result.verified is False
    assert result.events == [], (
        "an unverified readback records no flip event: the API can accept a field and "
        "silently ignore it (incident 2026-08-03), so the readback is the proof"
    )
    assert "101" in result.anomaly
