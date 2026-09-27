"""Context lens evals: the owner's context system must not drift silently.

The canonical context is one git repo; a generator builds stamped shims from
it. The failure modes pinned here are the ones that actually happened in the
predecessor system (2026-07-22 rebuild): a mirror target silently deleted, a
hand-forked shim missing newly added rules, and a health check that whispered
instead of screaming.
"""

from __future__ import annotations

import errno
import hashlib

import pytest

from auditor import config
from auditor.lenses import context
from auditor.store import AuditorStore

from . import fixtures
from .fixtures import NOW, commit, init_ledger_repo, make_context, make_ledger, mark_pushed

FRESH = "2026-07-21T05:00:00+00:00"  # 1h before NOW
STALE = "2026-07-19T03:00:00+00:00"  # 51h before NOW, past the 48h default window
OLD = "2026-07-01T05:00:00+00:00"  # 20 days before NOW, past the 14d focus window


def _conditions(findings):
    return sorted(f.condition for f in findings)


def _wire_upstream(root):
    """Give the fixture repo real branch-tracking config so ``@{upstream}``
    resolves to the refs/remotes/origin/<branch> ref that mark_pushed moves."""
    branch = fixtures._git(root, "rev-parse", "--abbrev-ref", "HEAD")
    fixtures._git(root, "config", "remote.origin.url", str(root))
    fixtures._git(root, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
    fixtures._git(root, "config", f"branch.{branch}.remote", "origin")
    fixtures._git(root, "config", f"branch.{branch}.merge", f"refs/heads/{branch}")


def _repo(tmp_path):
    root = tmp_path / "ctx-repo"
    root.mkdir()
    init_ledger_repo(root)
    _wire_upstream(root)
    return root


def _head(root):
    return fixtures._git(root, "rev-parse", "HEAD")


def _write_shim(path, body, *, source, generated="2026-07-20"):
    # The stamp is written out literally so the format is pinned by this eval
    # text, not by importing the lens's own regex.
    digest = hashlib.sha256(body.encode()).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"<!-- context-shim v1 source={source} generated={generated} sha256={digest} -->\n{body}"
    )


def _ctx(tmp_path, **overrides):
    ledger_root = tmp_path / "ledger"
    make_ledger(ledger_root)
    return make_context(ledger_root, **overrides)


# ---- scope -----------------------------------------------------------------


def test_unconfigured_tenant_is_out_of_scope(tmp_path):
    ctx = _ctx(tmp_path)  # context_repo defaults to ""
    with ctx.ledger:
        assert context.check(ctx) == []


def test_fresh_system_is_quiet(tmp_path):
    root = _repo(tmp_path)
    (root / "focus.md").write_text("current\n")
    commit(root, date=FRESH)
    mark_pushed(root)
    shim = tmp_path / "shims" / "CLAUDE.md"
    _write_shim(shim, "body\n", source=_head(root))
    ctx = _ctx(
        tmp_path,
        context_repo=str(root),
        context_shims=[str(shim)],
        context_focus_file="focus.md",
        context_forbidden_paths=[str(tmp_path / "retired-copy")],
    )
    with ctx.ledger:
        assert context.check(ctx) == []


# ---- repo health -----------------------------------------------------------


def test_missing_repo_is_critical(tmp_path):
    ctx = _ctx(tmp_path, context_repo=str(tmp_path / "nowhere"))
    with ctx.ledger:
        findings = context.check_repo(ctx)
    assert _conditions(findings) == ["not-a-repo"]
    assert findings[0].severity == "CRITICAL"


def test_dirty_worktree_flags(tmp_path):
    root = _repo(tmp_path)
    (root / "uncommitted.md").write_text("edit in flight\n")
    ctx = _ctx(tmp_path, context_repo=str(root))
    with ctx.ledger:
        findings = context.check_repo(ctx)
    assert _conditions(findings) == ["dirty-worktree"]
    assert findings[0].severity == "WARN"


def test_never_pushed_repo_is_critical(tmp_path):
    root = tmp_path / "ctx-repo"
    root.mkdir()
    init_ledger_repo(root)  # no upstream wiring at all
    ctx = _ctx(tmp_path, context_repo=str(root))
    with ctx.ledger:
        findings = context.check_repo(ctx)
    assert _conditions(findings) == ["no-remote-ref"]
    assert findings[0].severity == "CRITICAL"


def test_old_unpushed_commit_flags(tmp_path):
    root = _repo(tmp_path)
    commit(root, date=STALE)  # after mark_pushed: an unpushed commit, 51h old
    ctx = _ctx(tmp_path, context_repo=str(root))
    with ctx.ledger:
        findings = context.check_repo(ctx)
    assert _conditions(findings) == ["unpushed-too-long"]
    assert findings[0].severity == "CRITICAL"


def test_young_unpushed_commit_is_quiet(tmp_path):
    root = _repo(tmp_path)
    commit(root, date=FRESH)  # unpushed but only 1h old
    ctx = _ctx(tmp_path, context_repo=str(root))
    with ctx.ledger:
        assert context.check_repo(ctx) == []


