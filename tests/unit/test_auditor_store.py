"""Findings-store lifecycle: the checklist the auditor remembers.

The output contract (docs/auditor-design.md): a finding seen again is the
SAME checklist item, silently carried forward; a finding whose condition
stopped being true resolves itself; one that returns re-announces; a
CRITICAL still open after the aging window gets exactly one bump back into
the NEW section, never a daily repeat.
"""

from __future__ import annotations

from auditor.findings import Finding
from auditor.store import AuditorStore

T0 = "2026-07-18T06:00:00+00:00"
T1 = "2026-07-19T06:00:00+00:00"
T2 = "2026-07-20T06:00:00+00:00"
T3 = "2026-07-21T06:00:00+00:00"
T4 = "2026-07-22T06:00:00+00:00"


def _f(subject="inv-1", condition="no-evidence", lens="status", severity="WARN", detail="d"):
    return Finding(
        lens=lens, subject=subject, condition=condition, severity=severity, detail=detail
    )


def test_new_finding_lands_in_new_and_open(tmp_path):
    with AuditorStore.open(tmp_path) as store:
        r = store.reconcile("t", [_f()], now=T0)
    assert [x["subject"] for x in r.new] == ["inv-1"]
    assert [x["reason"] for x in r.new] == ["new"]
    assert len(r.open) == 1
    assert r.resolved == []
    assert r.open[0]["first_seen"] == T0


def test_carry_forward_is_silent(tmp_path):
    with AuditorStore.open(tmp_path) as store:
        store.reconcile("t", [_f()], now=T0)
        r = store.reconcile("t", [_f()], now=T1)
    assert r.new == []
    assert len(r.open) == 1
    assert r.open[0]["first_seen"] == T0  # same item, not a fresh sighting
    assert r.open[0]["last_seen"] == T1


def test_resolution_is_automatic(tmp_path):
    with AuditorStore.open(tmp_path) as store:
        store.reconcile("t", [_f()], now=T0)
        r = store.reconcile("t", [], now=T1)
    assert r.new == []
    assert r.open == []
    assert [x["subject"] for x in r.resolved] == ["inv-1"]


def test_reopen_reannounces_with_fresh_first_seen(tmp_path):
    with AuditorStore.open(tmp_path) as store:
        store.reconcile("t", [_f()], now=T0)
        store.reconcile("t", [], now=T1)  # resolves
        r = store.reconcile("t", [_f()], now=T2)  # comes back
    assert [x["reason"] for x in r.new] == ["returned"]
    assert r.open[0]["first_seen"] == T2


def test_severity_escalation_reannounces(tmp_path):
    with AuditorStore.open(tmp_path) as store:
        store.reconcile("t", [_f(severity="WARN")], now=T0)
        r = store.reconcile("t", [_f(severity="CRITICAL")], now=T1)
    assert [x["reason"] for x in r.new] == ["escalated"]
    assert r.open[0]["severity"] == "CRITICAL"
    assert r.open[0]["first_seen"] == T0  # escalation is news, not a new item


def test_severity_downgrade_is_silent(tmp_path):
    with AuditorStore.open(tmp_path) as store:
        store.reconcile("t", [_f(severity="CRITICAL")], now=T0)
        r = store.reconcile("t", [_f(severity="WARN")], now=T1)
    assert r.new == []
    assert r.open[0]["severity"] == "WARN"


def test_detail_refresh_does_not_reannounce(tmp_path):
    with AuditorStore.open(tmp_path) as store:
        store.reconcile("t", [_f(detail="aged 3 days")], now=T0)
        r = store.reconcile("t", [_f(detail="aged 4 days")], now=T1)
    assert r.new == []
    assert r.open[0]["detail"] == "aged 4 days"


def test_critical_aging_bump_fires_exactly_once(tmp_path):
    crit = _f(severity="CRITICAL")
    with AuditorStore.open(tmp_path) as store:
        store.reconcile("t", [crit], now=T0)
        assert store.reconcile("t", [crit], now=T1).new == []
        assert store.reconcile("t", [crit], now=T2).new == []
        r3 = store.reconcile("t", [crit], now=T3)  # 3 nights old
        assert [x["reason"] for x in r3.new] == ["aging"]
        r4 = store.reconcile("t", [crit], now=T4)  # never again
        assert r4.new == []


def test_warn_findings_never_age_bump(tmp_path):
    warn = _f(severity="WARN")
    with AuditorStore.open(tmp_path) as store:
        store.reconcile("t", [warn], now=T0)
        for t in (T1, T2, T3, T4):
            assert store.reconcile("t", [warn], now=t).new == []


