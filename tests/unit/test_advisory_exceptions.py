"""Accepted advisory exceptions (public issue #12): the CI advisory scan
fails on any published advisory against the lock, except the ones written
down in ONE file, each with a reason and a review date. A lapsed review
date fails the build, so an exception is a decision with an expiry, never
a permanent mute."""

from __future__ import annotations

from datetime import date

import pytest

from core.evals.advisory_exceptions import ExceptionsError, ignore_args, load

TODAY = date(2026, 10, 6)


def _write(tmp_path, text: str):
    p = tmp_path / "advisory-exceptions.toml"
    p.write_text(text)
    return p


def test_no_file_and_an_empty_file_ignore_nothing(tmp_path):
    assert ignore_args(load(tmp_path / "absent.toml"), today=TODAY) == []
    assert ignore_args(load(_write(tmp_path, "")), today=TODAY) == []


def test_a_live_exception_becomes_an_ignore_flag(tmp_path):
    p = _write(
        tmp_path,
        '[[exception]]\nid = "GHSA-aaaa-bbbb-cccc"\npackage = "pyjwt"\n'
        'reason = "no fixed release; the vulnerable path is unused"\nreview_by = 2026-11-01\n',
    )
    assert ignore_args(load(p), today=TODAY) == ["--ignore-vuln", "GHSA-aaaa-bbbb-cccc"]


def test_a_lapsed_review_date_fails(tmp_path):
    p = _write(
        tmp_path,
        '[[exception]]\nid = "GHSA-aaaa-bbbb-cccc"\npackage = "pyjwt"\n'
        'reason = "waiting on upstream"\nreview_by = 2026-10-01\n',
    )
    with pytest.raises(ExceptionsError, match="review date 2026-10-01 has passed"):
        ignore_args(load(p), today=TODAY)


@pytest.mark.parametrize("missing", ["id", "package", "reason", "review_by"])
def test_every_field_is_required(tmp_path, missing):
    fields = {
        "id": '"GHSA-aaaa-bbbb-cccc"',
        "package": '"pyjwt"',
        "reason": '"why"',
        "review_by": "2026-11-01",
    }
    del fields[missing]
    body = "".join(f"{k} = {v}\n" for k, v in fields.items())
    with pytest.raises(ExceptionsError, match=missing):
        load(_write(tmp_path, "[[exception]]\n" + body))


def test_the_shipped_file_parses_and_has_no_lapsed_entry():
    from core.evals.advisory_exceptions import DEFAULT_PATH

    ignore_args(load(DEFAULT_PATH), today=date.today())