# ---- shims -----------------------------------------------------------------


def test_missing_shim_is_critical(tmp_path):
    root = _repo(tmp_path)
    ctx = _ctx(
        tmp_path, context_repo=str(root), context_shims=[str(tmp_path / "gone" / "CLAUDE.md")]
    )
    with ctx.ledger:
        findings = context.check_shims(ctx)
    assert _conditions(findings) == ["missing"]
    assert findings[0].severity == "CRITICAL"


def test_shim_without_stamp_flags(tmp_path):
    root = _repo(tmp_path)
    shim = tmp_path / "CLAUDE.md"
    shim.write_text("# hand-written file, no stamp\n")
    ctx = _ctx(tmp_path, context_repo=str(root), context_shims=[str(shim)])
    with ctx.ledger:
        findings = context.check_shims(ctx)
    assert _conditions(findings) == ["no-stamp"]
    assert findings[0].severity == "WARN"


def test_hand_edited_shim_is_critical(tmp_path):
    # The exact unrecoverable failure this system replaces: edits made to a
    # generated copy die at the next regenerate.
    root = _repo(tmp_path)
    shim = tmp_path / "CLAUDE.md"
    _write_shim(shim, "body\n", source=_head(root))
    shim.write_text(shim.read_text() + "a rule added by hand\n")
    ctx = _ctx(tmp_path, context_repo=str(root), context_shims=[str(shim)])
    with ctx.ledger:
        findings = context.check_shims(ctx)
    assert _conditions(findings) == ["hand-edited"]
    assert findings[0].severity == "CRITICAL"


def test_stale_source_shim_flags(tmp_path):
    root = _repo(tmp_path)
    shim = tmp_path / "CLAUDE.md"
    _write_shim(shim, "body\n", source=_head(root))
    commit(root, date=FRESH)  # repo moves on; the shim was not regenerated
    mark_pushed(root)
    ctx = _ctx(tmp_path, context_repo=str(root), context_shims=[str(shim)])
    with ctx.ledger:
        findings = context.check_shims(ctx)
    assert _conditions(findings) == ["stale-source"]
    assert findings[0].severity == "WARN"


# ---- cloud-only shims (2026-08-11 incident) --------------------------------


def _raise_edeadlk_for(evicted):
    """A ``_shim_text`` stand-in: EDEADLK for one path, real text otherwise.

    Same simulation contract as the AP intake evals for the 2026-07-09
    cutover-night incident: a cloud-sync placeholder with no local content
    fails ``read()`` from a headless session with EDEADLK. Keyed by full path,
    not basename — several real shims are all named CLAUDE.md."""
    real = context._shim_text

    def reader(path):
        if path == evicted:
            raise OSError(errno.EDEADLK, "Resource deadlock avoided", str(path))
        return real(path)

    return reader


def test_cloud_only_shim_flags_and_the_rest_still_audited(tmp_path, monkeypatch):
    # The 2026-08-11 incident: the sync client evicted a cloud-mounted shim to
    # a dataless placeholder and the 02:00 read crashed the whole lens, taking
    # all five context checks down. The contract: one unreadable placeholder
    # is its own finding, and every other shim still gets its audit.
    root = _repo(tmp_path)
    evicted = tmp_path / "cloud" / "CLAUDE.md"
    _write_shim(evicted, "body\n", source=_head(root))
    tampered = tmp_path / "local" / "CLAUDE.md"
    _write_shim(tampered, "body\n", source=_head(root))
    tampered.write_text(tampered.read_text() + "a rule added by hand\n")
    monkeypatch.setattr(context, "_shim_text", _raise_edeadlk_for(evicted))
    ctx = _ctx(tmp_path, context_repo=str(root), context_shims=[str(evicted), str(tampered)])
    with ctx.ledger:
        findings = context.check_shims(ctx)
    assert _conditions(findings) == ["cloud-only", "hand-edited"]
    cloud = next(f for f in findings if f.condition == "cloud-only")
    assert cloud.severity == "WARN"
    assert cloud.subject == f"shim {evicted}"


def test_other_read_errors_still_propagate(tmp_path, monkeypatch):
    # EDEADLK and only EDEADLK maps to a finding (same contract as AP intake's
    # _md5): an unexpected read error must stay a loud lens crash, not fold
    # into the cloud-placeholder story.
    root = _repo(tmp_path)
    shim = tmp_path / "CLAUDE.md"
    _write_shim(shim, "body\n", source=_head(root))

    def reader(path):
        raise OSError(errno.EACCES, "Permission denied", str(path))

    monkeypatch.setattr(context, "_shim_text", reader)
    ctx = _ctx(tmp_path, context_repo=str(root), context_shims=[str(shim)])
    with ctx.ledger, pytest.raises(OSError):
        context.check_shims(ctx)


# ---- focus -----------------------------------------------------------------


def test_fresh_focus_is_quiet(tmp_path):
    root = _repo(tmp_path)
    (root / "focus.md").write_text("current\n")
    commit(root, date=FRESH)
    ctx = _ctx(tmp_path, context_repo=str(root), context_focus_file="focus.md")
    with ctx.ledger:
        assert context.check_focus(ctx) == []