def test_dedupe_within_a_run_keeps_highest_severity(tmp_path):
    with AuditorStore.open(tmp_path) as store:
        r = store.reconcile("t", [_f(severity="WARN"), _f(severity="CRITICAL")], now=T0)
    assert len(r.new) == 1
    assert r.new[0]["severity"] == "CRITICAL"


def test_open_checklist_is_oldest_first(tmp_path):
    with AuditorStore.open(tmp_path) as store:
        store.reconcile("t", [_f(subject="old")], now=T0)
        r = store.reconcile("t", [_f(subject="old"), _f(subject="fresh")], now=T1)
    assert [x["subject"] for x in r.open] == ["old", "fresh"]


def test_tenants_do_not_share_findings(tmp_path):
    with AuditorStore.open(tmp_path) as store:
        store.reconcile("a", [_f()], now=T0)
        r = store.reconcile("b", [], now=T0)
    assert r.open == [] and r.resolved == []


def test_run_bookkeeping_round_trip(tmp_path):
    with AuditorStore.open(tmp_path) as store:
        run_id = store.start_run("t", now=T0)
        assert store.last_run("t")["status"] == "running"
        store.finish_run(run_id, status="ok", report_path="/x/audit.md", now=T0)
        last = store.last_run("t")
    assert last["status"] == "ok"
    assert last["report_path"] == "/x/audit.md"
    assert last["finished_at"] == T0


def test_fingerprint_is_stable_and_ignores_detail_and_severity():
    a = _f(detail="one", severity="WARN")
    b = _f(detail="two", severity="CRITICAL")
    c = _f(subject="other")
    assert a.fingerprint == b.fingerprint
    assert a.fingerprint != c.fingerprint


# ---- honesty audit 2026-09-03, finding 04-F2: resolve only what was re-verified


def test_open_item_of_an_unverified_lens_stays_open_and_is_not_resolved(tmp_path):
    """A lens that crashed or was skipped never re-verified its items, so its
    open findings are neither resolved nor listed as resolved."""
    with AuditorStore.open(tmp_path) as store:
        store.reconcile("t", [_f(lens="book", subject="wb-row")], now=T0)
        r = store.reconcile("t", [], now=T1, verified_lenses={"status", "heartbeat"})
    assert r.resolved == []
    assert [(x["lens"], x["subject"], x["state"]) for x in r.open] == [("book", "wb-row", "open")]


def test_verified_lens_still_resolves_and_the_auditor_pseudo_lens_always_counts(tmp_path):
    with AuditorStore.open(tmp_path) as store:
        store.reconcile(
            "t",
            [
                _f(lens="status", subject="inv-1"),
                _f(lens="auditor", subject="lens book", condition="lens-crashed"),
            ],
            now=T0,
        )
        r = store.reconcile("t", [], now=T1, verified_lenses={"status"})
    assert sorted(x["subject"] for x in r.resolved) == ["inv-1", "lens book"]
    assert r.open == []


def test_reconcile_without_commit_can_be_rolled_back(tmp_path):
    """Finding 04-F3: the runner commits the checklist only after the report
    is on disk; until then every reconcile write must be discardable."""
    with AuditorStore.open(tmp_path) as store:
        r = store.reconcile("t", [_f()], now=T0, commit=False)
        assert [x["reason"] for x in r.new] == ["new"]
        store.rollback()
        assert store.open_findings("t") == []
        r2 = store.reconcile("t", [_f()], now=T1, commit=False)
        store.commit()
    assert [x["reason"] for x in r2.new] == ["new"]
    with AuditorStore.open(tmp_path) as store:
        assert [x["first_seen"] for x in store.open_findings("t")] == [T1]


def test_announce_reason_override_applies_to_new_and_returned_items(tmp_path):
    """Triage snooze (2026-09-04): an item coming back after its snooze
    expired is announced with the caller's reason, not the generic one."""
    f = _f()
    with AuditorStore.open(tmp_path) as store:
        r = store.reconcile("t", [f], now=T0, reasons={f.fingerprint: "snooze-expired"})
        assert [x["reason"] for x in r.new] == ["snooze-expired"]
        store.reconcile("t", [], now=T1)  # resolves
        r = store.reconcile("t", [f], now=T2, reasons={f.fingerprint: "snooze-expired"})
        assert [x["reason"] for x in r.new] == ["snooze-expired"]
        # carried forward: no announcement at all, override or not
        r = store.reconcile("t", [f], now=T3, reasons={f.fingerprint: "snooze-expired"})
        assert r.new == []
