"""FAILS BY DESIGN — the decision, not the build (phase 7 row 7.18).

Proposal: ``docs/proposals/2026-09-23-materiality-surveils-from-a-birth-date.md``
Candidate: e42b0ad2 (lens 19, ``materiality``/``amount-outlier``: 5 subjects in
60 days, every one an AP row that was already in the book when lens 13 was
founded on 2026-09-04, and every one silenced by a hand-researched dated mute).

Two contracts, written before the code exists:

1. a row that entered the book before the lens started watching is named ONCE,
   as a batch the owner can confirm in one sitting, instead of raising the same
   immutable WARN every night until a mute is written by hand;
2. the gate is the row's LEDGER ARRIVAL, never the date on the document. A
   back-dated row that arrives today is new money to this lens, whatever its
   invoice date says, and a rule keyed on ``invoice_date`` would exempt exactly
   the rows the hand-check lane is built to create.

Fixtures are neutral placeholders on purpose: this tree is hermetic and names
no tenant, vendor, or person. Amounts sit well over the lens's $5,000 floor so
the partition, not the threshold, is what these assertions measure.
"""

from __future__ import annotations

from datetime import date

# The lens's founding date stands in for whatever a tenant configures.
BIRTH = date(2026, 1, 1)


def _lens():
    """The partition this row adds. Absent today: lens 13 surveils every row in
    the book on every pass, forever, however old the row is."""
    from auditor.lenses import materiality

    return materiality


def _row(row_id: int, *, invoice_date: str, created_at: str) -> dict:
    """An ``ap_invoices`` row as the lens reads it, over the floor and over any
    plausible peer median."""
    return {
        "id": row_id,
        "vendor": "Placeholder Vendor",
        "invoice_number": f"PLACEHOLDER-{row_id}",
        "invoice_date": invoice_date,
        "amount_cents": 900_000,
        "created_at": created_at,
    }


def test_a_pre_birth_row_is_acknowledged_once_not_nagged_nightly():
    lens = _lens()

    # Arrived before the lens existed: its amount, its date and its vendor's
    # median can never change, so the nightly verdict can never change either.
    backlog_row = _row(1, invoice_date="2025-11-04", created_at="2025-11-04T10:00:00+00:00")
    # Arrived after: ordinary surveillance, the reason the lens exists.
    fresh_row = _row(2, invoice_date="2026-03-05", created_at="2026-03-05T10:00:00+00:00")

    surveilled, backlog = lens.partition_by_arrival([backlog_row, fresh_row], surveil_from=BIRTH)

    assert [r["id"] for r in surveilled] == [2], (
        "a row that arrived after the birth date is surveilled exactly as today"
    )
    assert [r["id"] for r in backlog] == [1], (
        "a row already in the book when the lens was founded is backlog, not a "
        "nightly WARN that only a hand-written mute can silence"
    )

    finding = lens.backlog_finding(backlog)
    assert finding is not None, "the backlog is reported, never dropped silently"
    assert finding.severity == "INFO"
    assert finding.condition == "materiality-backlog"
    assert "1" in finding.detail, (
        "the one finding names how many rows it stands for, so nothing is "
        "excluded without the report saying so"
    )

    # An absent knob is the default configuration, not an opt-out: a tenant that
    # sets nothing sees byte-identical output to today's.
    surveilled, backlog = lens.partition_by_arrival([backlog_row, fresh_row], surveil_from=None)
    assert [r["id"] for r in surveilled] == [1, 2]
    assert backlog == []
    assert lens.backlog_finding(backlog) is None


def test_a_backdated_row_that_arrived_after_the_birth_date_is_still_surveilled():
    lens = _lens()

    # The live shape this guards: two rows entered the book on 2026-09-17
    # carrying invoice dates from the previous March. A gate on the document's
    # date would have exempted them on the one day they mattered.
    backdated = _row(3, invoice_date="2025-03-05", created_at="2026-09-17T14:09:38+00:00")

    surveilled, backlog = lens.partition_by_arrival([backdated], surveil_from=BIRTH)

    assert [r["id"] for r in surveilled] == [3], (
        "the gate is when the book learned the fact, never the date printed on "
        "the document: a back-dated row that arrives today is new money here"
    )
    assert backlog == [], (
        "an old invoice date is not an old row; the hand-check lane exists to "
        "create exactly this shape and it must stay visible to the lens"
    )
