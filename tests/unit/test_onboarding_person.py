"""A person's own setup (core/onboarding/person.py, chunk 10): one question
per screen, each yes the person's own, and a step done only once Tim has
seen it work."""

from __future__ import annotations

import json
import stat

import pytest

from core.onboarding import OnboardingError
from core.onboarding.person import (
    FILE,
    Facts,
    SetupBook,
    at_sign_up,
    load_steps,
    next_step,
    offered,
    words,
)

NOW = "2026-10-22T14:00:00+00:00"
LATER = "2026-10-22T14:05:00+00:00"


def test_the_steps_read_in_order_with_plain_words():
    steps = load_steps()
    assert [s.id for s in steps] == [
        "role",
        "plan",
        "daily_email",
        "ai_connector",
        "home_screen",
        "mileage",
        "calendar",
    ]
    for s in steps:
        for text in (s.name, s.ask, s.help, s.yes, s.later, s.waiting, s.done, *s.points):
            assert text and "—" not in text  # no em dashes in a person's words
            for machinery in ("token", "manifest", "permission", "scope", "role"):
                assert machinery not in text.casefold()


def test_baseline_is_offered_where_it_fits_and_mileage_only_where_offered():
    everyone = Facts(email="ruth@example.org", files_expenses=True)
    assert offered(everyone, []) == ("daily_email", "home_screen")
    assert offered(everyone, ["mileage"]) == ("daily_email", "home_screen", "mileage")
    no_address = Facts(email="", files_expenses=True)
    assert offered(no_address, ["mileage"]) == ("home_screen", "mileage")
    no_receipts = Facts(email="abby@example.org", files_expenses=False)
    assert offered(no_receipts, []) == ("daily_email",)
    assert offered(no_receipts, ["mileage"]) == ("daily_email",)  # no expenses, no drives


def test_next_asks_then_checks_then_moves_on_once_seen(tmp_path):
    book = SetupBook(tmp_path)
    steps = ("daily_email", "home_screen")
    assert next_step(book.answers("ruth-hollis"), steps) == ("daily_email", "ask")
    book.record("ruth-hollis", "daily_email", "yes", now=NOW)
    assert next_step(book.answers("ruth-hollis"), steps) == ("daily_email", "check")
    assert book.saw("ruth-hollis", "daily_email", now=LATER) is True
    assert book.saw("ruth-hollis", "daily_email", now=LATER) is False  # once
    assert next_step(book.answers("ruth-hollis"), steps) == ("home_screen", "ask")
    book.record("ruth-hollis", "home_screen", "yes", now=NOW)
    book.saw("ruth-hollis", "home_screen", now=LATER)
    assert next_step(book.answers("ruth-hollis"), steps) is None


def test_not_now_moves_on_and_again_brings_it_back(tmp_path):
    book = SetupBook(tmp_path)
    steps = ("daily_email", "mileage")
    book.record("ruth-hollis", "daily_email", "later", now=NOW)
    assert next_step(book.answers("ruth-hollis"), steps) == ("mileage", "ask")
    book.record("ruth-hollis", "daily_email", "again", now=LATER)
    assert next_step(book.answers("ruth-hollis"), steps) == ("daily_email", "ask")


def test_mileage_and_the_calendar_wait_outside_sign_up_until_wanted(tmp_path):
    """The sign-up goal (2026-10-27): offered, never asked on day one."""
    assert {s.id for s in load_steps() if not s.at_sign_up} == {"mileage", "calendar"}
    book = SetupBook(tmp_path)
    steps = ("daily_email", "home_screen", "mileage", "calendar", "ai_connector")
    walk = at_sign_up(book.answers("ruth-hollis"), steps)
    assert walk == ("daily_email", "home_screen", "ai_connector")
    book.record("ruth-hollis", "mileage", "again", now=NOW)  # "Set this up now"
    walk = at_sign_up(book.answers("ruth-hollis"), steps)
    assert walk == ("daily_email", "home_screen", "mileage", "ai_connector")


