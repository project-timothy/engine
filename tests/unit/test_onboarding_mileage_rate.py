"""Setup asks a ministry what Tim pays drives back at (the owner, 2026-11-16).

Most small ministries use the IRS rate for the year, so that is the
default: the answer "IRS rate" (or nothing) leaves the ministry's rate
unset, and every drive prices at the IRS rate on its own day. A ministry
that sets its own rate gives it in cents; it lands in tenant.toml's
[institution] mileage_rate. A rate above today's IRS rate is asked again,
because anything above it counts as pay. A business is not asked, and a solo worker
gets the IRS rate unasked.
"""

from __future__ import annotations

import tomllib
from datetime import date
from decimal import Decimal

import pytest

from core.engine.config import load_tenant
from core.onboarding import OnboardingError, apply, next_question, plan, record


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "tenants"
    monkeypatch.setenv("ENGINE_TENANTS_ROOT", str(root))
    monkeypatch.setenv("ENGINE_LEDGER_ROOT", str(tmp_path / "ledger"))
    return root


CHURCH = {
    "who": "nonprofit-small",
    "legal_name": "Grace Fellowship, Inc.",
    "your_name": "Ruth Owens",
    "your_role": "treasurer",
}


def _walk(values: dict) -> tuple[dict, list[str]]:
    answers: dict = {}
    asked: list[str] = []
    while (q := next_question(answers)) is not None:
        asked.append(q["id"])
        answers = record(answers, q["id"], values.get(q["id"], q.get("default", "")))
    return answers, asked


def test_a_church_is_asked_and_the_irs_rate_is_the_default():
    _answers, asked = _walk(CHURCH)
    assert "mileage_rate" in asked
    q = next_question({k: v for k, v in _walk(CHURCH)[0].items() if k != "mileage_rate"})
    assert q["id"] == "mileage_rate"
    assert q["default"] == "IRS rate"


@pytest.mark.parametrize("who", ["commercial-solo", "commercial-small", "nonprofit-solo"])
def test_a_business_or_a_solo_worker_is_not_asked(world, who):
    """A solo missionary gets the IRS rate unasked: their sign-up stays short."""
    answers, asked = _walk(
        {"who": who, "legal_name": "Lee Works", "your_name": "Pat Lee", "your_role": "owner"}
    )
    assert "mileage_rate" not in asked
    assert plan(answers, "lee").mileage_rate is None


@pytest.mark.parametrize("said", ["IRS rate", "irs", "The IRS rate", "", "IRS"])
def test_the_irs_rate_leaves_the_ministry_rate_unset(world, said):
    answers, _ = _walk({**CHURCH, "mileage_rate": said})
    p = plan(answers, "grace")
    assert p.mileage_rate is None
    directory = apply(p, root=world, run_audit=False).tenant_dir
    assert "institution" not in tomllib.loads((directory / "tenant.toml").read_text())


@pytest.mark.parametrize(
    "said, rate",
    [("65", "0.65"), ("65 cents", "0.65"), ("$0.65", "0.65"), ("70.5", "0.705"), ("0.7", "0.70")],
)
def test_the_ministrys_own_rate_lands_in_its_institution_settings(world, said, rate):
    answers, _ = _walk({**CHURCH, "mileage_rate": said})
    p = plan(answers, "grace")
    assert p.mileage_rate == rate
    apply(p, root=world, run_audit=False)
    config = load_tenant("grace", tenants_root=world)
    assert config.institution.mileage_rate == Decimal(rate)


def test_a_rate_above_the_irs_rate_today_is_asked_again():
    answers = record({}, "who", "nonprofit-small")
    with pytest.raises(OnboardingError, match="above the IRS rate"):
        record(answers, "mileage_rate", "95", today=date(2026, 11, 16))


@pytest.mark.parametrize("said", ["a lot", "-5"])
def test_an_answer_that_is_no_rate_is_asked_again(said):
    answers = record({}, "who", "nonprofit-small")
    with pytest.raises(OnboardingError, match="mileage_rate"):
        record(answers, "mileage_rate", said, today=date(2026, 11, 16))
