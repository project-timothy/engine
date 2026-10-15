"""The institution profile (Robb's idea 1, decided 2026-10-13).

One profile per sending organization: whether Tim places a receipt in a
category, where a missionary's claims go, and the mileage rate. A small
church or an independent missionary keeps the default: Tim places it from
their words and the books pay it out. An institution with its own system
(AGWM sends claims into Sage, which does the categorizing) turns placing
off and names where claims go. When that system is down, the claim is
held and the person hears it in plain words, never an error code."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from core.adapters.pretend_institution import PretendInstitution
from core.contracts.institution import Claim, ClaimSink, InstitutionDown
from core.engine.config import InstitutionSettings
from core.institution import HELD, IRS_MILEAGE, InstitutionError, claim_sink, mileage_rate, submit

CLAIM = Claim(
    receipt="sha-1",
    person="Jon Example",
    day="2026-10-12",
    amount="4300.00",
    currency="NPR",
    usd="32.25",
    said="4,300 rupees for the bus to Pokhara",
    photo="receipts/bus.jpg",
)


def test_a_ministry_with_no_institution_places_receipts_and_pays_from_its_books():
    plain = InstitutionSettings()
    assert plain.categorizes is True and plain.submit_to == ""
    assert claim_sink(plain, store=None) is None


def test_the_irs_rate_applies_unless_the_institution_sets_its_own():
    rate, source = mileage_rate(InstitutionSettings(), date(2026, 3, 1))
    assert rate == Decimal("0.725") and source == "IRS"
    rate, source = mileage_rate(
        InstitutionSettings(name="AGWM", mileage_rate=Decimal("0.70")), date(2026, 3, 1)
    )
    assert rate == Decimal("0.70") and source == "AGWM"


def test_the_irs_table_is_dated_and_a_day_before_it_is_refused():
    assert all(isinstance(day, date) for day, _ in IRS_MILEAGE)
    assert [day for day, _ in IRS_MILEAGE] == sorted(day for day, _ in IRS_MILEAGE)
    with pytest.raises(InstitutionError, match="no IRS rate"):
        mileage_rate(InstitutionSettings(), date(2020, 1, 1))


def test_an_institution_that_categorizes_in_its_own_system_turns_placing_off():
    agwm = InstitutionSettings(name="AGWM", categorizes=False, submit_to="pretend")
    assert agwm.categorizes is False


def test_the_pretend_institution_takes_claims_and_answers_with_its_reference(tmp_path):
    sink = claim_sink(InstitutionSettings(name="AGWM", submit_to="pretend"), store=tmp_path)
    assert isinstance(sink, PretendInstitution) and isinstance(sink, ClaimSink)
    [out] = submit(sink, [CLAIM], institution="AGWM")
    assert out.status == "sent" and out.reference.startswith("PRETEND-")
    assert out.receipt == "sha-1"
    assert sink.status(out.reference) == "received"
    assert sink.sent()[0]["said"] == CLAIM.said  # what went, as the person said it


def test_when_the_institution_is_down_the_claim_is_held_in_plain_words(tmp_path):
    sink = PretendInstitution(tmp_path)
    sink.go_down()
    [out] = submit(sink, [CLAIM], institution="AGWM")
    assert out.status == HELD and out.reference == ""
    assert out.say == (
        "AGWM isn't answering right now. "
        "Tim is holding this receipt and will send it when AGWM is back."
    )
    for word in ("429", "error", "token", "timeout", "exception", "http"):
        assert word not in out.say.lower()
    assert sink.sent() == []
    sink.come_back()
    [again] = submit(sink, [CLAIM], institution="AGWM")
    assert again.status == "sent"


def test_a_surprise_from_the_institution_is_held_too_never_shown(tmp_path):
    class Broken:
        def submit(self, claims):
            raise RuntimeError("401 token not found")

        def status(self, reference):
            return ""

    [out] = submit(Broken(), [CLAIM], institution="AGWM")
    assert out.status == HELD and "401" not in out.say and "token" not in out.say


def test_an_unknown_destination_is_refused_by_name():
    with pytest.raises(InstitutionError, match="submit_to 'sage-direct'"):
        claim_sink(InstitutionSettings(name="AGWM", submit_to="sage-direct"), store=None)


def test_a_claim_never_carries_more_than_the_institution_needs():
    fields = set(Claim.__dataclass_fields__)
    assert fields == {"receipt", "person", "day", "amount", "currency", "usd", "said", "photo"}
    assert InstitutionDown.__mro__[1] is Exception or issubclass(InstitutionDown, RuntimeError)


# ---- on the door ------------------------------------------------------------------

from core.tools.receipts import Claim as Said  # noqa: E402
from tests.unit.test_door_my_words import solo  # noqa: E402, F401  (fixture)
from tests.unit.test_ministry_lines import _snap  # noqa: E402


def test_tim_places_a_receipt_from_the_words_for_a_small_ministry(solo):  # noqa: F811
    said = _snap(solo, "4,300 rupees for the bus to Pokhara", Said(Decimal("4300"), "", "rupees"))
    assert "under Travel" in said


def test_tim_never_places_a_receipt_when_the_institution_does(solo):  # noqa: F811
    toml = solo["root"] / "living-water" / "tenant.toml"
    toml.write_text(
        toml.read_text()
        + '\n[institution]\nname = "AGWM"\ncategorizes = false\nsubmit_to = "pretend"\n'
    )
    said = _snap(solo, "4,300 rupees for the bus to Pokhara", Said(Decimal("4300"), "", "rupees"))
    assert "under" not in said and said.startswith("Got it: 4,300.00 rupees")
