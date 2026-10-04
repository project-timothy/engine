"""The tenant model policy (phase 7 row 7.9, docs/model-seam-design.md).

``tenant.toml`` gains ``[llm.tiers]``, ``[llm.jobs]``, ``[llm.budget]``.
``core.llm.policy`` resolves a job type to a tier and the tier to an adapter,
a model id, and a price; ``complete_for`` wraps the gateway's ``complete()``
(signature untouched) so that every gateway call writes one ``llm_calls``
row, the run's result carries the totals, and the monthly budget refuses a
call past the cap with an ``llm.budget`` anomaly and a typed error the job
can catch, never a crash. Every test runs against the fixture adapter: no
network, no secret, no model.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import BaseModel, Field, ValidationError

from core.engine import runner as runner_mod
from core.engine.config import LLM_DETERMINISTIC, TenantConfig, default_tenants_root, load_tenant
from core.engine.contracts import JobContext, JobHandler, JobOutput
from core.engine.result import LlmTotals
from core.engine.runkey import RunKey
from core.engine.runner import resolve_ledger_root, run
from core.ledger import Ledger
from core.llm import DecimalString, GatewayTransportError, GatewayValidationError, Message, Usage
from core.llm.adapters.fixture import FixtureAdapter
from core.llm.policy import (
    BudgetExceeded,
    DeterministicJobError,
    LlmPolicyError,
    ResolvedModel,
    UnresolvedJobError,
    build_adapter,
    complete_for,
    resolve,
)
from core.llm.telemetry import month_to_date_usd, run_totals

TENANTS = default_tenants_root()

GOOD_REPLY = '{"vendor": "Acme Fasteners", "amount": "1875.00", "confidence": 0.91}'
BAD_REPLY = '{"vendor": "Acme Fasteners", "confidence": 0.91}'


class InvoiceLine(BaseModel):
    vendor: str
    amount: DecimalString
    confidence: float = Field(ge=0.0, le=1.0)


def _messages() -> list[Message]:
    return [
        Message(role="system", content="You classify documents for an intake system."),
        Message(role="user", content="Extract the vendor and total."),
    ]


def _settings(**overrides) -> dict:
    """A small in-memory tenant with two fixture tiers and one fallback."""
    data = {
        "identity": {"legal_name": "Policy Test Co", "slug": "demo"},
        "llm": {
            "tiers": {
                "cheap": {
                    "adapter": "fixture",
                    "model": "cheap-model",
                    "api_key_env": "TEST_CHEAP_KEY",
                    "pricing": {"input_usd_per_mtok": "1", "output_usd_per_mtok": "5"},
                    "fallback": ["strong"],
                },
                "strong": {
                    "adapter": "fixture",
                    "model": "strong-model",
                    "base_url": "http://localhost:4000/v1",
                    "api_key_env": "TEST_STRONG_KEY",
                    "pricing": {"input_usd_per_mtok": "3", "output_usd_per_mtok": "15"},
                },
            },
            "jobs": {
                "invoice_extract": "cheap",
                "draft_advisory": "strong",
                "w9_detect": "deterministic",
                "default": "strong",
            },
            "budget": {"monthly_usd": "25"},
        },
    }
    data["llm"].update(overrides)
    return data


def _ctx(tmp_path, data: dict | None = None, *, run_key: str = "demo.x.k1") -> JobContext:
    tenant = TenantConfig.model_validate(data or _settings())
    ledger = Ledger.open(tmp_path / "ledger")
    return JobContext(
        tenant=tenant,
        tenant_slug="demo",
        ledger=ledger,
        agent="demo",
        job="x",
        run_key=run_key,
    )


def _rows(ledger: Ledger) -> list[dict]:
    return [dict(r) for r in ledger.conn.execute("SELECT * FROM llm_calls ORDER BY id").fetchall()]


def _seed_spend(ledger: Ledger, usd: str, *, created_at: str, tenant: str = "demo") -> None:
    ledger.conn.execute(
        "INSERT INTO llm_calls (tenant, job_type, run_key, adapter, model, tokens_in, "
        "tokens_out, usd, retries, latency_ms, created_at) "
        "VALUES (?, 'invoice_extract', 'earlier', 'fixture', 'cheap-model', 10, 10, ?, 0, 1, ?)",
        (tenant, usd, created_at),
    )
    ledger.conn.commit()


# ---- config: the three tables parse, with defaults ----------------------------


def test_the_demo_tenant_carries_the_three_tables_on_the_fixture_adapter():
    cfg = load_tenant("demo", tenants_root=TENANTS)
    assert cfg.llm.tiers, "the demo tenant names at least one tier"
    for name, tier in cfg.llm.tiers.items():
        assert tier.adapter == "fixture", f"demo tier {name!r} must run on the fixture adapter"
        assert tier.pricing.input_usd_per_mtok >= 0
    assert cfg.llm.jobs["w9_detect"] == LLM_DETERMINISTIC
    model_jobs = {j for j, t in cfg.llm.jobs.items() if t != LLM_DETERMINISTIC}
    assert {"invoice_extract", "receipt_extract", "inbox_classify", "scan_group"} <= model_jobs
    assert cfg.llm.budget.monthly_usd == Decimal("25")


def test_an_absent_llm_table_means_no_tiers_no_jobs_no_cap():
    cfg = TenantConfig.model_validate({"identity": {"legal_name": "X", "slug": "x"}})
    assert cfg.llm.tiers == {}
    assert cfg.llm.jobs == {}
    assert cfg.llm.budget.monthly_usd is None


def test_pricing_parses_from_toml_numbers_and_strings():
    data = _settings()
    data["llm"]["tiers"]["cheap"]["pricing"] = {
        "input_usd_per_mtok": 0.25,
        "output_usd_per_mtok": 2,
    }
    cfg = TenantConfig.model_validate(data)
    cheap = cfg.llm.tiers["cheap"]
    assert cheap.pricing.input_usd_per_mtok == Decimal("0.25")
    assert cheap.pricing.output_usd_per_mtok == Decimal("2")
    assert cfg.llm.tiers["strong"].pricing.output_usd_per_mtok == Decimal("15")
    assert cheap.base_url == ""
    assert cheap.fallback == ["strong"]


def test_a_job_naming_an_unknown_tier_fails_config_validation_naming_the_job():
    data = _settings(jobs={"invoice_extract": "nope"})
    with pytest.raises(ValidationError, match="invoice_extract"):
        TenantConfig.model_validate(data)


def test_a_tier_named_deterministic_is_refused():
    data = _settings()
    data["llm"]["tiers"]["deterministic"] = data["llm"]["tiers"]["cheap"]
    with pytest.raises(ValidationError, match="deterministic"):
        TenantConfig.model_validate(data)


def test_a_fallback_must_name_another_existing_tier():
    data = _settings()
    data["llm"]["tiers"]["cheap"]["fallback"] = ["ghost"]
    with pytest.raises(ValidationError, match="ghost"):
        TenantConfig.model_validate(data)
    data["llm"]["tiers"]["cheap"]["fallback"] = ["cheap"]
    with pytest.raises(ValidationError, match="cheap"):
        TenantConfig.model_validate(data)


def test_an_unknown_adapter_name_is_refused_at_config_load():
    data = _settings()
    data["llm"]["tiers"]["cheap"]["adapter"] = "carrier_pigeon"
    with pytest.raises(ValidationError, match="carrier_pigeon"):
        TenantConfig.model_validate(data)


# ---- resolve: job type -> tier -> model --------------------------------------


def test_resolve_picks_the_tier_named_for_the_job():
    cfg = TenantConfig.model_validate(_settings())
    resolved = resolve(cfg.llm, "invoice_extract")
    assert isinstance(resolved, ResolvedModel)
    assert resolved.tier == "cheap"
    assert resolved.adapter == "fixture"
    assert resolved.model == "cheap-model"
    assert resolved.api_key_env == "TEST_CHEAP_KEY"
    assert resolved.pricing.input_usd_per_mtok == Decimal("1")
    assert resolved.pricing.output_usd_per_mtok == Decimal("5")
    assert resolved.fallback == ("strong",)


def test_resolve_falls_back_to_the_default_tier_when_present():
    cfg = TenantConfig.model_validate(_settings())
    assert resolve(cfg.llm, "some_new_job").tier == "strong"


def test_resolve_refuses_an_unlisted_job_without_a_default():
    jobs = {"invoice_extract": "cheap"}
    cfg = TenantConfig.model_validate(_settings(jobs=jobs))
    with pytest.raises(UnresolvedJobError, match="some_new_job") as info:
        resolve(cfg.llm, "some_new_job")
    assert isinstance(info.value, LlmPolicyError)


def test_resolving_a_deterministic_job_raises_a_typed_error_naming_the_job():
    cfg = TenantConfig.model_validate(_settings())
    with pytest.raises(DeterministicJobError, match="w9_detect") as info:
        resolve(cfg.llm, "w9_detect")
    assert isinstance(info.value, LlmPolicyError)


def test_the_seat_adapter_name_now_builds_a_real_complete_adapter():
    """Row 7.9 left ``claude_agent_sdk`` describe-only: the tier could say
    what the tenant ran but the policy refused to build it. Row 7.10 shipped
    the adapter, so the seat can SERVE a job at the same flat rate. Retargeted
    from the refusal test, intent preserved: the name still resolves, and what
    it builds is never a silent stand-in for another provider."""
    data = _settings()
    data["llm"]["tiers"]["seat"] = {
        "adapter": "claude_agent_sdk",
        "model": "default",
        "pricing": {"input_usd_per_mtok": "0", "output_usd_per_mtok": "0"},
    }
    data["llm"]["jobs"]["inbox_classify"] = "seat"
    cfg = TenantConfig.model_validate(data)
    resolved = resolve(cfg.llm, "inbox_classify")
    assert resolved.adapter == "claude_agent_sdk"
    assert build_adapter(resolved).name == "claude_agent_sdk"


def test_a_tier_can_be_named_directly_instead_of_resolving_the_job(tmp_path):
    """Row 7.10: ``--param extractor=`` pins a tier by name. The job type
    still names the telemetry row, and a job the table calls deterministic is
    refused even with a tier in hand."""
    ctx = _ctx(tmp_path)
    result = complete_for(
        ctx,
        "invoice_extract",
        _messages(),
        InvoiceLine,
        adapter=FixtureAdapter({"*": GOOD_REPLY}),
        tier="strong",
    )
    assert result.record.model == "strong-model"
    row = _rows(ctx.ledger)[0]
    assert row["tier"] == "strong"
    assert row["job_type"] == "invoice_extract"
    with pytest.raises(LlmPolicyError, match="ghost"):
        complete_for(ctx, "invoice_extract", _messages(), InvoiceLine, tier="ghost")
    with pytest.raises(DeterministicJobError):
        complete_for(ctx, "w9_detect", _messages(), InvoiceLine, tier="strong")


def test_build_adapter_constructs_the_named_adapter_from_the_tier():
    cfg = TenantConfig.model_validate(_settings())
    assert build_adapter(resolve(cfg.llm, "invoice_extract")).name == "fixture"
    data = _settings()
    data["llm"]["tiers"]["strong"]["adapter"] = "openai_compat"
    data["llm"]["tiers"]["cheap"]["adapter"] = "anthropic_messages"
    cfg = TenantConfig.model_validate(data)
    assert build_adapter(resolve(cfg.llm, "draft_advisory")).name == "openai_compat"
    assert build_adapter(resolve(cfg.llm, "invoice_extract")).name == "anthropic_messages"


# ---- complete_for: one row per gateway call ----------------------------------


def test_a_policy_call_writes_one_row_with_usd_from_the_tier_pricing(tmp_path):
    ctx = _ctx(tmp_path)
    adapter = FixtureAdapter({"invoice_extract": GOOD_REPLY}, usage=Usage(1000, 500))
    result = complete_for(ctx, "invoice_extract", _messages(), InvoiceLine, adapter=adapter)
    assert result.output.amount == "1875.00"
    assert result.record.model == "cheap-model"
    assert result.record.usd == Decimal("0.0035")  # 1000 * 1 + 500 * 5, per million
    (row,) = _rows(ctx.ledger)
    assert row["tenant"] == "demo"
    assert row["job_type"] == "invoice_extract"
    assert row["run_key"] == "demo.x.k1"
    assert row["tier"] == "cheap"
    assert row["adapter"] == "fixture"
    assert row["model"] == "cheap-model"
    assert row["tokens_in"] == 1000
    assert row["tokens_out"] == 500
    assert Decimal(row["usd"]) == Decimal("0.0035")
    assert row["retries"] == 0
    assert row["status"] == "ok"
    assert row["latency_ms"] >= 0
    assert row["created_at"]
    assert len(adapter.calls) == 1
    assert adapter.calls[0].model == "cheap-model"


def test_a_failed_validation_retry_is_one_row_with_retries_one(tmp_path):
    ctx = _ctx(tmp_path)
    adapter = FixtureAdapter([BAD_REPLY, GOOD_REPLY], usage=Usage(100, 10))
    result = complete_for(ctx, "invoice_extract", _messages(), InvoiceLine, adapter=adapter)
    assert result.record.retries == 1
    (row,) = _rows(ctx.ledger)
    assert row["retries"] == 1
    assert row["tokens_in"] == 200  # summed over both attempts
    assert row["status"] == "ok"


def test_two_bad_replies_raise_and_leave_one_failed_row(tmp_path):
    ctx = _ctx(tmp_path)
    adapter = FixtureAdapter([BAD_REPLY, BAD_REPLY])
    with pytest.raises(GatewayValidationError):
        complete_for(ctx, "invoice_extract", _messages(), InvoiceLine, adapter=adapter)
    (row,) = _rows(ctx.ledger)
    assert row["status"] == "validation_failed"
    assert row["retries"] == 1


def test_a_deterministic_job_never_reaches_the_adapter(tmp_path):
    ctx = _ctx(tmp_path)
    adapter = FixtureAdapter({"*": GOOD_REPLY})
    with pytest.raises(DeterministicJobError, match="w9_detect"):
        complete_for(ctx, "w9_detect", _messages(), InvoiceLine, adapter=adapter)
    assert adapter.calls == []
    assert _rows(ctx.ledger) == []


def test_a_transient_transport_failure_walks_the_fallback_tier(tmp_path):
    ctx = _ctx(tmp_path)

    def reply(bundle, schema):
        if bundle.model == "cheap-model":
            raise ConnectionError("cheap gateway unreachable")
        return GOOD_REPLY

    adapter = FixtureAdapter(reply, usage=Usage(10, 10))
    result = complete_for(ctx, "invoice_extract", _messages(), InvoiceLine, adapter=adapter)
    assert result.record.model == "strong-model"
    assert [b.model for b in adapter.calls] == ["cheap-model", "strong-model"]
    first, second = _rows(ctx.ledger)
    assert (first["tier"], first["status"]) == ("cheap", "transport_failed")
    assert (second["tier"], second["status"], second["model"]) == ("strong", "ok", "strong-model")
    assert Decimal(second["usd"]) == Decimal("0.00018")  # 10 * 3 + 10 * 15, per million


def test_a_non_transient_failure_does_not_walk_the_fallback(tmp_path):
    ctx = _ctx(tmp_path)
    adapter = FixtureAdapter(
        GatewayTransportError("refused", cause="refusal", transient=False), usage=Usage(1, 1)
    )
    with pytest.raises(GatewayTransportError):
        complete_for(ctx, "invoice_extract", _messages(), InvoiceLine, adapter=adapter)
    assert len(adapter.calls) == 1
    (row,) = _rows(ctx.ledger)
    assert row["status"] == "transport_failed"


# ---- the monthly budget ---------------------------------------------------------


def test_month_to_date_usd_counts_this_months_rows_only(tmp_path):
    ctx = _ctx(tmp_path)
    now = datetime(2026, 9, 12, 15, 0, tzinfo=UTC)
    _seed_spend(ctx.ledger, "10.50", created_at="2026-09-02T08:00:00+00:00")
    _seed_spend(ctx.ledger, "4.25", created_at="2026-09-11T08:00:00+00:00")
    _seed_spend(ctx.ledger, "99", created_at="2026-08-31T23:59:00+00:00")
    _seed_spend(ctx.ledger, "99", created_at="2026-09-05T08:00:00+00:00", tenant="other")
    assert month_to_date_usd(ctx.ledger.conn, "demo", now=now) == Decimal("14.75")


def test_a_call_past_the_monthly_cap_is_refused_before_any_call(tmp_path):
    ctx = _ctx(tmp_path)
    now = datetime(2026, 9, 12, 15, 0, tzinfo=UTC)
    _seed_spend(ctx.ledger, "30", created_at="2026-09-02T08:00:00+00:00")
    adapter = FixtureAdapter({"*": GOOD_REPLY})
    with pytest.raises(BudgetExceeded) as info:
        complete_for(ctx, "invoice_extract", _messages(), InvoiceLine, adapter=adapter, now=now)
    assert info.value.job_type == "invoice_extract"
    assert info.value.spent == Decimal("30")
    assert info.value.cap == Decimal("25")
    assert adapter.calls == [], "no call was made"
    seed, refusal = _rows(ctx.ledger)
    assert refusal["status"] == "budget_refused"
    assert refusal["tokens_in"] == 0 and refusal["tokens_out"] == 0
    assert Decimal(refusal["usd"]) == 0


def test_last_months_spend_does_not_count_against_this_month(tmp_path):
    ctx = _ctx(tmp_path)
    now = datetime(2026, 9, 12, 15, 0, tzinfo=UTC)
    _seed_spend(ctx.ledger, "30", created_at="2026-08-12T08:00:00+00:00")
    adapter = FixtureAdapter({"*": GOOD_REPLY})
    result = complete_for(
        ctx, "invoice_extract", _messages(), InvoiceLine, adapter=adapter, now=now
    )
    assert result.output.vendor == "Acme Fasteners"


def test_no_cap_means_no_refusal(tmp_path):
    ctx = _ctx(tmp_path, _settings(budget={}))
    _seed_spend(ctx.ledger, "1000", created_at=datetime.now(UTC).isoformat())
    adapter = FixtureAdapter({"*": GOOD_REPLY})
    assert complete_for(ctx, "invoice_extract", _messages(), InvoiceLine, adapter=adapter)


# ---- the run event carries the totals --------------------------------------------


def _handler(fn):
    return JobHandler(key=lambda ctx: RunKey(ctx, "x").config("llm").digest(), run=fn)


def _demo_ledger(tmp_path) -> Ledger:
    return Ledger.open(resolve_ledger_root("demo", tmp_path))


def test_the_run_result_carries_llm_totals_for_the_run(tmp_path, monkeypatch):
    def job(ctx):
        adapter = FixtureAdapter({"*": GOOD_REPLY}, usage=Usage(1000, 500))
        complete_for(ctx, "invoice_extract", _messages(), InvoiceLine, adapter=adapter)
        adapter2 = FixtureAdapter([BAD_REPLY, GOOD_REPLY], usage=Usage(100, 10))
        complete_for(ctx, "receipt_extract", _messages(), InvoiceLine, adapter=adapter2)
        return JobOutput(status="ok", summary="two model calls")

    monkeypatch.setattr(runner_mod, "get_job", lambda agent, name: _handler(job))
    result = run("demo", "demo", "x", ledger_dir=tmp_path)
    assert result.status == "ok", result.summary
    assert isinstance(result.llm, LlmTotals)
    assert result.llm.calls == 2
    assert result.llm.tokens_in == 1200
    assert result.llm.tokens_out == 520
    assert isinstance(result.llm.usd, Decimal)
    assert result.llm.usd > 0
    with _demo_ledger(tmp_path) as ledger:
        rows = _rows(ledger)
        assert len(rows) == 2
        assert {r["run_key"] for r in rows} == {result.idempotency_key}
        totals = run_totals(ledger.conn, result.idempotency_key)
        assert totals["calls"] == 2
        assert Decimal(str(totals["usd"])) == result.llm.usd
        # the stored run row carries the same block (the run event)
        stored = ledger.find_run(result.idempotency_key)
        assert stored is not None
        assert '"llm"' in stored.result_json
    # a replay reports the same totals without a new call
    replay = run("demo", "demo", "x", ledger_dir=tmp_path)
    assert replay.status == "noop"
    assert replay.llm.calls == 2
    with _demo_ledger(tmp_path) as ledger:
        assert len(_rows(ledger)) == 2


def test_a_run_with_no_model_call_carries_zero_totals(tmp_path):
    result = run("demo", "demo", "ingest", ledger_dir=tmp_path)
    assert result.status == "ok"
    assert result.llm == LlmTotals()
    assert result.llm.calls == 0 and result.llm.usd == 0


def test_a_budget_refusal_in_a_run_is_an_llm_budget_anomaly_not_a_crash(tmp_path, monkeypatch):
    with _demo_ledger(tmp_path) as ledger:
        _seed_spend(ledger, "30", created_at=datetime.now(UTC).isoformat())
    adapter = FixtureAdapter({"*": GOOD_REPLY})

    def job(ctx):
        try:
            complete_for(ctx, "invoice_extract", _messages(), InvoiceLine, adapter=adapter)
        except BudgetExceeded as exc:
            return JobOutput(status="ok", summary=f"routed to review: {exc}")
        return JobOutput(status="ok", summary="called")

    monkeypatch.setattr(runner_mod, "get_job", lambda agent, name: _handler(job))
    result = run("demo", "demo", "x", ledger_dir=tmp_path)
    assert result.status == "ok"
    assert "routed to review" in result.summary
    assert adapter.calls == []
    codes = [a.code for a in result.anomalies]
    assert codes == ["llm.budget"]
    (anomaly,) = result.anomalies
    assert "invoice_extract" in anomaly.detail
    assert "25" in anomaly.detail
    assert result.llm.calls == 0
    assert result.llm.usd == 0


def test_an_uncaught_budget_refusal_is_a_recorded_failure_with_the_anomaly(tmp_path, monkeypatch):
    with _demo_ledger(tmp_path) as ledger:
        _seed_spend(ledger, "30", created_at=datetime.now(UTC).isoformat())

    def job(ctx):
        complete_for(ctx, "invoice_extract", _messages(), InvoiceLine, adapter=FixtureAdapter({}))
        return JobOutput(status="ok", summary="unreachable")

    monkeypatch.setattr(runner_mod, "get_job", lambda agent, name: _handler(job))
    result = run("demo", "demo", "x", ledger_dir=tmp_path)
    assert result.status == "error"
    codes = {a.code for a in result.anomalies}
    assert {"job.exception", "llm.budget", "engine.run_failed"} <= codes
    assert result.llm.calls == 0
