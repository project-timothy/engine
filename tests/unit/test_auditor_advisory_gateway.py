"""Row 7.12: the advisory drafter goes through a model seam the auditor owns.

The auditor imports nothing from ``core`` (``auditor.evals.independence_lint``),
so ``auditor/advisory/llm_client.py`` is a deliberate minimal copy of the
seam's shape: read the tenant's own ``[llm]`` tables, resolve the
``draft_advisory`` job to a tier, speak to the adapter that tier names. These
tests sit on the ENGINE side of the boundary, where importing both copies is
allowed, the same way ``tests/unit/test_auditor_fixture_schema_parity.py``
does for the ledger DDL: the parity test is the tripwire that keeps the two
copies in step.

Four facts the row must hold, each with its own test below: the tenant's
policy chooses the tier; the request carries NO tools; any failure degrades
to the deterministic fallback voice AND the report says so; ``--local-only``
never constructs a client at all.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from auditor.advisory import draft, llm_client, render
from auditor.evals.fixtures import make_ledger
from auditor.runner import run_audit

FIXTURES = Path(__file__).parent / "fixtures" / "llm"
FACTS = json.loads((FIXTURES / "advisory_facts.json").read_text(encoding="utf-8"))
NOW = datetime(2026, 7, 21, 6, 0, tzinfo=UTC)

KEY_VAR = "TEST_ADVISORY_API_KEY"
FAKE_KEY = "sk-test-not-a-real-key"

ANTHROPIC_POLICY = f"""
[llm.tiers]
strong = {{ adapter = "anthropic_messages", model = "recorded-anthropic-model", \
api_key_env = "{KEY_VAR}", pricing = {{ input_usd_per_mtok = "3", output_usd_per_mtok = "15" }} }}

[llm.jobs]
draft_advisory = "strong"
"""

OPENAI_POLICY = f"""
[llm.tiers]
strong = {{ adapter = "openai_compat", model = "recorded-openai-compatible-model", \
base_url = "http://localhost:4000/v1", api_key_env = "{KEY_VAR}", \
pricing = {{ input_usd_per_mtok = "0", output_usd_per_mtok = "0" }} }}

[llm.jobs]
draft_advisory = "strong"
"""

SEAT_POLICY = """
[llm.tiers]
seat = { adapter = "claude_agent_sdk", model = "default", api_key_env = "", \
pricing = { input_usd_per_mtok = "0", output_usd_per_mtok = "0" } }

