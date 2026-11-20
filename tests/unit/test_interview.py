"""The missionary answers the setup questions herself (engine docs/screens/
README.md, screens 4 to 9; board c49, the owner's go of 2026-11-17).

A maintainer makes a one-time link on the box. The missionary answers six
screens on her phone: how support reaches her, her IRS letter (the photo
kept, the name typed), where she lives, who looks over her spending (and
whether they approve each month), whether she drives, and what Tim heard.
Her yes writes the same ministry file a maintainer drafts by hand today.
Support that comes only straight to her (no nonprofit of her own) is a
setup shape Tim can't do yet: it says so and leaves it for the maintainer.
"""

from __future__ import annotations

import tomllib

import pytest

from core.onboarding import interview as iv
from core.onboarding import missing, plan, record


@pytest.fixture
def book(tmp_path):
    return iv.InterviewBook(tmp_path / "interviews")


def _new(book, **over):
    args = {"name": "Hannah Cole", "email": "hannah@example.org", "by": "Paul Weston"} | over
    token = book.mint(**args, days=14, now=1_000)
    return token


def _walk(book, token, *, approves: bool = False, now: int = 2_000):
    steps = [
        ("support", {"support": ["own_nonprofit", "platform"]}),
        ("letter", {"legal_name": "Hope Springs Ministries Inc."}),
        ("where", {"town": "Nakuru, Kenya"}),
        (
            "reviewer",
            {
                "reviewer_name": "Sarah Whitfield",
                "reviewer_email": "sarah@example.org",
                "approves": "approve" if approves else "look",
            },
        ),
        ("driving", {"drives": "yes"}),
    ]
    for screen, form in steps:
        assert book.next_screen(token, now=now) == screen
        assert book.take(token, screen, form, now=now) == ""
    assert book.next_screen(token, now=now) == "heard"


HAND_DRAFTED = """name = "hope-springs-ministries"
slug = "hope-springs-ministries"
ministry = "Hope Springs Ministries"
presence = ["expense.report", "payment", "authority"]

[[setup]]
id = "who"
said = "Just me, a missionary or ministry worker with my own nonprofit"
[[setup]]
id = "legal_name"
said = "Hope Springs Ministries Inc."
[[setup]]
id = "your_name"
said = "Hannah Cole, hannah@example.org"
[[setup]]
id = "timezone"
said = "Africa/Nairobi"
[[setup]]
id = "fiscal_start"
said = "1"
[[setup]]
id = "entity_nonprofit"
said = "Another charity (it files a Form 990 each year)"
[[setup]]
id = "reviewer"
said = "Sarah Whitfield, sarah@example.org"
[[setup]]
id = "reviewer_approves"
said = "Just look it over"
[[setup]]
id = "cash_on_hand"
said = "1500"
[[setup]]
id = "spelling"
said = "American"
"""


def test_her_answers_write_the_file_a_maintainer_drafts_by_hand(book):
    token = _new(book)
    _walk(book, token)
    assert book.take(token, "heard", {"right": "yes"}, now=2_000) == ""
    assert book.status(token) == "done"
    assert book.ministry_file(token) == HAND_DRAFTED


def test_the_file_leaves_no_required_question_unanswered(book):
    token = _new(book)
    _walk(book, token, approves=True)
    book.take(token, "heard", {"right": "yes"}, now=2_000)
    data = tomllib.loads(book.ministry_file(token))
    answers: dict = {}
    for said in data["setup"]:
        answers = record(answers, said["id"], said["said"])
    assert missing(answers) == []
    built = plan(answers, data["slug"])
    assert built.reviewer_approves
    assert built.timezone == "Africa/Nairobi"


def test_a_link_is_one_person_and_runs_out(book):
    token = _new(book)
    assert book.next_screen(token, now=2_000) == "support"
    assert book.next_screen("made-up", now=2_000) is None
    assert book.next_screen(token, now=1_000 + 15 * 86_400) is None


def test_only_its_hash_is_kept(book, tmp_path):
    token = _new(book)
    for f in (tmp_path / "interviews").rglob("*"):
        if f.is_file():
            assert token not in f.read_text(encoding="utf-8", errors="replace")


def test_support_needs_at_least_one_pick(book):
    token = _new(book)
    assert book.take(token, "support", {"support": []}, now=2_000)
    assert book.next_screen(token, now=2_000) == "support"


