"""What the usability walk of 2026-10-13 found in the engine's own words:
setup questions a missionary can answer without looking anything up, a
reviewer promise that matches what Tim does, plain sentences on the
capability page, and a receipt page that shows the photo going."""

from __future__ import annotations

from core.capabilities import build
from core.onboarding import load_questions, record
from core.tools.receipts import page


def _q(qid: str):
    return next(q for q in load_questions() if q.id == qid)


def test_the_questions_say_what_to_type_and_where_to_find_it():
    assert _q("who").ask == "What kind of work will Tim keep the books for?"
    assert (
        "bank account" in _q("legal_name").help and "sending organization" in _q("legal_name").help
    )
    assert "Form 990" not in _q("entity_nonprofit").ask
    assert "leave it blank" in _q("banned_words").ask.lower() and "like" in _q("banned_words").ask


def test_an_answer_written_with_the_old_label_still_counts():
    """A ministry file written before the rewording (Caleb's) still installs."""
    answers = record({}, "who", "nonprofit-solo")
    old = record(answers, "entity_nonprofit", "A charity that files a Form 990")
    assert old["entity_nonprofit"] == "public-charity"


def test_not_sure_is_an_answer():
    answers = record({}, "who", "nonprofit-solo")
    assert record(answers, "entity_nonprofit", "Not sure")["entity_nonprofit"] == "public-charity"


def test_the_reviewer_is_promised_what_tim_does():
    help_ = _q("reviewer").help
    assert "short monthly review" not in help_
    assert "an email when each month is ready" in help_


def test_the_capability_page_reads_in_plain_sentences():
    says = {t["name"]: t["says"] for t in build()["ask"] + build()["tell"]}
    assert says["month_report"].endswith("shows what you paid and its dollar value on that day.")
    assert "dollar line" not in says["decide_card"] and "Face ID" in says["decide_card"]


def test_the_receipt_page_shows_the_photo_going():
    shown = page("n")
    assert "Sending your photo" in shown and "upload.onprogress" in shown
    assert "Saved." in shown and "Your link still works" in shown


def test_name_questions_ask_for_the_email_with_it():
    """The second walk (2026-10-13): asked only for a name, Jon types a name
    and nobody gets a sign-in link."""
    assert "email" in _q("your_name").ask and "@" in _q("your_name").ask
    assert "email" in _q("reviewer").ask and "won't hear anything" in _q("reviewer").help
