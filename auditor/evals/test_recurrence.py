"""Recurrence lens evals (lens 19, owner's go 2026-09-10): the auditor
re-reads its own store for repeats and names the automation that would
retire each. Counting is code, naming is the triage, deciding is the owner.

Contract under test:
- a finding still open after ``long_nights`` nights is a ``long-lived``
  INFO line carrying its age and its candidate; younger ones are quiet;
- one condition on ``class_subjects`` or more subjects inside the window is
  a ``class`` line (open and resolved both count; outside the window not);
- candidates resolve ``lens/condition`` first, then ``lens``, else the
  "name one" handoff to the triage;
- the lens never reads its own prior findings, is quiet on an empty store,
  and is off when ``[auditor.recurrence].enabled`` is false.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from auditor.findings import Finding, candidate_id
from auditor.lenses import recurrence
from auditor.store import AuditorStore

from .fixtures import NOW, make_context, make_ledger

_NOW = datetime.fromisoformat(NOW)


def _night(days_ago: int) -> str:
    return (_NOW - timedelta(days=days_ago)).isoformat()


def _seed(store_root, nights: dict[int, list[Finding]]) -> None:
    """Replay prior nights through the real store: ``nights`` maps days-ago
    to that night's findings (a finding absent one night resolves)."""
    with AuditorStore.open(store_root) as store:
        for days_ago in sorted(nights, reverse=True):
            store.reconcile("t", nights[days_ago], now=_night(days_ago))


def _f(lens, subject, condition="x", severity="WARN"):
    return Finding(lens=lens, subject=subject, condition=condition, severity=severity, detail="d")


def _run(tmp_path, **overrides):
    marker = tmp_path / ".ledger-made"
    if not marker.exists():  # the fixture ledger is built once per test dir
        make_ledger(tmp_path)
        marker.write_text("")
    ctx = make_context(tmp_path, store_root=tmp_path / "store", **overrides)
    with ctx.ledger:
        return recurrence.check(ctx)


def test_quiet_on_an_empty_store(tmp_path):
    assert _run(tmp_path) == []


def test_a_finding_open_past_the_threshold_is_long_lived_with_its_candidate(tmp_path):
    disk = _f("host", "internal disk", "low-free")
    _seed(tmp_path / "store", {d: [disk] for d in (10, 9, 8, 7, 6, 5, 4, 3, 2, 1)})
    findings = _run(
        tmp_path,
        recurrence_candidates={"host/low-free": "auto-thin local snapshots at the floor"},
    )
    (long,) = [f for f in findings if f.condition == "long-lived"]
    assert long.lens == "recurrence" and long.severity == "INFO"
    assert long.subject == "host: internal disk"
    assert "open 10 nights" in long.detail
    assert "candidate: auto-thin local snapshots at the floor" in long.detail


def test_a_young_open_finding_is_quiet(tmp_path):
    _seed(tmp_path / "store", {d: [_f("host", "internal disk", "low-free")] for d in (3, 2, 1)})
    assert [f for f in _run(tmp_path) if f.condition == "long-lived"] == []


def test_a_resolved_finding_is_not_long_lived(tmp_path):
    disk = _f("host", "internal disk", "low-free")
    nights = {d: [disk] for d in range(12, 2, -1)}
    nights[1] = []  # gone last night: resolved
    _seed(tmp_path / "store", nights)
    assert [f for f in _run(tmp_path) if f.condition == "long-lived"] == []


def test_one_condition_on_many_subjects_is_a_class(tmp_path):
    vendors = [_f("vendor-1099", v, "flag-missing") for v in ("Acme", "Bolt", "Cog", "Dyn")]
    nights = {3: vendors, 2: vendors, 1: vendors[:1]}  # three resolved last night, one open
    _seed(tmp_path / "store", nights)
    findings = _run(
        tmp_path, recurrence_candidates={"vendor-1099/flag-missing": "flip the flag via the API"}
    )
    (cls,) = [f for f in findings if f.condition == "class"]
    assert cls.subject == "vendor-1099 flag-missing"
    assert "4 subjects" in cls.detail and "(1 still open)" in cls.detail
    assert "Acme; Bolt; Cog; Dyn" in cls.detail
    assert "candidate: flip the flag via the API" in cls.detail


def test_a_class_needs_the_subject_threshold_and_the_window(tmp_path):
    two = [_f("reconcile", s, "unknown-clearing") for s in ("Purchase:1", "Purchase:2")]
    old = [
        _f("reconcile", s, "unknown-clearing") for s in ("Purchase:7", "Purchase:8", "Purchase:9")
    ]
    nights = {90: old, 89: old, 2: two, 1: two}  # the old trio fell out of the 60-day window
    _seed(tmp_path / "store", nights)
    assert [f for f in _run(tmp_path) if f.condition == "class"] == []
    # widen the window and the old trio counts
    findings = _run(tmp_path, recurrence_class_window_days=120)
    (cls,) = [f for f in findings if f.condition == "class"]
    assert "5 subjects" in cls.detail