def test_no_nonprofit_of_her_own_is_said_and_left_for_the_maintainer(book):
    token = _new(book)
    assert book.take(token, "support", {"support": ["straight", "agency"]}, now=2_000) == ""
    assert book.status(token) == "not_yet"
    assert book.next_screen(token, now=2_000) == "not_yet"
    assert book.ministry_file(token) == ""
    [row] = book.rows()
    assert row["name"] == "Hannah Cole" and row["status"] == "not_yet"
    assert row["support"] == ["straight", "agency"]


def test_where_she_lives_sets_her_days_and_refuses_a_place_it_cannot_find(book):
    token = _new(book)
    book.take(token, "support", {"support": ["own_nonprofit"]}, now=2_000)
    book.take(token, "letter", {"legal_name": "Hope Springs Ministries Inc."}, now=2_000)
    assert book.take(token, "where", {"town": "Atlantis"}, now=2_000)
    assert book.take(token, "where", {"town": "Nakuru, Kenya"}, now=2_000) == ""
    assert book.answers(token)["zone"] == "Africa/Nairobi"
    assert "Nairobi time" in " ".join(iv.so_ill(book.answers(token)))


def test_the_letter_needs_the_name_and_keeps_the_photo(book):
    token = _new(book)
    book.take(token, "support", {"support": ["own_nonprofit"]}, now=2_000)
    assert book.take(token, "letter", {"legal_name": " "}, now=2_000)
    photo = b"\xff\xd8\xff\xe0" + b"\x00" * 64
    assert (
        book.take(
            token, "letter", {"legal_name": "Hope Springs Ministries Inc."}, photo=photo, now=2_000
        )
        == ""
    )
    assert book.letter(token) == photo


def test_no_reviewer_yet_is_fine_and_leaves_no_approver(book):
    token = _new(book)
    book.take(token, "support", {"support": ["own_nonprofit"]}, now=2_000)
    book.take(token, "letter", {"legal_name": "Hope Springs Ministries Inc."}, now=2_000)
    book.take(token, "where", {"town": "Kenya"}, now=2_000)
    assert book.take(token, "reviewer", {"nobody": "yes"}, now=2_000) == ""
    book.take(token, "driving", {"drives": "no"}, now=2_000)
    book.take(token, "heard", {"right": "yes"}, now=2_000)
    ids = [s["id"] for s in tomllib.loads(book.ministry_file(token))["setup"]]
    assert "reviewer" not in ids and "reviewer_approves" not in ids


def test_a_reviewer_needs_an_email(book):
    token = _new(book)
    for screen, form in (
        ("support", {"support": ["own_nonprofit"]}),
        ("letter", {"legal_name": "Hope Springs Ministries Inc."}),
        ("where", {"town": "Kenya"}),
    ):
        book.take(token, screen, form, now=2_000)
    assert book.take(
        token, "reviewer", {"reviewer_name": "Sarah", "reviewer_email": "x"}, now=2_000
    )


def test_what_tim_heard_comes_from_her_answers_and_the_defaults(book):
    token = _new(book)
    _walk(book, token, approves=True)
    heard = iv.heard(book.answers(token), name="Hannah Cole")
    said = dict(heard["mine"])
    assert said["Your ministry"] == "Hope Springs Ministries Inc."
    assert said["You"] == "Hannah Cole, in Nakuru, Kenya"
    assert said["Looks over your spending"] == "Sarah Whitfield, who also approves each month"
    assert said["Driving"].startswith("Yes")
    assumed = " ".join(heard["assumed"])
    assert "January to December" in assumed
    assert "$250" in assumed  # the solo shape's own line, read from setup's defaults
    assert "$1,500" in assumed


def test_change_something_reopens_one_screen_and_keeps_the_rest(book):
    token = _new(book)
    _walk(book, token)
    assert book.take(token, "heard", {"change": "where"}, now=2_000) == ""
    assert book.next_screen(token, now=2_000) == "where"
    book.take(token, "where", {"town": "Kampala, Uganda"}, now=2_000)
    assert book.next_screen(token, now=2_000) == "heard"
    assert book.answers(token)["legal_name"] == "Hope Springs Ministries Inc."


def test_a_finished_interview_takes_no_more_answers(book):
    token = _new(book)
    _walk(book, token)
    book.take(token, "heard", {"right": "yes"}, now=2_000)
    assert book.take(token, "where", {"town": "Uganda"}, now=2_000)
    assert book.next_screen(token, now=2_000) == "done"


def test_the_words_carry_no_machinery_and_no_em_dash():
    for text in iv.all_words():
        assert "—" not in text
        for word in ("tenant", "slug", "toml", "ledger", "token", "iana"):
            assert word not in text.casefold().split()