def test_stale_focus_flags(tmp_path):
    root = _repo(tmp_path)
    (root / "focus.md").write_text("ancient\n")
    commit(root, date=OLD)
    ctx = _ctx(tmp_path, context_repo=str(root), context_focus_file="focus.md")
    with ctx.ledger:
        findings = context.check_focus(ctx)
    assert _conditions(findings) == ["gone-stale"]
    assert findings[0].severity == "WARN"


def test_focus_under_the_size_cap_is_quiet(tmp_path):
    root = _repo(tmp_path)
    (root / "focus.md").write_text("x" * 900)
    commit(root, date=FRESH)
    ctx = _ctx(
        tmp_path,
        context_repo=str(root),
        context_focus_file="focus.md",
        context_focus_max_bytes=1_000,
    )
    with ctx.ledger:
        assert context.check_focus(ctx) == []


def test_focus_over_the_size_cap_flags_with_its_size(tmp_path):
    # focus.md is "one page"; it reached 156K before anything measured it
    root = _repo(tmp_path)
    (root / "focus.md").write_text("x" * 1_500)
    commit(root, date=FRESH)
    ctx = _ctx(
        tmp_path,
        context_repo=str(root),
        context_focus_file="focus.md",
        context_focus_max_bytes=1_000,
    )
    with ctx.ledger:
        findings = context.check_focus(ctx)
    assert _conditions(findings) == ["over-size"]
    assert findings[0].severity == "WARN"
    assert "1,500" in findings[0].detail and "1,000" in findings[0].detail


def test_stale_and_oversized_focus_names_both(tmp_path):
    root = _repo(tmp_path)
    (root / "focus.md").write_text("x" * 1_500)
    commit(root, date=OLD)
    ctx = _ctx(
        tmp_path,
        context_repo=str(root),
        context_focus_file="focus.md",
        context_focus_max_bytes=1_000,
    )
    with ctx.ledger:
        findings = context.check_focus(ctx)
    assert sorted(_conditions(findings)) == ["gone-stale", "over-size"]


def test_focus_size_cap_defaults_to_the_generator_soft_cap(tmp_path):
    # matches the context generator: warn past 25K, refuse past 40K
    (tmp_path / "t").mkdir()
    (tmp_path / "t" / "tenant.toml").write_text('[identity]\nslug = "t"\n')
    cfg = config.load_auditor_tenant("t", tenants_dir=tmp_path)
    assert cfg.context_focus_max_bytes == 25_000


def test_uncommitted_focus_flags(tmp_path):
    root = _repo(tmp_path)
    ctx = _ctx(tmp_path, context_repo=str(root), context_focus_file="focus.md")
    with ctx.ledger:
        findings = context.check_focus(ctx)
    assert _conditions(findings) == ["never-committed"]


# ---- fossils ---------------------------------------------------------------


def test_fossil_reappearance_flags(tmp_path):
    root = _repo(tmp_path)
    fossil = tmp_path / "old-context-copy"
    fossil.mkdir()
    ctx = _ctx(
        tmp_path,
        context_repo=str(root),
        context_forbidden_paths=[str(fossil), str(tmp_path / "still-absent")],
    )
    with ctx.ledger:
        findings = context.check_fossils(ctx)
    assert _conditions(findings) == ["reappeared"]
    assert findings[0].subject == f"fossil {fossil}"


# ---- monthly review card ---------------------------------------------------


def test_monthly_card_fires_on_first_run_of_month_only(tmp_path):
    root = _repo(tmp_path)
    ctx = _ctx(tmp_path, context_repo=str(root), context_monthly_review=True)
    # Empty store: first audit ever counts as the month's first run.
    with ctx.ledger:
        findings = context.check_monthly_review(ctx)
    assert _conditions(findings) == ["monthly-review-due"]
    assert findings[0].severity == "INFO"
    assert "2026-07" in findings[0].subject

    # A previous run earlier this month: the card stays quiet.
    with AuditorStore.open(ctx.store_root) as store:
        store.start_run("t", now="2026-07-02T02:00:00+00:00")
        store.start_run("t", now=NOW)  # tonight's own row must not count
    with ctx.ledger:
        assert context.check_monthly_review(ctx) == []


def test_monthly_card_fires_after_month_rollover(tmp_path):
    root = _repo(tmp_path)
    ctx = _ctx(tmp_path, context_repo=str(root), context_monthly_review=True)
    with AuditorStore.open(ctx.store_root) as store:
        store.start_run("t", now="2026-06-30T02:00:00+00:00")  # previous month
    with ctx.ledger:
        findings = context.check_monthly_review(ctx)
    assert _conditions(findings) == ["monthly-review-due"]


def test_monthly_card_disabled_is_quiet(tmp_path):
    root = _repo(tmp_path)
    ctx = _ctx(tmp_path, context_repo=str(root))  # monthly_review defaults off in evals
    with ctx.ledger:
        assert context.check_monthly_review(ctx) == []