def test_the_ai_step_asks_no_face_id_its_own_button_is_the_yes(tmp_path):
    """The sign-up goal (2026-10-28, the owner's iPhone walk): the AI screen's
    button is Claude's own add box, and the sign-in that follows asks for
    Face ID itself, so a Face ID yes before it was one tap too many. Nothing
    on the box changes until their AI signs in, and then Tim sees it work."""
    book = SetupBook(tmp_path)
    assert next_step(book.answers("ruth-hollis"), ("ai_connector",)) == ("ai_connector", "check")
    assert book.saw("ruth-hollis", "ai_connector", now=LATER) is True
    assert next_step(book.answers("ruth-hollis"), ("ai_connector",)) is None
    # every other step still needs its own yes before Tim can see it work
    assert next_step(book.answers("ruth-hollis"), ("home_screen",)) == ("home_screen", "ask")
    assert book.saw("ruth-hollis", "home_screen", now=LATER) is False


def test_moving_on_from_a_check_keeps_the_yes(tmp_path):
    book = SetupBook(tmp_path)
    book.record("ruth-hollis", "mileage", "yes", now=NOW)
    book.record("ruth-hollis", "mileage", "later", now=NOW)
    assert next_step(book.answers("ruth-hollis"), ("mileage",)) is None
    book.record("ruth-hollis", "mileage", "again", now=LATER)
    assert next_step(book.answers("ruth-hollis"), ("mileage",)) == ("mileage", "check")


def test_nothing_is_seen_working_without_the_persons_yes(tmp_path):
    book = SetupBook(tmp_path)
    assert book.saw("ruth-hollis", "mileage", now=NOW) is False
    assert next_step(book.answers("ruth-hollis"), ("mileage",)) == ("mileage", "ask")


def test_each_person_has_their_own_answers(tmp_path):
    book = SetupBook(tmp_path)
    book.record("ruth-hollis", "daily_email", "yes", now=NOW)
    assert book.answers("tom-and-jen-baker") == {}
    assert next_step(book.answers("tom-and-jen-baker"), ("daily_email",)) == (
        "daily_email",
        "ask",
    )


def test_answers_are_settings_in_the_tenant_folder_only_the_box_reads(tmp_path):
    book = SetupBook(tmp_path)
    book.record("ruth-hollis", "daily_email", "yes", now=NOW)
    path = tmp_path / FILE
    held = json.loads(path.read_text())
    assert held == {"ruth-hollis": {"daily_email": {"yes": NOW}}}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert SetupBook(tmp_path).answers("ruth-hollis") == held["ruth-hollis"]


@pytest.mark.parametrize(
    ("step", "said"), [("bank_login", "yes"), ("daily_email", "maybe"), ("daily_email", "")]
)
def test_only_a_real_step_and_a_real_answer_are_recorded(tmp_path, step, said):
    with pytest.raises(OnboardingError):
        SetupBook(tmp_path).record("ruth-hollis", step, said, now=NOW)
    assert not (tmp_path / FILE).exists()


def test_a_damaged_file_reads_as_nothing_answered(tmp_path):
    (tmp_path / FILE).write_text("{not json")
    assert SetupBook(tmp_path).answers("ruth-hollis") == {}


def test_words_fill_in_the_address_and_nothing_else():
    said = words("daily_email", email="ruth@example.org")
    assert "ruth@example.org" in said["ask"]
    assert not any("{" in str(v) for v in said.values())
    assert set(said) == {
        "name",
        "ask",
        "help",
        "yes",
        "later",
        "waiting",
        "done",
        "points_heading",
        "points",
    }


def test_the_reviewer_side_comes_first_and_only_to_whoever_adopts_the_plan():
    """Screens chunk 3 (R2, R3): what a reviewer is asked, then the plan."""
    reviewer = Facts(email="linda@example.org", adopts_plan=True)
    assert offered(reviewer, []) == ("role", "plan", "daily_email")
    assert "role" not in offered(Facts(email="daniel@example.org", files_expenses=True), [])
    assert not next(s for s in load_steps() if s.id == "plan").face_id  # its yes page asks


def test_the_reviewer_words_name_the_person_they_look_over():
    said = words("role", who="Daniel")
    assert said["ask"] == "Here's what Daniel is asking."
    assert said["points_heading"] == "What you'll see, and what you won't"
    assert len(said["points"]) == 3
    assert all("{who}" not in line for line in said["points"])
    assert "Daniel's own bank account" in said["points"][1]
    assert words("plan", who="Daniel")["points"] == []
