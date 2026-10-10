"""Onboarding takes answers in the words people use (Tim walkthrough 1, 2026-10-09).

Three personas answered the setup questions the way people do. Ruth (61,
rural Nepal) said "nepal" for her time zone, Carol (a church treasurer) said
"Eastern," and Daniel said "CAT (UTC+2)." The engine stored all three as
typed, doctor called each tenant ok, and the read server then died on start
(ZoneInfoNotFoundError): a missionary gets silence. Meanwhile "january" was
refused as a month and "volunteer treasurer" as a role. These tests hold the
exact walkthrough answers.

The rule for time: the engine keeps instants in UTC and each tenant's IANA
zone name (never a fixed offset, which carries no daylight-saving rules), so
an answer resolves to a real zone name or is asked again in plain words.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

from core.onboarding import OnboardingError, load_questions, record
from core.onboarding.zones import ZoneAnswerError, resolve_zone, zone_names

SOLO = {"who": "nonprofit-solo"}
CHURCH = {"who": "nonprofit-small"}


# ---- time zones -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("said", "zone"),
    [
        ("nepal", "Asia/Kathmandu"),  # Ruth
        ("Eastern", "America/New_York"),  # Carol
        ("CAT (UTC+2)", "Africa/Maputo"),  # Daniel
        ("Africa/Maputo", "Africa/Maputo"),
        ("asia/kathmandu", "Asia/Kathmandu"),
        ("Kathmandu", "Asia/Kathmandu"),
        ("Mozambique", "Africa/Maputo"),
        ("eastern time", "America/New_York"),
        ("Pacific", "America/Los_Angeles"),
        ("New York", "America/New_York"),
        ("GMT", "UTC"),
        ("UTC", "UTC"),
    ],
)
def test_a_time_zone_answer_resolves_to_a_real_zone_name(said, zone):
    assert resolve_zone(said) == zone
    assert record(SOLO, "timezone", said)["timezone"] == zone


@pytest.mark.parametrize(
    ("said", "hint"),
    [
        ("United States", "more than one time zone"),
        ("UTC+2", "city"),
        ("Mars", "city"),
        ("IST", "city"),
    ],
)
def test_an_answer_that_names_no_single_zone_is_asked_again_in_plain_words(said, hint):
    with pytest.raises(ZoneAnswerError, match=hint) as caught:
        resolve_zone(said)
    assert "iana" not in str(caught.value).lower()
    with pytest.raises(OnboardingError):
        record(SOLO, "timezone", said)


def test_every_zone_the_resolver_can_return_loads_on_this_host():
    for name in zone_names():
        ZoneInfo(name)


def test_the_time_zone_question_never_says_iana():
    q = next(q for q in load_questions() if q.id == "timezone")
    assert "iana" not in (q.ask + " " + q.help).lower()


# ---- months --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("said", "month"),
    [("january", "1"), ("Jan", "1"), ("July", "7"), ("7", "7"), (" sept ", "9")],
)
def test_a_month_takes_its_name_or_its_number(said, month):
    assert record(SOLO, "fiscal_start", said)["fiscal_start"] == month


def test_a_month_that_is_neither_refuses():
    with pytest.raises(OnboardingError, match="month"):
        record(SOLO, "fiscal_start", "13")
    with pytest.raises(OnboardingError, match="month"):
        record(SOLO, "fiscal_start", "someday")


# ---- roles ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("said", "role"),
    [("volunteer treasurer", "treasurer"), ("Treasurer", "treasurer"), ("on the board", "board")],
)
def test_a_role_answer_finds_the_one_role_it_names(said, role):
    answers = {**CHURCH, "legal_name": "Grace", "your_name": "Carol"}
    assert record(answers, "your_role", said)["your_role"] == role


def test_a_role_answer_naming_two_roles_or_none_refuses():
    answers = {**CHURCH, "legal_name": "Grace", "your_name": "Carol"}
    with pytest.raises(OnboardingError):
        record(answers, "your_role", "pastor and treasurer")
    with pytest.raises(OnboardingError):
        record(answers, "your_role", "wizard")


def test_a_person_line_finds_the_role_inside_the_words_around_it():
    people = record(CHURCH, "people", "Ruth Hollis = missionary in Nepal\nDon Pruitt = board")
    assert people["people"] == [
        {"name": "Ruth Hollis", "role": "missionary"},
        {"name": "Don Pruitt", "role": "board"},
    ]


def test_a_host_without_the_iana_tables_still_takes_names_it_can_check(monkeypatch, tmp_path):
    from core.onboarding import zones

    monkeypatch.setattr(zones.zoneinfo, "TZPATH", (str(tmp_path),))
    zones._data.cache_clear()
    try:
        assert resolve_zone("Africa/Maputo") == "Africa/Maputo"
        assert resolve_zone("Eastern") == "America/New_York"
        with pytest.raises(ZoneAnswerError, match="city"):
            resolve_zone("nepal")
    finally:
        zones._data.cache_clear()
