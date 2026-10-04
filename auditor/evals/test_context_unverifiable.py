"""A context shim the lens could not read is a coverage gap, not drift (built 2026-10-02).

Proposal: ``docs/proposals/2026-10-01-context-shims-unverifiable-is-not-drift.md``
Candidate: 9ef65b3a (lens 19, ``context``/``cloud-only``: 4 subjects in 60 days,
every one a stamped shim evicted to a dataless placeholder on a cloud-sync
mount, reported as a WARN beside real drift, and every one resolved by the owner
pinning that single file by hand).

Two contracts, written before the code exists:

1. a shim the lens could not READ is in a third state. Not clean, not drifted:
   unverifiable. It is reported as a coverage gap about the mount the shims sit
   on — once, naming the count — instead of one WARN per evicted file, because
   the answerable question is the arrangement and not the symptom;
2. a shim the lens DID read and found wrong stays exactly as loud as it is
   today. The middle state must never be able to absorb a hand-edit, which is
   the failure mode this lens exists to scream about.

Fixtures are neutral placeholders on purpose: this tree is hermetic and names no
tenant, person, or host path. Reads are injected, so nothing here touches a
filesystem or a sync client.
"""

from __future__ import annotations

import errno

# Stands in for whatever synced trees a tenant configures.
SYNC_ROOT = "/synced/workspace"
SYNC_ROOTS = (SYNC_ROOT,)

EVICTED = (
    f"{SYNC_ROOT}/CLAUDE.md",
    f"{SYNC_ROOT}/AGENTS.md",
    f"{SYNC_ROOT}/context/focus-shim.md",
)

STAMP = (
    "<!-- context-shim v1 source=" + "a" * 40 + " generated=2026-10-01 sha256=" + "b" * 64 + " -->"
)


def _lens():
    """The three-valued verdict this row adds. Absent today: ``check_shims``
    inlines a clean-or-drifted decision and files an unreadable placeholder
    with the drift verdicts."""
    from auditor.lenses import context

    return context


def _placeholder_read(_path: str) -> str:
    """A dataless cloud placeholder read from a headless session: EDEADLK, and
    the read does not hydrate it (the 2026-08-11 incident, narrowed by #106)."""
    raise OSError(errno.EDEADLK, "Resource deadlock avoided")


def _hand_edited_read(_path: str) -> str:
    """A shim that was read successfully and whose body no longer matches the
    hash its own stamp carries."""
    return STAMP + "\nsomebody edited this by hand\n"


def test_an_unverifiable_shim_is_a_coverage_gap_not_drift():
    lens = _lens()

    states = {path: lens.shim_verification_state(path, read=_placeholder_read) for path in EVICTED}

    assert set(states.values()) == {"unverifiable"}, (
        "a read that could not happen says nothing about the file's content: "
        "unverifiable is its own state, never 'drifted'"
    )

    findings = lens.unverifiable_findings(EVICTED, sync_roots=SYNC_ROOTS)

    assert len(findings) == 1, (
        "three shims evicted from one mount are one arrangement, not three "
        "chores: the lens names the location once instead of a WARN per file"
    )
    finding = findings[0]
    assert finding.severity == "INFO", (
        "severity is a claim about what was learned, and nothing was learned "
        "about these files; the answerable question is the mount, not the file"
    )
    assert finding.condition == "unverifiable"
    assert SYNC_ROOT in finding.subject, "the finding is about the synced tree, named"
    assert "3" in finding.detail, (
        "the one finding says how many shims went unverified, so coverage is "
        "never reduced without the report saying by how much"
    )

    # An absent knob is the default configuration, not an opt-out: a tenant that
    # configures no synced trees sees today's per-file findings.
    ungrouped = lens.unverifiable_findings(EVICTED, sync_roots=())
    assert len(ungrouped) == 3, (
        "with no sync root configured the lens groups nothing; inferring a mount "
        "from a path substring is explaining what it cannot see"
    )


def test_a_readable_shim_that_disagrees_with_its_stamp_is_still_loud():
    lens = _lens()

    edited = f"{SYNC_ROOT}/CLAUDE.md"
    assert lens.shim_verification_state(edited, read=_hand_edited_read) == "drifted", (
        "a shim that WAS read and is wrong is drift at today's severity; the "
        "middle state must never be able to absorb a hand-edit"
    )

    assert lens.shim_verification_state(edited, read=_placeholder_read) != "verified", (
        "absence of evidence about a stamp is never evidence of a good one"
    )


# ---- end to end through check_shims --------------------------------------------


def _stamped(body: str) -> str:
    import hashlib

    digest = hashlib.sha256(body.encode()).hexdigest()
    return (
        f"<!-- context-shim v1 source={'a' * 40} generated=2026-10-01 sha256={digest} -->\n{body}"
    )


