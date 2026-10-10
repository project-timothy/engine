"""The [books] section of tenant.toml (issue #433, docs/tenant-kit-design.md
section 4).

[books] names the accounting system, the entity and the return it files, the
cost-object format (a project number for a business, a fund or unit code for
a nonprofit), the 1099 rules, and the nonprofit keys (funds, designated gifts,
donor acknowledgment thresholds, housing allowance). Every key is parsed and
checked now; nothing in the daily loop reads them yet. The three sites that
hard-code today's project-number format (#340) move to
[books.cost_object] on an owner coding day, with a replay proof against the
live ledger, because two of them sit on the 08:00 path.

A tenant file with no [books] (the first tenant's today) loads exactly as
before.
"""

from __future__ import annotations

import tomllib

import pytest
from pydantic import ValidationError

from core.engine.config import Books, load_tenant
from core.engine.doctor import run_doctor
from core.engine.init import init_tenant, render


def _books(**data) -> Books:
    return Books.model_validate(data)


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "tenants"
    monkeypatch.setenv("ENGINE_TENANTS_ROOT", str(root))
    monkeypatch.setenv("ENGINE_LEDGER_ROOT", str(tmp_path / "ledger"))
    return tmp_path, root


# ---- the model -------------------------------------------------------------------


def test_no_books_section_means_today_s_behavior():
    books = Books()
    assert books.system == "qbo"
    assert books.entity is None
    assert books.cost_object.pattern == ""
    assert books.nonprofit is None


@pytest.mark.parametrize(
    "entity",
    ["s-corp", "partnership", "c-corp", "sole-proprietor", "public-charity", "church"],
)
def test_the_six_entities(entity):
    assert _books(entity=entity).entity == entity


def test_an_unknown_entity_refuses():
    with pytest.raises(ValidationError, match="entity"):
        _books(entity="llc-of-mystery")


def test_the_return_follows_the_entity():
    assert _books(entity="s-corp").tax_return == "1120-S"
    assert _books(entity="partnership").tax_return == "1065"
    assert _books(entity="c-corp").tax_return == "1120"
    assert _books(entity="sole-proprietor").tax_return == "Schedule C"
    assert _books(entity="public-charity").tax_return == "990"
    assert _books(entity="church").tax_return == ""  # a church files no 990
    assert _books().tax_return == ""


# ---- the cost object (#340's shape, parsed, not yet read) ----------------------------


def test_a_cost_object_pattern_canonicalizes_through_its_groups():
    books = _books(
        cost_object={"label": "project", "pattern": r"P(\d{2})_?(\d{4})", "canonical": "P{0}_{1}"}
    )
    assert books.cost_object.canonicalize("see P262040 and P26_2043") == ["P26_2040", "P26_2043"]


def test_a_fund_code_works_the_same_way():
    books = _books(
        cost_object={"label": "fund", "pattern": r"\b(\d{2})-(\d{3})\b", "canonical": "{0}-{1}"}
    )
    assert books.cost_object.canonicalize("gift to 10-200") == ["10-200"]


def test_no_pattern_resolves_nothing():
    assert _books().cost_object.canonicalize("P26_2040") == []


def test_a_pattern_that_does_not_compile_refuses():
    with pytest.raises(ValidationError, match="pattern"):
        _books(cost_object={"pattern": "(unclosed", "canonical": "{0}"})


def test_a_canonical_format_must_fit_the_groups():
    with pytest.raises(ValidationError, match="canonical"):
        _books(cost_object={"pattern": r"P(\d{2})", "canonical": "P{0}_{1}"})


def test_a_pattern_needs_a_canonical_format():
    with pytest.raises(ValidationError, match="canonical"):
        _books(cost_object={"pattern": r"P(\d{2})"})


# ---- nonprofit keys -------------------------------------------------------------------


def test_nonprofit_defaults_carry_the_acknowledgment_thresholds():
    books = _books(entity="church", nonprofit={})
    assert books.nonprofit.acknowledgment_at == 250
    assert books.nonprofit.quid_pro_quo_above == 75


def test_funds_say_whether_they_are_restricted():
    books = _books(
        entity="public-charity",
        nonprofit={"funds": [{"name": "general"}, {"name": "field-unit", "restricted": True}]},
    )
    assert [f.restricted for f in books.nonprofit.funds] == [False, True]


def test_nonprofit_keys_on_a_business_refuse():
    with pytest.raises(ValidationError, match="nonprofit"):
        _books(entity="s-corp", nonprofit={})


# ---- templates, demo and doctor ----------------------------------------------------------


def test_a_business_template_leaves_the_entity_for_onboarding():
    files = render("acme", "A", shape="commercial-small", data_root_rel="acme-data")
    books = tomllib.loads(files["tenant.toml"])["books"]
    assert books["system"] == "qbo" and books["entity"] == ""
    assert books["cost_object"]["label"] == "project"
    assert "nonprofit" not in books


@pytest.mark.parametrize(
    "shape,entity", [("nonprofit-small", "church"), ("nonprofit-organization", "public-charity")]
)
def test_a_nonprofit_template_names_its_entity_and_funds(shape, entity):
    files = render("grace", "C", shape=shape, data_root_rel="grace-data")
    books = tomllib.loads(files["tenant.toml"])["books"]
    assert books["entity"] == entity
    assert books["cost_object"]["label"] == "fund"
    assert books["nonprofit"]["acknowledgment_at"] == 250


def test_every_shape_renders_books_that_load(world):
    _tmp, root = world
    for shape in (
        "commercial-solo",
        "nonprofit-solo",
        "nonprofit-small",
        "commercial-organization",
    ):
        slug = f"t-{shape}"
        init_tenant(slug, root=root, shape=shape, run_audit=False)
        assert load_tenant(slug, tenants_root=root).books.system == "qbo"


def test_doctor_reports_books(world):
    _tmp, root = world
    init_tenant("grace", root=root, shape="nonprofit-small", archetype="C", run_audit=False)
    init_tenant("acme", root=root, run_audit=False)
    lines = {
        slug: next(
            c for c in run_doctor(slug, tenants_root=root, env={}).checks if c.name == "books"
        )
        for slug in ("grace", "acme")
    }
    assert lines["grace"].status == "ok" and "church" in lines["grace"].detail
    assert lines["acme"].status == "skip" and "entity" in lines["acme"].detail
