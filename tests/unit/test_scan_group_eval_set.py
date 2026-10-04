"""The ``scan_group`` eval set: the combined-scan grouper is gated too.

Phase 7 row 7.13b (issue #272). Row 7.13 (PR #271) built the gate and left
``scan_group`` ungated with a reason: its reply contract, ``GroupingReply``,
landed with row 7.11 (PR #269) after the gate was written, so there was no
class for a case to validate into. Both are on main now, so the set can
exist, and this file is the row's eval.

What it proves, beyond the generic checks every gated job already gets in
``test_llm_eval_sets.py`` (3 to 5 cases, a job file, green well-formed
results, each document the PDF of its case text):

- ``scan_group`` is GATED, and ships green results for both models the
  shipped tenants name: ``default`` on the live seat, and ``fixture-model``
  on the demo tenant and every ``engine init`` tenant;
- the set declares the contract the LIVE grouper validates into, and its
  probe prompt is the site's own brief word for word, so a site prompt
  reworded out from under the set is caught here instead of silently moving
  every score;
- each case's expectation itself satisfies the coverage rule the split pass
  enforces in code: every page of that case's document, exactly once;
- the one thing a case CANNOT state. A case says what a right answer looks
  like; the harness has no "expected refusal" field, and the refusal does
  not belong to the model anyway. ``_parse_groups_payload`` +
  ``validate_groups`` are the code that rejects a grouping listing a page
  twice or dropping one, so that half is proved here directly, against a
  real case document, and the case note says which failure mode it guards.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.agents.expenses.scan_split import (
    GroupingReply,
    _parse_groups_payload,
    pdf_page_count,
    validate_groups,
)
from core.engine.config import load_tenant
from core.llm.evals import (
    EvalCase,
    check_eval_gate,
    eval_sets_root,
    gated_jobs,
    load_job,
    results_path,
)

JOB = "scan_group"
REPO = Path(__file__).resolve().parents[2]
TENANTS = REPO / "tenants"

# The four cases, and the shape of the scan each one is about.
EXPECTED_GROUPS = {
    "a_terms_page_belongs_to_its_receipt": [[1, 2], [3]],
    "one_receipt_over_three_pages": [[1, 2, 3]],
    "three_receipts_one_page_each": [[1], [2], [3]],
    "two_receipts_split_after_page_two": [[1, 2], [3, 4]],
}


def _cases() -> dict[str, EvalCase]:
    return {case.name: case for case in load_job(JOB).cases}


def _pages(case: EvalCase) -> list[list[int]]:
    return [list(group["pages"]) for group in case.expect["groups"]]


def _document(case: EvalCase) -> Path:
    return eval_sets_root() / JOB / "documents" / case.document


# ---- the set exists and is gated --------------------------------------------------


def test_scan_group_is_gated():
    """The rule from row 7.13: a job that owns cases is gated. This row is
    what turns the gate on for the grouper."""
    assert JOB in gated_jobs()


def test_it_ships_green_results_for_every_model_the_shipped_tenants_name():
    """``default`` is the seat (the flat-rate tier a live tenant uses) and
    ``fixture-model`` is the demo tenant and every tenant ``engine init``
    renders. A set that arrives without both refuses a tenant that loads
    today."""
    for model_id, adapter, seeded in (
        ("default", "claude_agent_sdk", False),
        ("fixture-model", "fixture", True),
    ):
        path = results_path(JOB, model_id)
        assert path.exists(), f"{path} is the evidence the gate reads"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["failed"] == 0
        assert data["cases"] == len(EXPECTED_GROUPS)
        assert data["adapter"] == adapter
        assert data["seeded"] is seeded


@pytest.mark.parametrize("slug", ["demo"])
def test_the_shipped_tenants_still_load_with_the_grouper_gated(slug: str):
    """Both shipped tenants point ``scan_group`` at a tier; adding the set is
    what makes that assignment need evidence. The 08:00 run and the nightly
    auditor see no change only if this passes."""
    cfg = load_tenant(slug, tenants_root=TENANTS)
    assert cfg.llm.jobs[JOB]
    check_eval_gate(cfg.llm, tenant=slug)


# ---- the set measures the live contract -------------------------------------------


def test_the_set_declares_the_contract_the_live_grouper_validates_into():
    assert load_job(JOB).output_model() is GroupingReply


def test_the_probe_prompt_is_the_sites_own_brief():
    """The set carries its own copy of the prompt (row 7.13 decision 5), and
    this is the drift alarm that copy needs: if the site's brief changes, a
    reviewer decides whether the set is re-scored on the new wording or
    deliberately left on the old one, rather than nobody noticing.

    The one edit is the page count. The site formats ``{pages}`` per scan;
    the eval's ``user`` is one string for the whole set, so the harness
    substitutes ``{page_count}`` from the case's own document.
    """
    from core.agents.expenses.scan_split import _GROUP_ASK, _GROUP_SPEC

    spec = load_job(JOB)
    assert spec.system == _GROUP_SPEC
    assert spec.user == _GROUP_ASK.replace("{pages}", "{page_count}")


def test_the_reply_contract_accepts_a_grouping_the_code_then_refuses():
    """Invariant 2, stated as a test: the contract owns the SHAPE and the
    code owns the COVERAGE. A reply listing page 2 twice validates fine, so
    nothing but ``validate_groups`` stands between it and a wrong split."""
    reply = GroupingReply.model_validate({"groups": [{"pages": [1, 2]}, {"pages": [2, 3]}]})
    groups = _parse_groups_payload([g.model_dump() for g in reply.groups], "eval")
    assert validate_groups(groups, 3), "the code check is the one that refuses this"


# ---- the cases --------------------------------------------------------------------


def test_the_set_covers_whole_split_and_multiway_scans():
    """Four shapes, one each: a multi-page single receipt left WHOLE (the
    hotel-folio shape), a clean two-receipt split, a three-receipt scan, and
    the page a grouper is tempted to drop."""
    cases = _cases()
    assert sorted(cases) == sorted(EXPECTED_GROUPS)
    for name, expected in EXPECTED_GROUPS.items():
        assert _pages(cases[name]) == expected, name


def test_every_case_expectation_covers_its_document_exactly_once():
    """The expectation is itself a legal grouping: ``validate_groups`` says
    so. A case whose own answer the split pass would refuse is a broken
    case, and a model that matched it would be scored green for a grouping
    the engine then holds."""
    for name, case in _cases().items():
        groups = _parse_groups_payload(case.expect["groups"], name)
        pages = pdf_page_count(_document(case))
        assert validate_groups(groups, pages) == "", name


def test_every_case_document_has_the_pages_its_expectation_groups():
    """The PDF, the inlined case text, and the expectation are one document:
    the page count pypdf reads is the number the harness tells the model and
    the number the expectation covers."""
    for name, case in _cases().items():
        pages = pdf_page_count(_document(case))
        assert pages > 1, f"{name}: a combined scan has more than one page"
        assert sorted(p for group in _pages(case) for p in group) == list(range(1, pages + 1))
        assert case.text.count("\f") + 1 == pages, f"{name}: the case text says the page count"


def test_every_case_says_why_it_exists():
    for name, case in _cases().items():
        assert len(case.note) > 40, f"{name}: the note is what a reader needs in a year"


# ---- the refusal the harness cannot state -----------------------------------------


def test_a_grouping_that_lists_a_page_twice_is_refused_by_the_code_check():
    """The negative case, expressed where the rule actually lives. A case
    file states an expected REPLY, and a refusal is not a reply: it is what
    the split pass does with one. So the bad grouping runs against a real
    case document here."""
    case = _cases()["a_terms_page_belongs_to_its_receipt"]
    pages = pdf_page_count(_document(case))
    groups = _parse_groups_payload([{"pages": [1, 2]}, {"pages": [2, 3]}], case.document)
    reason = validate_groups(groups, pages)
    assert "every page must appear exactly once" in reason


def test_a_grouping_that_drops_a_page_is_refused_by_the_code_check():
    """The live failure mode this case is about: the back side of a receipt
    carries no total and no vendor header, a grouper drops it as "not a
    receipt", and the whole scan is held with an anomaly rather than filed
    with a page missing."""
    case = _cases()["a_terms_page_belongs_to_its_receipt"]
    pages = pdf_page_count(_document(case))
    groups = _parse_groups_payload([{"pages": [1]}, {"pages": [3]}], case.document)
    reason = validate_groups(groups, pages)
    assert "[1, 3]" in reason
    assert f"{pages}-page scan" in reason
