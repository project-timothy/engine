"""Triage evals: the auditor proposes, the owner decides, silence accepts."""

from __future__ import annotations

from datetime import date

from auditor.findings import Finding
from auditor.triage import Triage, load_triage


def _f(lens="filing", condition="no-disposition", severity="WARN", subject="thing.pdf"):
    return Finding(lens=lens, subject=subject, condition=condition, severity=severity, detail="d")


def _write_triage(tmp_path, body):
    tdir = tmp_path / "t"
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "triage.toml").write_text(body)
    return tmp_path


def test_missing_file_means_every_proposal_stands(tmp_path):
    triage = load_triage("t", tenants_dir=tmp_path)
    finding = _f()
    assert triage.filter([finding]) == [finding]
    assert not triage.is_topic_muted("coding-drift")


def test_mute_by_class(tmp_path):
    root = _write_triage(tmp_path, '[findings]\nmuted = ["filing/no-disposition"]\n')
    triage = load_triage("t", tenants_dir=root)
    assert triage.filter([_f()]) == []
    assert triage.filter([_f(condition="stalled-at-top-level")]) != []


def test_mute_a_whole_lens(tmp_path):
    root = _write_triage(tmp_path, '[findings]\nmuted = ["filing"]\n')
    triage = load_triage("t", tenants_dir=root)
    assert triage.filter([_f(), _f(condition="stalled-at-top-level")]) == []
    assert triage.filter([_f(lens="status")]) != []


def test_mute_one_item_by_fingerprint(tmp_path):
    target = _f(subject="noisy.pdf")
    sibling = _f(subject="real.pdf")
    root = _write_triage(tmp_path, f'[findings]\nmuted = ["{target.fingerprint}"]\n')
    triage = load_triage("t", tenants_dir=root)
    kept = triage.filter([target, sibling])
    assert [f.subject for f in kept] == ["real.pdf"]


def test_severity_override_regrades(tmp_path):
    root = _write_triage(tmp_path, '[findings.severity]\n"filing/no-disposition" = "INFO"\n')
    triage = load_triage("t", tenants_dir=root)
    kept = triage.filter([_f(severity="WARN")])
    assert kept[0].severity == "INFO"
    assert kept[0].fingerprint == _f().fingerprint  # identity survives a regrade


def test_unknown_severity_override_is_ignored(tmp_path):
    root = _write_triage(tmp_path, '[findings.severity]\n"filing/no-disposition" = "WHATEVER"\n')
    triage = load_triage("t", tenants_dir=root)
    assert triage.filter([_f(severity="WARN")])[0].severity == "WARN"


def test_muted_topics_load(tmp_path):
    root = _write_triage(tmp_path, '[advisory]\nmuted_topics = ["coding-drift"]\n')
    triage = load_triage("t", tenants_dir=root)
    assert triage.is_topic_muted("coding-drift")
    assert not triage.is_topic_muted("aging")


def test_empty_triage_object_is_permissive():
    triage = Triage()
    finding = _f(severity="CRITICAL")
    assert triage.filter([finding]) == [finding]


def test_acknowledged_items_load(tmp_path):
    root = _write_triage(
        tmp_path,
        '[advisory.acknowledged]\n"coding-drift/Owner Person" = "real loans, confirmed 7/24"\n',
    )
    triage = load_triage("t", tenants_dir=root)
    assert triage.acknowledged == {"coding-drift/Owner Person": "real loans, confirmed 7/24"}


def test_empty_triage_has_no_acknowledgments():
    assert Triage().acknowledged == {}


# ---- dated entries: snooze and aging (2026-09-04) --------------------------


def test_snoozed_entry_mutes_until_the_date(tmp_path):
    root = _write_triage(
        tmp_path,
        '[findings]\nmuted = [{key = "filing/no-disposition", snooze_until = "2026-09-10"}]\n',
    )
    triage = load_triage("t", tenants_dir=root, as_of=date(2026, 9, 9))
    assert triage.filter([_f()]) == []


def test_expired_snooze_lets_the_finding_through_with_a_reason(tmp_path):
    root = _write_triage(
        tmp_path,
        '[findings]\nmuted = [{key = "filing/no-disposition", snooze_until = "2026-09-10"}]\n',
    )
    triage = load_triage("t", tenants_dir=root, as_of=date(2026, 9, 10))
    finding = _f()
    assert triage.filter([finding]) == [finding]
    assert triage.announce_reasons([finding]) == {finding.fingerprint: "snooze-expired"}
    # an unrelated finding carries no reason override
    assert triage.announce_reasons([_f(lens="status")]) == {}


def test_snooze_by_fingerprint_and_by_lens(tmp_path):
    target = _f(subject="noisy.pdf")
    root = _write_triage(
        tmp_path,
        "[findings]\nmuted = [\n"
        f'  {{key = "{target.fingerprint}", snooze_until = "2026-09-10"}},\n'
        '  {key = "status", snooze_until = "2026-09-10"},\n]\n',
    )
    triage = load_triage("t", tenants_dir=root, as_of=date(2026, 9, 1))
    kept = triage.filter([target, _f(subject="real.pdf"), _f(lens="status")])
    assert [f.subject for f in kept] == ["real.pdf"]


def test_undated_entries_keep_the_permanent_semantics(tmp_path):
    root = _write_triage(
        tmp_path,
        '[findings]\nmuted = ["filing/no-disposition", {key = "status"}]\n',
    )
    triage = load_triage("t", tenants_dir=root, as_of=date(2030, 1, 1))
    assert triage.filter([_f(), _f(lens="status")]) == []
    assert triage.aged_entries(max_age_days=90) == []


def test_dated_mute_and_acknowledgment_age_out(tmp_path):
    root = _write_triage(
        tmp_path,
        "[findings]\nmuted = [\n"
        '  {key = "filing/no-disposition", since = "2026-06-01", why = "known"},\n'
        '  {key = "status", since = "2026-08-20"},\n]\n'
        "[advisory.acknowledged]\n"
        '"coding-drift/Owner Person" = {answer = "real loans", since = "2026-05-01"}\n'
        '"coding-drift/Other Person" = "undated, permanent"\n',
    )
    triage = load_triage("t", tenants_dir=root, as_of=date(2026, 9, 4))
    # the mute still applies while aged out: the aging is reported, never a flood
    assert triage.filter([_f()]) == []
    assert triage.acknowledged == {
        "coding-drift/Owner Person": "real loans",
        "coding-drift/Other Person": "undated, permanent",
    }
    aged = triage.aged_entries(max_age_days=90)
    assert [(a.kind, a.key, a.since.isoformat()) for a in aged] == [
        ("acknowledgment", "coding-drift/Owner Person", "2026-05-01"),
        ("mute", "filing/no-disposition", "2026-06-01"),
    ]
    assert aged[1].age_days == 95


def test_malformed_dates_are_ignored_not_fatal(tmp_path):
    root = _write_triage(
        tmp_path,
        '[findings]\nmuted = [{key = "filing/no-disposition", snooze_until = "soon"}]\n',
    )
    triage = load_triage("t", tenants_dir=root, as_of=date(2026, 9, 4))
    # an unreadable snooze date is an undated (permanent) mute, the safe side
    assert triage.filter([_f()]) == []
    assert triage.aged_entries(max_age_days=90) == []


def test_as_of_defaults_to_today(tmp_path):
    triage = load_triage("t", tenants_dir=tmp_path)
    assert triage.as_of == date.today()