def test_candidate_falls_back_to_the_lens_then_to_name_one(tmp_path):
    rows = [_f("context", s, "cloud-only") for s in ("a", "b", "c")]
    _seed(tmp_path / "store", {2: rows, 1: rows})
    (by_lens,) = [
        f
        for f in _run(tmp_path, recurrence_candidates={"context": "hydrate on write"})
        if f.condition == "class"
    ]
    assert "candidate: hydrate on write" in by_lens.detail
    (unnamed,) = [f for f in _run(tmp_path) if f.condition == "class"]
    assert "candidate: name one" in unnamed.detail


def test_a_finding_cleared_and_back_again_is_recurring(tmp_path):
    """The disk shape (2026-09-10): cleared by hand, back the next night,
    young every morning by first_seen alone. The store counts the returns
    and the lens reports it once it passes reopen_min."""
    disk = _f("host", "internal disk", "low-free")
    # present, gone, present, gone, present, gone, present: three returns
    nights = {7: [disk], 6: [], 5: [disk], 4: [], 3: [disk], 2: [], 1: [disk]}
    _seed(tmp_path / "store", nights)
    findings = _run(
        tmp_path, recurrence_candidates={"host/low-free": "auto-thin snapshots at the floor"}
    )
    (rec,) = [f for f in findings if f.condition == "recurring"]
    assert rec.subject == "host: internal disk" and rec.severity == "INFO"
    assert "cleared and back 3 times (open again" in rec.detail
    assert "candidate: auto-thin snapshots at the floor" in rec.detail
    # and it is NOT long-lived: first_seen is last night
    assert [f for f in findings if f.condition == "long-lived"] == []
    # below the threshold it stays quiet
    assert [f for f in _run(tmp_path, recurrence_reopen_min=4) if f.condition == "recurring"] == []


def test_the_lens_never_reads_its_own_findings(tmp_path):
    own = [_f("recurrence", f"host: disk {i}", "long-lived", "INFO") for i in range(4)]
    _seed(tmp_path / "store", {d: own for d in range(12, 0, -1)})
    assert _run(tmp_path) == []


def test_disabled_by_config(tmp_path):
    disk = _f("host", "internal disk", "low-free")
    _seed(tmp_path / "store", {d: [disk] for d in range(12, 0, -1)})
    assert _run(tmp_path, recurrence_enabled=False) == []


# ------------------------------------------ the candidate as data (row 7.18)


def test_a_named_candidate_rides_the_finding_as_data(tmp_path):
    """The prose line is for the report; the structured candidate is for the
    triage lane, which turns a named one into a proposal PR."""
    disk = _f("host", "internal disk", "low-free")
    _seed(tmp_path / "store", {d: [disk] for d in (10, 9, 8, 7, 6, 5, 4, 3, 2, 1)})
    (long,) = [
        f
        for f in _run(
            tmp_path, recurrence_candidates={"host/low-free": "auto-thin local snapshots"}
        )
        if f.condition == "long-lived"
    ]
    candidate = long.candidate
    assert candidate is not None
    assert candidate.text == "auto-thin local snapshots"
    assert candidate.named is True
    assert candidate.source_lens == "host" and candidate.source_condition == "low-free"
    assert candidate.subjects == ("internal disk",)
    assert candidate.count == 10
    assert candidate.id == candidate_id("host", "low-free")
    # the prose keeps the candidate sentence and gains the id the lane keys on
    assert "candidate: auto-thin local snapshots" in long.detail
    assert f"[{candidate.id}]" in long.detail


def test_the_candidate_id_is_stable_and_names_the_automation_not_the_finding(tmp_path):
    """The id is a function of the source lens and condition, so two nights
    over the same repeat agree and the lane can say "already proposed"."""
    disk = _f("host", "internal disk", "low-free")
    _seed(tmp_path / "store", {d: [disk] for d in (10, 9, 8, 7, 6, 5, 4, 3, 2, 1)})
    table = {"host/low-free": "auto-thin local snapshots"}
    tonight = {f.candidate.id for f in _run(tmp_path, recurrence_candidates=table)}
    tomorrow = {f.candidate.id for f in _run(tmp_path, recurrence_candidates=table)}
    assert tonight == tomorrow == {candidate_id("host", "low-free")}
    assert candidate_id("host", "low-free") != candidate_id("host", "no-heartbeat")


def test_a_class_candidate_carries_its_subjects_and_count(tmp_path):
    rows = [_f("context", s, "cloud-only") for s in ("a", "b", "c")]
    _seed(tmp_path / "store", {2: rows, 1: rows})
    (klass,) = [
        f
        for f in _run(tmp_path, recurrence_candidates={"context": "hydrate on write"})
        if f.condition == "class"
    ]
    assert klass.candidate.subjects == ("a", "b", "c")
    assert klass.candidate.count == 3
    assert klass.candidate.named is True


def test_an_unnamed_candidate_is_data_too_and_says_it_is_unnamed(tmp_path):
    """No proposal comes of it: the triage names it first, in the note."""
    rows = [_f("context", s, "cloud-only") for s in ("a", "b", "c")]
    _seed(tmp_path / "store", {2: rows, 1: rows})
    (klass,) = [f for f in _run(tmp_path) if f.condition == "class"]
    assert klass.candidate.named is False
    assert klass.candidate.text == recurrence.UNNAMED
    assert klass.candidate.id == candidate_id("context", "cloud-only")