def _shims_ctx(tmp_path, monkeypatch, *, sync_roots, evicted_names, local_names=()):
    """Shims on disk under a fake synced tree; the named ones read as dataless
    placeholders. The context repo is left unconfigured-as-git, so HEAD is ''
    and stale-source never fires: these evals are about the read, not the source."""
    from auditor.lenses import context

    from .fixtures import make_context, make_ledger

    synced = tmp_path / "synced"
    paths = {}
    for name in (*evicted_names, *local_names):
        base = synced if name in evicted_names else tmp_path / "local"
        p = base / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_stamped("body\n"))
        paths[name] = p
    evicted = {paths[n] for n in evicted_names}
    real = context._shim_text

    def reader(path):
        if path in evicted:
            raise OSError(errno.EDEADLK, "Resource deadlock avoided", str(path))
        return real(path)

    monkeypatch.setattr(context, "_shim_text", reader)
    ledger_root = tmp_path / "ledger"
    make_ledger(ledger_root)
    ctx = make_context(
        ledger_root,
        context_repo=str(tmp_path / "not-a-repo"),
        context_shims=[str(p) for p in paths.values()],
        context_sync_roots=tuple(str(r) for r in sync_roots(synced)),
    )
    return ctx, synced, paths


def test_check_shims_groups_evicted_shims_under_a_configured_root(tmp_path, monkeypatch):
    from auditor.lenses import context

    ctx, synced, _ = _shims_ctx(
        tmp_path,
        monkeypatch,
        sync_roots=lambda s: (s,),
        evicted_names=("a/CLAUDE.md", "b/CLAUDE.md"),
    )
    with ctx.ledger:
        findings = context.check_shims(ctx)
    assert [(f.condition, f.severity) for f in findings] == [("unverifiable", "INFO")]
    assert str(synced) in findings[0].subject
    assert "2" in findings[0].detail


def test_check_shims_without_sync_roots_is_todays_per_file_warn(tmp_path, monkeypatch):
    # Absent knob == today's behaviour exactly: one cloud-only WARN per file,
    # same subject and condition, so existing fingerprints and mutes carry over.
    from auditor.lenses import context

    ctx, _, paths = _shims_ctx(
        tmp_path, monkeypatch, sync_roots=lambda s: (), evicted_names=("a/CLAUDE.md", "b/CLAUDE.md")
    )
    with ctx.ledger:
        findings = context.check_shims(ctx)
    assert sorted((f.condition, f.severity, f.subject) for f in findings) == sorted(
        ("cloud-only", "WARN", f"shim {p}") for p in paths.values()
    )


def test_an_evicted_shim_outside_every_root_keeps_its_own_finding(tmp_path, monkeypatch):
    from auditor.lenses import context

    ctx, synced, _ = _shims_ctx(
        tmp_path,
        monkeypatch,
        sync_roots=lambda s: (s / "elsewhere",),
        evicted_names=("a/CLAUDE.md",),
    )
    with ctx.ledger:
        findings = context.check_shims(ctx)
    assert [(f.condition, f.severity) for f in findings] == [("cloud-only", "WARN")]


def test_a_hand_edited_local_shim_stays_critical_beside_a_grouped_gap(tmp_path, monkeypatch):
    from auditor.lenses import context

    ctx, _, paths = _shims_ctx(
        tmp_path,
        monkeypatch,
        sync_roots=lambda s: (s,),
        evicted_names=("a/CLAUDE.md",),
        local_names=("CLAUDE.md",),
    )
    local = paths["CLAUDE.md"]
    local.write_text(local.read_text() + "a rule added by hand\n")
    with ctx.ledger:
        findings = context.check_shims(ctx)
    assert sorted((f.condition, f.severity) for f in findings) == [
        ("hand-edited", "CRITICAL"),
        ("unverifiable", "INFO"),
    ]


def test_only_edeadlk_reaches_the_unverifiable_state():
    from auditor.lenses import context

    def denied(_path):
        raise OSError(errno.EACCES, "Permission denied")

    import pytest

    with pytest.raises(OSError):
        context.shim_verification_state(EVICTED[0], read=denied)


def test_sync_roots_parse_from_tenant_toml(tmp_path):
    from auditor import config

    (tmp_path / "t").mkdir()
    (tmp_path / "t" / "tenant.toml").write_text(
        '[auditor.context]\nrepo = "/r"\nsync_roots = ["/synced/one", "~/two"]\n'
    )
    assert config.load_auditor_tenant("t", tenants_dir=tmp_path).context_sync_roots == (
        "/synced/one",
        "~/two",
    )
    (tmp_path / "u").mkdir()
    (tmp_path / "u" / "tenant.toml").write_text('[auditor.context]\nrepo = "/r"\n')
    assert config.load_auditor_tenant("u", tenants_dir=tmp_path).context_sync_roots == ()
