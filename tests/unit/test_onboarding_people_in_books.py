"""Onboarding puts the tenant's own people in its books (Tim walkthrough 1, 2026-10-09).

Every tenant the walkthrough onboarded (a solo missionary, a church, an
independent nonprofit) came out with the template's placeholder people,
Pat Owner, Sam Staffer and Vic Vendor, as its expense filers and its owner,
with drop folders in their names and none for the real people. The fix is in
onboarding, not the template: `engine init` without answers still renders the
placeholders an owner replaces by hand.

Who files expenses is read from the rules the answers made: a person whose
role may submit gets a drop folder. A nonprofit has no owners; a business
owner is its owner.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.engine.config import load_tenant
from core.onboarding import apply, next_question, plan, record

PLACEHOLDERS = ("Pat Owner", "Sam Staffer", "Vic Vendor")


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "tenants"
    monkeypatch.setenv("ENGINE_TENANTS_ROOT", str(root))
    monkeypatch.setenv("ENGINE_LEDGER_ROOT", str(tmp_path / "ledger"))
    return tmp_path, root


def _onboard(root, slug: str, values: dict):
    answers: dict = {}
    while (q := next_question(answers)) is not None:
        answers = record(answers, q["id"], values.get(q["id"], q.get("default", "")))
    apply(plan(answers, slug), root=root, run_audit=False)
    return load_tenant(slug, tenants_root=root)


def _persons(cfg) -> dict[str, str]:
    return {p.name: p.role for p in cfg.expenses.persons}


def test_a_church_files_expenses_for_its_treasurer_and_missionaries(world):
    _tmp, root = world
    cfg = _onboard(
        root,
        "grace",
        {
            "who": "nonprofit-small",
            "legal_name": "Grace Fellowship",
            "your_name": "Carol Jennings",
            "your_role": "treasurer",
            "people": "Mark Ellison = pastor\nRuth Hollis = missionary\nDon Pruitt = board",
        },
    )
    assert _persons(cfg) == {"Carol Jennings": "employee", "Ruth Hollis": "employee"}
    assert cfg.close.owner_names == []
    assert cfg.expenses.inbox_person == "Carol Jennings"
    folders = {p.name for p in Path(cfg.expenses.drop_dir).iterdir()}
    assert {"Carol Jennings", "Ruth Hollis"} <= folders
    assert not set(PLACEHOLDERS) & folders


def test_a_solo_missionary_files_their_own_and_the_reviewer_files_none(world):
    _tmp, root = world
    cfg = _onboard(
        root,
        "water",
        {
            "who": "nonprofit-solo",
            "legal_name": "Living Water Mozambique, Inc.",
            "your_name": "Daniel Reyes",
            "reviewer": "Linda Park",
        },
    )
    assert _persons(cfg) == {"Daniel Reyes": "employee"}
    assert cfg.close.owner_names == []


def test_a_business_owner_is_the_owner(world):
    _tmp, root = world
    cfg = _onboard(
        root,
        "shop",
        {"who": "commercial-solo", "legal_name": "Lee Studio LLC", "your_name": "Sam Lee"},
    )
    assert _persons(cfg) == {"Sam Lee": "owner"}
    assert cfg.close.owner_names == ["Sam Lee"]


def test_no_placeholder_name_survives_onboarding(world):
    _tmp, root = world
    _onboard(
        root,
        "grace",
        {
            "who": "nonprofit-small",
            "legal_name": "Grace",
            "your_name": "Carol",
            "your_role": "treasurer",
        },
    )
    text = (root / "grace" / "tenant.toml").read_text()
    for name in PLACEHOLDERS:
        assert name not in text, name
