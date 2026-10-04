"""Run-key builder + undeclared-input audit (issue #153).

The disease these pin: a job reads an input its run key does not fold in,
so a change to that input replays the stale result (#117 vendor registry,
#121 unknown folder, #135 resolved month, #137 code version, #143 landing
change). The builder makes declaring uniform; the audit makes forgetting
loud — in the test suite, not in production.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest
from pydantic import BaseModel

from core.engine import runkey
from core.engine.config import TenantConfig
from core.engine.contracts import JobHandler, JobOutput
from core.engine.runkey import (
    InputTrail,
    RunKey,
    TracedConfig,
    TracedParams,
    file_digest,
    tracing,
    undeclared,
)

# ---------- fixtures ---------------------------------------------------------


def _tenant(**expenses) -> TenantConfig:
    return TenantConfig.model_validate(
        {
            "identity": {"legal_name": "Example Co", "slug": "example"},
            "expenses": {"category_accounts": {"Meals": "Meals"}, **expenses},
        }
    )


class _Ctx:
    """The slice of JobContext RunKey touches."""

    def __init__(self, tenant, params=None, shadow=False):
        self.tenant = tenant
        self.tenant_slug = "example"
        self.shadow = shadow
        self.params = params or {}


# ---------- builder ----------------------------------------------------------


def test_same_inputs_same_digest_and_parts_read_as_the_input_list():
    ctx = _Ctx(_tenant(), {"since": "2026-08-01"})
    a = RunKey(ctx, "intake").config("expenses.category_accounts")
    a.param("since")
    b = RunKey(ctx, "intake").config("expenses.category_accounts")
    b.param("since")
    assert a.digest() == b.digest()
    assert a.parts[:4] == ["tenant:example", "job:intake", "version:1", "mode:live"]
    assert any(p.startswith("config:expenses.category_accounts=") for p in a.parts)
    assert 'param:since="2026-08-01"' in a.parts


@pytest.mark.parametrize(
    "mutate",
    [
        lambda k: k.config("expenses.default_channel"),  # an extra declared input
        lambda k: k.env("ENGINE_TEST_ENV_X", "other"),
        lambda k: k.value("v", {"b": 1}),
    ],
)
def test_any_extra_part_changes_the_digest(mutate):
    ctx = _Ctx(_tenant())
    base = RunKey(ctx, "j").config("expenses.category_accounts").digest()
    other = RunKey(ctx, "j").config("expenses.category_accounts")
    mutate(other)
    assert other.digest() != base


def test_config_change_changes_the_digest_and_shadow_hashes_apart():
    a = RunKey(_Ctx(_tenant()), "j").config("expenses").digest()
    b = RunKey(_Ctx(_tenant(default_channel="Check")), "j").config("expenses").digest()
    c = RunKey(_Ctx(_tenant(), shadow=True), "j").config("expenses").digest()
    assert len({a, b, c}) == 3


def test_version_bump_re_runs_the_same_inputs():
    ctx = _Ctx(_tenant())
    assert RunKey(ctx, "j").digest() != RunKey(ctx, "j", version="2").digest()


def test_canonical_is_order_independent_for_dicts_and_models():
    class M(BaseModel):
        x: int
        y: str

    assert runkey.canonical({"b": 1, "a": [2, 3]}) == runkey.canonical({"a": [2, 3], "b": 1})
    assert runkey.canonical(M(x=1, y="z")) == runkey.canonical({"x": 1, "y": "z"})
    assert runkey.canonical({Path("a")}) == runkey.canonical(["a"])


def test_files_are_order_independent_and_content_sensitive(tmp_path):
    a, b = tmp_path / "a.pdf", tmp_path / "b.pdf"
    a.write_bytes(b"one")
    b.write_bytes(b"two")
    ctx = _Ctx(_tenant())
    k1 = RunKey(ctx, "j").files([a, b]).digest()
    k2 = RunKey(ctx, "j").files([b, a]).digest()
    assert k1 == k2
    b.write_bytes(b"two-changed")
    assert RunKey(ctx, "j").files([a, b]).digest() != k1


def test_file_digest_sentinels_keep_the_key_total(tmp_path, monkeypatch):
    missing = tmp_path / "gone.pdf"
    assert file_digest(missing) == runkey.MISSING
    placeholder = tmp_path / "evicted.pdf"
    placeholder.write_bytes(b"x")

    def _deadlock(self):
        # errno.EDEADLK, never the literal 11: on Linux 11 is EAGAIN and the
        # runner raised BlockingIOError instead (CI red 2026-08-26).
        raise OSError(errno.EDEADLK, "Resource deadlock avoided")

    monkeypatch.setattr(Path, "read_bytes", _deadlock)
    assert file_digest(placeholder) == runkey.CLOUD_ONLY


def test_ignore_requires_a_reason():
    with pytest.raises(ValueError):
        RunKey(_Ctx(_tenant()), "j").ignore("expenses.file_prefix", reason="  ")


# ---------- trail + audit ------------------------------------------------------


def test_traced_config_records_leaf_paths_only():
    trail = InputTrail()
    cfg = TracedConfig(_tenant())
    with tracing(trail):
        _ = cfg.expenses.category_accounts
        _ = cfg.identity.timezone
        section = cfg.expenses  # a section proxy is not a read
        assert isinstance(section, TracedConfig)
    assert trail.touched == {"expenses.category_accounts", "identity.timezone"}


def test_traced_params_records_lookups():
    trail = InputTrail()
    params = TracedParams({"since": "x"})
    with tracing(trail):
        params.get("since")
        _ = "month" in params
        _ = params.get("absent", "")
    assert trail.touched == {"param:since", "param:month", "param:absent"}


def test_undeclared_uses_prefix_coverage_and_counts_key_reads():
    key = InputTrail()
    key.declared = {"expenses", "param:since"}
    key.touched = {"identity.timezone"}  # read while computing the key
    run = InputTrail()
    run.touched = {
        "expenses.category_accounts",  # covered by the section declaration
        "identity.timezone",  # covered: the key read it
        "param:since",  # declared
        "param:month",  # NOT declared -> the #135 shape
        "registry:vendors",  # NOT declared -> the #117 shape
    }
    assert undeclared(key, run) == ["param:month", "registry:vendors"]


def test_nothing_is_recorded_outside_a_tracing_block():
    cfg = TracedConfig(_tenant())
    _ = cfg.expenses.category_accounts
    runkey.touch("registry:vendors")  # no active trail: silently ignored
    trail = InputTrail()
    with tracing(trail):
        pass
    assert trail.touched == set()


# ---------- through the runner --------------------------------------------------


def _install_probe_agent(monkeypatch, *, key_declares: bool):
    """A synthetic job whose run reads expenses.default_channel and --param
    month; its key declares them only when asked."""

    def _key(ctx):
        k = RunKey(ctx, "probe")
        if key_declares:
            k.config("expenses.default_channel")
            k.param("month")
        return k.digest()

    def _run(ctx):
        channel = ctx.tenant.expenses.default_channel
        month = ctx.params.get("month") or "2026-08"
        return JobOutput(status="ok", summary=f"{channel} {month}")

    handler = JobHandler(key=_key, run=_run)
    monkeypatch.setattr("core.engine.runner.get_job", lambda agent, job: handler)
    monkeypatch.setattr("core.engine.runner.agent_dir", lambda agent: Path("/nonexistent"))
    monkeypatch.setattr("core.engine.runner.load_tenant", lambda slug, tenants_root=None: _tenant())


def test_strict_audit_fails_a_job_that_reads_undeclared_inputs(tmp_path, monkeypatch):
    from core.engine.runner import run

    _install_probe_agent(monkeypatch, key_declares=False)
    monkeypatch.setenv(runkey.KEY_AUDIT_ENV, "strict")
    result = run("example", "probe", "probe", ledger_dir=tmp_path)
    assert result.status == "error"
    assert result.anomalies[0].code == "job.exception"
    assert "UndeclaredInputError" in result.anomalies[0].detail
    assert "expenses.default_channel" in result.anomalies[0].detail
    assert "param:month" in result.anomalies[0].detail
    # nothing recorded: the job re-runs once its key is fixed
    rerun = run("example", "probe", "probe", ledger_dir=tmp_path)
    assert rerun.status == "error"


def test_strict_audit_passes_a_job_whose_key_declares_what_run_reads(tmp_path, monkeypatch):
    from core.engine.runner import run

    _install_probe_agent(monkeypatch, key_declares=True)
    monkeypatch.setenv(runkey.KEY_AUDIT_ENV, "strict")
    first = run("example", "probe", "probe", ledger_dir=tmp_path)
    assert first.status == "ok"
    replay = run("example", "probe", "probe", ledger_dir=tmp_path)
    assert replay.status == "noop"
    # the #135 shape, fixed: a new --param month is a new run, not a replay
    fresh = run("example", "probe", "probe", ledger_dir=tmp_path, params={"month": "2026-09"})
    assert fresh.status == "ok"


def test_production_mode_never_traces_or_fails(tmp_path, monkeypatch):
    from core.engine.runner import run

    _install_probe_agent(monkeypatch, key_declares=False)
    monkeypatch.delenv(runkey.KEY_AUDIT_ENV, raising=False)
    assert os.environ.get(runkey.KEY_AUDIT_ENV) is None
    result = run("example", "probe", "probe", ledger_dir=tmp_path)
    assert result.status == "ok"
    assert isinstance(result.summary, str)


def test_vendor_registry_load_is_a_traced_input(tmp_path):
    from core.agents.ap.registry import load_vendor_registry

    (tmp_path / "vendors.toml").write_text('[acme]\nvendor = "Acme"\n', encoding="utf-8")
    trail = InputTrail()
    with tracing(trail):
        load_vendor_registry(tmp_path / "vendors.toml")
    assert "registry:vendors" in trail.touched
