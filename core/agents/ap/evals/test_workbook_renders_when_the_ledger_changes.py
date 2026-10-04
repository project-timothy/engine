"""Built 2026-10-04 (#326); written as the decision on 2026-09-20 (phase 7 row 7.18).

Proposal: ``docs/proposals/2026-09-20-the-workbook-renders-when-the-ledger-changes.md``
Candidate: 951aaf6d (lens 19, ``book``/``missing-from-sheet``: 7 subjects in 60
days, every one a ledger row written after the morning's render and reported
CRITICAL until the next one).

Two contracts, written before the code exists:

1. a committed change to the AP book renders the delivered view once, at the
   end of the invocation that made it — including a write that happens outside
   any job run, which is how two of the six CRITICALs of 2026-09-18 were made
   and which no run-level hook can see;
2. a render that fails NEVER rolls back the accounting write that triggered it.
   The ledger is truth (invariant 1) and the workbook is a photograph of it; a
   delivery view that can eat an AP write has the relationship backwards.

Fixtures are neutral placeholders on purpose: this tree is hermetic and names
no tenant, vendor, or person. The keys below stand in for ``_workbook_key``,
the hash the workbook job already computes over every row's
``(id, status, amount_cents, updated_at)`` plus the column layout, the output
path and the view version.
"""

from __future__ import annotations

TENANT = "placeholder-tenant"

KEY_AT_RENDER = "key-as-the-morning-render-left-it"
KEY_AFTER_INSERT = "key-once-a-paid-row-was-inserted"
KEY_AFTER_STATUS = "key-once-a-status-was-flipped"


def _lane():
    """The refresh lane this row adds. Absent today: the render is reachable
    only as a scheduled job, once a morning."""
    from core.agents.ap import view_refresh

    return view_refresh


class _Renderer:
    """Stands in for the existing ``workbook`` handler, which is already
    idempotent on the key and is reused unchanged by this row."""

    def __init__(self) -> None:
        self.rendered: list[str] = []

    def __call__(self, tenant: str) -> None:
        self.rendered.append(tenant)


class _FailingRenderer:
    """openpyxl refuses the write (a locked file, a full disk). The AP row that
    triggered this is already committed and must stay that way."""

    def __call__(self, tenant: str) -> None:
        raise RuntimeError("openpyxl refused the workbook write")


def test_a_write_outside_the_render_job_leaves_the_view_stale():
    lane = _lane()

    # One row inserted after the morning render — the hand-backfill path, which
    # writes ap_status_history and no event, so nothing run-shaped observes it.
    renderer = _Renderer()
    refresh = lane.ViewRefresh(TENANT, render=renderer, rendered_key=KEY_AT_RENDER)
    refresh.mark_dirty(KEY_AFTER_INSERT)
    result = refresh.drain()
    assert renderer.rendered == [TENANT], (
        "a committed AP write renders the delivered view; leaving it for the next "
        "morning is what raises a CRITICAL the owner cannot act on"
    )
    assert result.rendered == 1

    # Five writes in one invocation are one photograph, not five.
    renderer = _Renderer()
    refresh = lane.ViewRefresh(TENANT, render=renderer, rendered_key=KEY_AT_RENDER)
    for key in (KEY_AFTER_INSERT, KEY_AFTER_INSERT, KEY_AFTER_STATUS, KEY_AFTER_STATUS):
        refresh.mark_dirty(key)
    refresh.mark_dirty(KEY_AFTER_INSERT)
    assert refresh.drain().rendered == 1, "the drain is per invocation, never per row"
    assert renderer.rendered == [TENANT]

    # A write that moves nothing the view shows (a note, a QBO id) costs nothing:
    # the existing key already excludes those columns.
    renderer = _Renderer()
    refresh = lane.ViewRefresh(TENANT, render=renderer, rendered_key=KEY_AT_RENDER)
    refresh.mark_dirty(KEY_AT_RENDER)
    assert refresh.drain().rendered == 0
    assert renderer.rendered == []


def test_a_render_failure_never_rolls_back_the_ledger_write():
    lane = _lane()
    committed_rows = ["the AP row whose commit marked the view dirty"]

    refresh = lane.ViewRefresh(TENANT, render=_FailingRenderer(), rendered_key=KEY_AT_RENDER)
    refresh.mark_dirty(KEY_AFTER_INSERT)
    result = refresh.drain()  # must not raise: the drain is after the commit

    assert result.rendered == 0
    assert "openpyxl refused the workbook write" in result.anomaly, (
        "a render failure is an anomaly on the run, named plainly"
    )
    assert committed_rows == ["the AP row whose commit marked the view dirty"], (
        "the accounting write stands: the ledger is truth and the workbook is a "
        "photograph of it, so the photograph never eats the book"
    )
