"""Row 7.11: the combined-scan grouper runs through the model gateway.

``ClaudeGrouper`` held the grouping proposal against one hard-coded provider.
It becomes :class:`GatewayGrouper`, and which model answers is
``[llm.tiers]`` plus ``[llm.jobs].scan_group``.

The division of labor (issue #104) does not move an inch: the model PROPOSES
page groups, and CODE decides. ``_parse_groups_payload`` still parses the
proposal into :class:`ScanGroup` objects, ``validate_groups`` still refuses
anything that does not cover every page exactly once, and ``split_pdf``
still writes the children. A scan the gateway cannot group is HELD, never
filed, because filing a 17-receipt scan as one line is the failure the whole
pass exists to prevent.

What this eval pins:

- one gateway call per scan, recorded in ``llm_calls``, including on the
  fixture adapter;
- the natural reply is a list, so it rides inside an object
  (``{"groups": [...]}``, docs/model-seam-design.md), and the parsed groups
  still come back through the same code path;
- EVERY PAGE EXACTLY ONCE is still refused in code: a reply that lists a
  page twice, or skips one, holds the scan with an anomaly and files
  nothing, end to end through the intake job;
- the failure taxonomy: a transport blip or an unusable reply is a
  per-scan ``GroupingError`` (hold this file), while a missing ``[claude]``
  extra is a deployment fault that escapes;
- ``--param grouper=`` keeps ``fixture`` and ``claude`` for one release,
  plus ``tier:<name>``;
- the intake run key changes when the resolved ``scan_group`` tier changes.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from pypdf import PdfWriter

from core.agents.expenses.jobs import JOBS
from core.agents.expenses.scan_split import (
    SCAN_GROUP_JOB,
    FixtureGrouper,
    GatewayGrouper,
    GroupingError,
    ScanGroup,
    build_grouper,
    resolved_tier,
    validate_groups,
)
from core.engine.config import TenantConfig
from core.engine.contracts import JobContext
from core.engine.runner import resolve_ledger_root, run
from core.ledger import Ledger
from core.llm import GatewayTransportError, policy
from core.llm.adapters.fixture import FixtureAdapter

PERSON = "Pat Owner"

GOOD_REPLY = json.dumps(
    {
        "groups": [
            {"pages": [1], "vendor": "Kala Coffeehouse", "amount": "108.37", "date": "2026-08-08"},
            {"pages": [2], "vendor": "Bucks Tavern", "amount": "29.25", "date": "2026-07-09"},
            {"pages": [3], "vendor": "Sunoco", "amount": "41.10", "date": "2026-07-11"},
        ]
    }
)

PAGE_TWICE_REPLY = json.dumps(
    {"groups": [{"pages": [1, 2], "vendor": "Kala"}, {"pages": [2, 3], "vendor": "Bucks"}]}
)

PAGE_SKIPPED_REPLY = json.dumps(
    {"groups": [{"pages": [1], "vendor": "Kala"}, {"pages": [3], "vendor": "Sunoco"}]}
)


def _tenant_data(*, seat_model: str = "seat-model", with_local: bool = True) -> dict:
    tiers: dict[str, dict] = {
        "seat": {
            "adapter": "fixture",
            "model": seat_model,
            "api_key_env": "",
            "pricing": {"input_usd_per_mtok": "0", "output_usd_per_mtok": "0"},
        }
    }
    if with_local:
        tiers["local"] = {
            "adapter": "fixture",
            "model": "local-coder",
            "base_url": "http://localhost:4000/v1",
            "api_key_env": "ENGINE_GATEWAY_KEY",
            "pricing": {"input_usd_per_mtok": "0", "output_usd_per_mtok": "0"},
        }
    return {
        "identity": {"legal_name": "Split Test Co", "slug": "demo"},
        "llm": {
            "tiers": tiers,
            "jobs": {"scan_group": "seat", "receipt_extract": "seat"},
            "budget": {"monthly_usd": "25"},
        },
    }


def _ctx(tmp_path: Path, data: dict | None = None, **params) -> JobContext:
    tenant = TenantConfig.model_validate(data or _tenant_data())
    ledger = Ledger.open(tmp_path / "ledger-unit")
    return JobContext(
        tenant=tenant,
        tenant_slug="demo",
        ledger=ledger,
        agent="expenses",
        job="intake",
        params=params,
        run_key="demo.expenses.intake.k1",
    )


def _pdf_bytes(pages: int, width: int = 200) -> bytes:
    writer = PdfWriter()
    for i in range(pages):
        writer.add_blank_page(width=width + 10 * i, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _scan(tmp_path: Path, name: str = "combined.pdf", pages: int = 3) -> Path:
    path = tmp_path / name
    path.write_bytes(_pdf_bytes(pages))
    return path


def _rows(ctx: JobContext) -> list[dict]:
    return [
        dict(r) for r in ctx.ledger.conn.execute("SELECT * FROM llm_calls ORDER BY id").fetchall()
    ]


# ---- one call, one row, one proposal -----------------------------------------


def test_grouping_goes_through_the_policy_and_records_one_llm_call(tmp_path):
    ctx = _ctx(tmp_path)
    adapter = FixtureAdapter({"*": GOOD_REPLY})

    groups = GatewayGrouper(ctx, adapter=adapter).propose_groups(_scan(tmp_path), 3)

    assert [g.pages for g in groups] == [[1], [2], [3]]
    assert groups[0].vendor == "Kala Coffeehouse" and groups[0].amount == "108.37"
    assert isinstance(groups[0], ScanGroup)
    rows = _rows(ctx)
    assert len(rows) == 1, "one gateway call, one llm_calls row"
    assert rows[0]["job_type"] == SCAN_GROUP_JOB
    assert rows[0]["tier"] == "seat"
    assert rows[0]["adapter"] == "fixture"
    assert rows[0]["model"] == "seat-model"
    assert rows[0]["status"] == "ok"


def test_the_scan_rides_as_an_attachment_and_the_prompt_names_the_page_count(tmp_path):
    ctx = _ctx(tmp_path)
    adapter = FixtureAdapter({"*": GOOD_REPLY})
    scan = _scan(tmp_path)

    GatewayGrouper(ctx, adapter=adapter).propose_groups(scan, 3)

    bundle = adapter.calls[0]
    assert [(a.path, a.mime) for a in bundle.attachments] == [(scan, "application/pdf")]
    asked = "\n".join(m.content for m in bundle.turns())
    assert "3" in asked
    assert "exactly one group" in bundle.system_text()


# ---- every page exactly once: still refused in code ---------------------------


def test_a_reply_that_lists_a_page_twice_is_refused_in_code(tmp_path):
    ctx = _ctx(tmp_path)
    groups = GatewayGrouper(ctx, adapter=FixtureAdapter({"*": PAGE_TWICE_REPLY})).propose_groups(
        _scan(tmp_path), 3
    )
    reason = validate_groups(groups, 3)
    assert "exactly once" in reason


def test_a_reply_that_skips_a_page_is_refused_in_code(tmp_path):
    ctx = _ctx(tmp_path)
    groups = GatewayGrouper(ctx, adapter=FixtureAdapter({"*": PAGE_SKIPPED_REPLY})).propose_groups(
        _scan(tmp_path), 3
    )
    reason = validate_groups(groups, 3)
    assert "exactly once" in reason


def test_a_reply_with_no_groups_at_all_is_refused_in_code(tmp_path):
    ctx = _ctx(tmp_path)
    groups = GatewayGrouper(ctx, adapter=FixtureAdapter({"*": '{"groups": []}'})).propose_groups(
        _scan(tmp_path), 3
    )
    assert validate_groups(groups, 3) == "the grouper proposed no groups"


# ---- the failure taxonomy -----------------------------------------------------


def test_a_transport_blip_holds_this_scan_instead_of_failing_the_job(tmp_path):
    ctx = _ctx(tmp_path)
    adapter = FixtureAdapter(
        GatewayTransportError("the pipe broke", cause="transport_error", transient=True)
    )
    with pytest.raises(GroupingError):
        GatewayGrouper(ctx, adapter=adapter).propose_groups(_scan(tmp_path), 3)


def test_an_unusable_reply_holds_this_scan(tmp_path):
    ctx = _ctx(tmp_path)
    adapter = FixtureAdapter(["not json at all", "still not json"])
    with pytest.raises(GroupingError):
        GatewayGrouper(ctx, adapter=adapter).propose_groups(_scan(tmp_path), 3)
    assert _rows(ctx)[0]["status"] == "validation_failed"


def test_a_missing_sdk_extra_is_a_deployment_fault_not_a_grouping_error(tmp_path):
    ctx = _ctx(tmp_path)
    adapter = FixtureAdapter(
        GatewayTransportError("install the [claude] extra", cause="sdk_missing", transient=False)
    )
    with pytest.raises(GatewayTransportError) as caught:
        GatewayGrouper(ctx, adapter=adapter).propose_groups(_scan(tmp_path), 3)
    assert not isinstance(caught.value, GroupingError)
    assert caught.value.cause == "sdk_missing"


def test_the_split_cap_still_refuses_before_any_call(tmp_path):
    from core.agents.expenses.scan_split import MAX_SPLIT_FILE_BYTES

    ctx = _ctx(tmp_path)
    adapter = FixtureAdapter({"*": GOOD_REPLY})
    big = tmp_path / "huge.pdf"
    with big.open("wb") as fh:
        fh.seek(MAX_SPLIT_FILE_BYTES)
        fh.write(b"\0")

    with pytest.raises(GroupingError, match="split cap"):
        GatewayGrouper(ctx, adapter=adapter).propose_groups(big, 40)

    assert adapter.calls == []
    assert _rows(ctx) == [], "a file refused by the cap costs no model call"


# ---- end to end through the intake job ----------------------------------------


def _params(tmp_path, **extra):
    return {
        "drop_dir": str(tmp_path / "drop"),
        "filing_dir": str(tmp_path / "filing"),
        "month": "2026-08",
        "extractor": "fixture",
        "grouper": "claude",
        **extra,
    }


def _drop_scan(tmp_path, name: str = "combined.pdf", pages: int = 3) -> Path:
    target = tmp_path / "drop" / PERSON / "P26_2001"
    target.mkdir(parents=True, exist_ok=True)
    path = target / name
    path.write_bytes(_pdf_bytes(pages))
    return path


def _run_intake(tmp_path, monkeypatch, reply: str, **extra):
    adapter = FixtureAdapter({"*": reply})
    monkeypatch.setattr(policy, "build_adapter", lambda resolved: adapter)
    result = run(
        "demo",
        "expenses",
        "intake",
        params=_params(tmp_path, **extra),
        ledger_dir=tmp_path / "ledger",
    )
    return result, adapter


def test_a_gateway_grouped_scan_splits_and_files_in_the_same_run(tmp_path, monkeypatch):
    scan = _drop_scan(tmp_path)

    result, adapter = _run_intake(tmp_path, monkeypatch, GOOD_REPLY)

    assert len(adapter.calls) == 1
    assert not scan.exists(), "the original moved to _originals/"
    children = sorted(p.name for p in scan.parent.glob("*.pdf"))
    assert len(children) == 3
    assert any("Kala" in name for name in children)
    assert result.status in {"ok", "needs_approval"}


def test_a_page_listed_twice_holds_the_scan_and_files_nothing(tmp_path, monkeypatch):
    scan = _drop_scan(tmp_path)

    result, _ = _run_intake(tmp_path, monkeypatch, PAGE_TWICE_REPLY)

    assert scan.exists(), "the scan is held in the drop tree"
    assert sorted(p.name for p in scan.parent.glob("*.pdf")) == ["combined.pdf"]
    assert any(a.code == "expenses.scan_split_invalid" for a in result.anomalies)
    root = resolve_ledger_root("demo", tmp_path / "ledger")
    with Ledger.open(root) as ledger:
        assert [e for e in ledger.read_event_log() if e["event_type"] == "expense.scan_split"] == []


def test_a_transport_failure_holds_the_scan_with_an_anomaly(tmp_path, monkeypatch):
    scan = _drop_scan(tmp_path)
    adapter = FixtureAdapter(GatewayTransportError("gone", cause="transport_error"))
    monkeypatch.setattr(policy, "build_adapter", lambda resolved: adapter)

    result = run(
        "demo",
        "expenses",
        "intake",
        params=_params(tmp_path),
        ledger_dir=tmp_path / "ledger",
    )

    assert scan.exists()
    assert any(a.code == "expenses.scan_split_failed" for a in result.anomalies)


# ---- the aliases -------------------------------------------------------------


def test_the_claude_alias_maps_to_the_tier_the_policy_names_for_the_job(tmp_path):
    ctx = _ctx(tmp_path)
    assert resolved_tier(ctx, "claude") == ("seat", "fixture", "seat-model")
    assert isinstance(build_grouper("claude", ctx), GatewayGrouper)


def test_a_tier_can_be_named_directly(tmp_path):
    ctx = _ctx(tmp_path)
    assert resolved_tier(ctx, "tier:local") == ("local", "fixture", "local-coder")
    assert isinstance(build_grouper("tier:local", ctx), GatewayGrouper)


def test_the_fixture_alias_needs_no_tenant_policy_at_all(tmp_path):
    assert isinstance(build_grouper("fixture"), FixtureGrouper)
    assert resolved_tier(_ctx(tmp_path), "fixture") is None


def test_an_unknown_grouper_fails_loudly_with_the_list(tmp_path):
    ctx = _ctx(tmp_path)
    with pytest.raises(ValueError) as caught:
        build_grouper("gpt5", ctx)
    message = str(caught.value)
    for legal in ("fixture", "claude", "tier:"):
        assert legal in message


def test_a_tier_that_does_not_exist_fails_loudly_naming_the_tenant_tiers(tmp_path):
    ctx = _ctx(tmp_path)
    with pytest.raises(ValueError) as caught:
        build_grouper("tier:ghost", ctx)
    assert "ghost" in str(caught.value) and "seat" in str(caught.value)


def test_a_live_grouper_without_a_context_says_so(tmp_path):
    with pytest.raises(ValueError, match="tenant"):
        build_grouper("claude")


# ---- the run key -------------------------------------------------------------


def _intake_key(tmp_path: Path, data: dict) -> str:
    drop = tmp_path / "drop" / PERSON / "P26_2001"
    drop.mkdir(parents=True, exist_ok=True)
    (drop / "combined.pdf").write_bytes(_pdf_bytes(3))
    ctx = _ctx(
        tmp_path,
        data,
        drop_dir=str(tmp_path / "drop"),
        filing_dir=str(tmp_path / "filing"),
        month="2026-08",
        grouper="claude",
        extractor="fixture",
    )
    return JOBS["intake"].key(ctx)


def test_the_intake_run_key_changes_when_the_resolved_group_tier_changes(tmp_path):
    same = _intake_key(tmp_path, _tenant_data())
    assert same == _intake_key(tmp_path, _tenant_data()), "same policy, same key, no work"
    assert _intake_key(tmp_path, _tenant_data(seat_model="a-different-model")) != same


def test_the_intake_run_key_changes_when_the_job_is_pointed_at_another_tier(tmp_path):
    same = _intake_key(tmp_path, _tenant_data())
    data = _tenant_data()
    data["llm"]["jobs"]["scan_group"] = "local"
    assert _intake_key(tmp_path, data) != same