[llm.jobs]
draft_advisory = "seat"
"""


def _policy(body: str) -> dict:
    import tomllib

    return tomllib.loads(body)["llm"]


class _FakeHTTPResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> _FakeHTTPResponse:
        return self

    def __exit__(self, *exc) -> None:
        return None


def _recorded(monkeypatch, response_name: str) -> list:
    """Patch the vendored client's transport with a recorded reply; return the
    list the sent requests land in."""
    payload = (FIXTURES / response_name).read_bytes()
    seen: list = []

    def fake_urlopen(request, timeout=None):
        seen.append((request, timeout))
        return _FakeHTTPResponse(payload)

    monkeypatch.setattr(llm_client, "urlopen", fake_urlopen)
    return seen


def _world(tmp_path, policy_body: str = ""):
    tdir = tmp_path / "tenants" / "t"
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "tenant.toml").write_text(
        '[identity]\nslug = "t"\ntimezone = "UTC"\n' + policy_body, encoding="utf-8"
    )
    make_ledger(tmp_path / "ledger" / "t").close()
    return {
        "tenants_dir": tmp_path / "tenants",
        "ledger_dir": tmp_path / "ledger",
        "store_dir": tmp_path / "store",
        "report_dir": tmp_path / "reports",
    }


def _advisory(report_text: str) -> str:
    return report_text[report_text.index("## Advisory") :]


# ---- the tenant's policy chooses the tier -----------------------------------


def test_resolve_sends_the_advisory_job_to_the_tier_the_tenant_names():
    tier = llm_client.resolve(_policy(ANTHROPIC_POLICY), llm_client.ADVISORY_JOB)
    assert tier.name == "strong"
    assert tier.adapter == "anthropic_messages"
    assert tier.model == "recorded-anthropic-model"
    assert tier.api_key_env == KEY_VAR


def test_resolve_takes_the_default_entry_for_an_unlisted_job():
    policy = _policy(ANTHROPIC_POLICY)
    policy["jobs"] = {"default": "strong"}
    assert llm_client.resolve(policy, llm_client.ADVISORY_JOB).name == "strong"


def test_resolve_refuses_an_unlisted_job_a_deterministic_one_and_an_unknown_tier():
    policy = _policy(ANTHROPIC_POLICY)
    policy["jobs"] = {"something_else": "strong"}
    with pytest.raises(llm_client.PolicyError) as unlisted:
        llm_client.resolve(policy, llm_client.ADVISORY_JOB)
    assert llm_client.ADVISORY_JOB in str(unlisted.value)

    policy["jobs"] = {llm_client.ADVISORY_JOB: llm_client.DETERMINISTIC}
    with pytest.raises(llm_client.PolicyError) as deterministic:
        llm_client.resolve(policy, llm_client.ADVISORY_JOB)
    assert llm_client.DETERMINISTIC in str(deterministic.value)

    policy["jobs"] = {llm_client.ADVISORY_JOB: "nowhere"}
    with pytest.raises(llm_client.PolicyError) as unknown:
        llm_client.resolve(policy, llm_client.ADVISORY_JOB)
    assert "nowhere" in str(unknown.value)


def test_an_empty_policy_keeps_the_seat_path_the_auditor_runs_today():
    tier = llm_client.resolve({}, llm_client.ADVISORY_JOB)
    assert tier.adapter == llm_client.ADAPTER_SEAT


# ---- the request carries no tools -------------------------------------------


def test_the_anthropic_request_matches_the_recording_and_carries_no_tools(monkeypatch):
    seen = _recorded(monkeypatch, "advisory_anthropic_response.json")
    monkeypatch.setenv(KEY_VAR, FAKE_KEY)

    text = draft.draft_counsel(FACTS, llm=_policy(ANTHROPIC_POLICY))

    assert "Acme Fasteners A-100 is 41 days old at $1,875.00" in text
    (request, timeout), *rest = seen
    assert rest == []
    assert request.full_url == llm_client.ANTHROPIC_ENDPOINT
    assert timeout == draft.DRAFT_TIMEOUT_S
    headers = {k.lower(): v for k, v in request.header_items()}
    assert headers["x-api-key"] == FAKE_KEY
    assert headers["anthropic-version"] == llm_client.ANTHROPIC_API_VERSION
    body = json.loads(request.data)
    expected = json.loads((FIXTURES / "advisory_anthropic_request.json").read_text("utf-8"))
    assert body == expected
    # The "no tools" clause of the row, testable: no tools key, and one turn.
    assert "tools" not in body
    assert [m["role"] for m in body["messages"]] == ["user"]


def test_the_openai_compat_request_matches_the_recording_and_carries_no_tools(monkeypatch):
    seen = _recorded(monkeypatch, "advisory_openai_response.json")
    monkeypatch.setenv(KEY_VAR, FAKE_KEY)

    text = draft.draft_counsel(FACTS, llm=_policy(OPENAI_POLICY))

    assert "Acme Fasteners A-100 sits at 41 days ($1,875.00)" in text
    (request, _timeout), *rest = seen
    assert rest == []
    assert request.full_url == "http://localhost:4000/v1/chat/completions"
    body = json.loads(request.data)
    expected = json.loads((FIXTURES / "advisory_openai_request.json").read_text("utf-8"))
    assert body == expected
    assert "tools" not in body
    assert [m["role"] for m in body["messages"]] == ["system", "user"]


def test_the_prompt_the_seat_path_sends_is_unchanged_by_the_split(monkeypatch):
    """The seam splits the one prompt into a system turn and a facts turn. The
    seat path rejoins them, so the string the SDK sees is byte-identical to the
    string it saw before this row."""
    prompt = llm_client.Prompt(
        job_type=llm_client.ADVISORY_JOB,
        model="default",
        system=draft.SYSTEM_PROMPT,
        user=draft.facts_turn(FACTS),
        timeout_s=1,
    )
    assert prompt.joined() == draft.PROMPT.format(facts=json.dumps(FACTS, indent=1, sort_keys=True))


# ---- a missing key, a dead provider, a missing SDK --------------------------


def test_a_missing_key_names_the_variable_and_never_a_value(monkeypatch):
    monkeypatch.delenv(KEY_VAR, raising=False)
    with pytest.raises(llm_client.TransportError) as info:
        draft.draft_counsel(FACTS, llm=_policy(ANTHROPIC_POLICY))
    assert KEY_VAR in str(info.value)
    assert FAKE_KEY not in str(info.value)
    assert info.value.cause == "no_api_key"


def test_the_seat_tier_keeps_todays_claude_agent_sdk_path(monkeypatch):
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
    with pytest.raises(draft.SdkMissing) as info:
        draft.draft_counsel(FACTS, llm=_policy(SEAT_POLICY))
    assert "[claude]" in str(info.value)
    assert issubclass(draft.SdkMissing, ModuleNotFoundError)


def test_failure_labels_are_short_words_a_report_can_print():
    assert llm_client.failure_label(llm_client.TransportError("x", cause="timeout")) == "timeout"
    assert llm_client.failure_label(llm_client.PolicyError("x")) == "policy"
    assert llm_client.failure_label(llm_client.SdkMissing("x")) == "sdk_missing"
    assert llm_client.failure_label(ValueError("x")) == "drafter_error"


# ---- the report names the fallback -----------------------------------------


def test_render_advisory_names_the_fallback_voice_and_its_reason():
    quiet = render.render_advisory({}, "All quiet in the books.")
    assert render.FALLBACK_NOTICE not in quiet

    named = render.render_advisory({}, "All quiet in the books.", fallback_reason="no_api_key")
    assert render.FALLBACK_NOTICE in named
    assert "no_api_key" in named


def test_a_failed_draft_renders_the_fallback_and_the_report_names_it(tmp_path, monkeypatch):
    monkeypatch.delenv(KEY_VAR, raising=False)  # the tier's key is unset: no call possible
    world = _world(tmp_path, ANTHROPIC_POLICY)
    result = run_audit("t", lenses=[], now=NOW, **world)

    assert result.report_path is not None
    advisory = _advisory(result.report_text)
    assert "All quiet in the books." in advisory
    assert render.FALLBACK_NOTICE in advisory
    assert "no_api_key" in advisory
    # One line, no secrets and no stack trace: the variable NAME never leaks
    # into the report either, and nothing looks like a traceback.
    assert KEY_VAR not in result.report_text
    assert "Traceback" not in result.report_text


def test_local_only_never_constructs_a_client(tmp_path, monkeypatch):
    built: list = []

    def never(tier):
        built.append(tier)
        raise AssertionError("--local-only must never construct a model client")

    monkeypatch.setattr(llm_client, "build_client", never)
    world = _world(tmp_path, ANTHROPIC_POLICY)
    result = run_audit("t", lenses=[], now=NOW, local_only=True, **world)

    assert built == []
    advisory = _advisory(result.report_text)
    assert render.FALLBACK_NOTICE in advisory
    assert render.LOCAL_ONLY_REASON in advisory


def test_an_injected_drafter_still_speaks_in_its_own_voice(tmp_path):
    world = _world(tmp_path, ANTHROPIC_POLICY)
    result = run_audit("t", lenses=[], now=NOW, drafter=lambda facts: "Counsel.", **world)
    advisory = _advisory(result.report_text)
    assert "Counsel." in advisory
    assert render.FALLBACK_NOTICE not in advisory


# ---- the parity tripwire ----------------------------------------------------


def test_the_vendored_seam_matches_the_engines():
    """The auditor's copy may be smaller than the engine's; it may not
    DISAGREE with it. This test lives here because it is the one place both
    sides may be imported."""
    from core.engine.config import LLM_ADAPTERS, LLM_DETERMINISTIC
    from core.llm.adapters import anthropic_messages, openai_compat
    from core.llm.policy import DEFAULT_JOB, ResolvedModel

    assert llm_client.ADAPTERS == LLM_ADAPTERS
    assert llm_client.DETERMINISTIC == LLM_DETERMINISTIC
    assert llm_client.DEFAULT_JOB == DEFAULT_JOB
    assert llm_client.ANTHROPIC_ENDPOINT == anthropic_messages.DEFAULT_ENDPOINT
    assert llm_client.ANTHROPIC_API_VERSION == anthropic_messages.API_VERSION
    assert llm_client.NO_AUTH_PLACEHOLDER == openai_compat.NO_AUTH_PLACEHOLDER
    tier_fields = set(llm_client.Tier.__dataclass_fields__)
    assert tier_fields <= set(ResolvedModel.__dataclass_fields__) | {"name"}
