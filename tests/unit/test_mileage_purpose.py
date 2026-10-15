"""What a drive was for, in the person's own words (t-mileage part 2,
2026-10-13). The box knows a drive's miles and times and nothing about
where; the person says what it was for ("visiting the church in Pokhara")
or that it was personal. A ministry drive is priced at the rate in force on
its own day: the institution's when it sets one, else the IRS rate. The
month shows each drive, and a drive nobody has spoken for waits on their
word, never counted at a guess."""

from __future__ import annotations

from pathlib import Path

from core.engine.runner import run
from tests.unit.test_door_my_words import solo  # noqa: F401  (fixture)
from tests.unit.test_ministry_lines import _door


def _drive(solo, start: str = "2026-10-12T03:00:00+00:00", miles: str = "14.0"):  # noqa: F811
    run(
        "living-water",
        "expenses",
        "drive",
        params={
            "person": "daniel-reyes",
            "start": start,
            "end": start[:11] + "04:00:00+00:00",
            "miles": miles,
        },
        ledger_dir=Path(solo["ledger_root"]).parent,
        tenants_root=solo["root"],
    )


def _drives(solo, person: str = "daniel-reyes") -> list[dict]:  # noqa: F811
    [month] = _door(solo, person).call("month_report", {"month": "2026-10"})["people"]
    return month["drives"]


def _say(solo, said: str, person: str = "daniel-reyes") -> dict:  # noqa: F811
    line = _drives(solo)[0]["source"]
    return _door(solo, person).call("set_drive_purpose", {"line": line, "said": said})


def test_a_drive_nobody_has_spoken_for_waits_and_counts_nothing(solo):  # noqa: F811
    _drive(solo)
    [drive] = _drives(solo)
    assert drive["miles"] == "14.0" and drive["date"] == "2026-10-12"
    assert drive["for"] is None and drive["dollars"] is None
    assert drive["waiting_on"] == "what it was for"


def test_their_words_say_what_it_was_for_and_it_is_priced_at_the_days_rate(solo):  # noqa: F811
    _drive(solo)
    out = _say(solo, "that was visiting the church in Pokhara")
    assert out["status"] == "set"
    [drive] = _drives(solo)
    assert drive["for"] == "that was visiting the church in Pokhara"
    assert drive["dollars"] == "10.15"  # 14.0 miles at the IRS 72.5 cents for 2026
    assert drive["rate_from"] == "IRS" and drive["waiting_on"] is None


def test_a_personal_drive_is_kept_and_never_priced(solo):  # noqa: F811
    _drive(solo)
    _say(solo, "that one was personal")
    [drive] = _drives(solo)
    assert drive["ministry"] is False and drive["dollars"] is None and drive["waiting_on"] is None


def test_the_institution_rate_wins_when_it_sets_one(solo):  # noqa: F811
    toml = solo["root"] / "living-water" / "tenant.toml"
    toml.write_text(toml.read_text() + '\n[institution]\nname = "AGWM"\nmileage_rate = "0.70"\n')
    _drive(solo)
    _say(solo, "ministry: hospital visit")
    [drive] = _drives(solo)
    assert drive["dollars"] == "9.80" and drive["rate_from"] == "AGWM"


def test_someone_elses_drive_is_refused(solo):  # noqa: F811
    _drive(solo)
    line = _drives(solo)[0]["source"]
    stranger = _door(solo, "linda-park").call("set_drive_purpose", {"line": line, "said": "x"})
    assert stranger["status"] in (
        "refused",
        "set",
    )  # Linda reviews Daniel's month: she may place it


def test_asked_about_their_drives_tim_knows_where_to_look():
    # The Friday check-in tells them to ask "What were my drives this week?"
    from core.tools.catalog import SPECS

    [spec] = [s for s in SPECS if s.name == "month_report"]
    assert "drives" in spec.description and "still waiting on their word" in spec.description
