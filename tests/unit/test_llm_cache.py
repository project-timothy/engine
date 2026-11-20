"""The gateway caches the repeated start of a prompt when the caller asks
(Standalone Tim, shot 1, card V1).

Tim's own voice sends the same brief and the same tool list on every turn.
A provider that caches a prompt's opening bills those tokens at a fraction,
so ``complete(..., cache=True)`` marks the system text and the usage that
comes back is counted and priced by kind. A job that does not ask sends
exactly what it sent before.
"""

from __future__ import annotations

import json
import urllib.request
from decimal import Decimal

from pydantic import BaseModel

from core.engine.config import TenantConfig
from core.llm import Message, Pricing, Usage, complete
from core.llm.adapters.anthropic_messages import AnthropicMessagesAdapter
from core.llm.adapters.fixture import FixtureAdapter
from core.llm.policy import resolve


class Said(BaseModel):
    say: str


def _messages() -> list[Message]:
    return [Message("system", "You are Tim."), Message("user", "Hey Tim")]


class _Response:
    def __init__(self, body: dict) -> None:
        self._data = json.dumps(body).encode("utf-8")

    def read(self) -> bytes:
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


def _wire(monkeypatch, usage: dict) -> list:
    seen: list = []
    body = {
        "model": "provider-model",
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": '{"say": "Hi."}'}],
        "usage": usage,
    }

    def fake_urlopen(request, timeout=None):
        seen.append(json.loads(request.data))
        return _Response(body)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("TEST_TALK_KEY", "sk-test-not-a-real-key")
    return seen


def test_the_system_text_is_marked_for_caching_only_when_the_caller_asks(monkeypatch):
    seen = _wire(monkeypatch, {"input_tokens": 12, "output_tokens": 8})
    adapter = AnthropicMessagesAdapter(api_key_env="TEST_TALK_KEY")
    complete("talk", _messages(), Said, adapter=adapter, model="m", cache=True)
    complete("receipt_extract", _messages(), Said, adapter=adapter, model="m")

    asked, plain = seen
    assert isinstance(asked["system"], list) and len(asked["system"]) == 1
    block = asked["system"][0]
    assert block["type"] == "text" and block["text"].startswith("You are Tim.")
    assert block["cache_control"] == {"type": "ephemeral"}
    # Every other job's request is byte for byte what it was.
    assert isinstance(plain["system"], str) and plain["system"] == block["text"]


def test_cache_tokens_are_counted_and_priced_at_their_own_rates(monkeypatch):
    _wire(
        monkeypatch,
        {
            "input_tokens": 40,
            "output_tokens": 30,
            "cache_creation_input_tokens": 1000,
            "cache_read_input_tokens": 5000,
        },
    )
    pricing = Pricing(
        input_usd_per_mtok=Decimal("4"),
        output_usd_per_mtok=Decimal("20"),
        cache_write_usd_per_mtok=Decimal("5"),
        cache_read_usd_per_mtok=Decimal("0.20"),
    )
    adapter = AnthropicMessagesAdapter(api_key_env="TEST_TALK_KEY")
    record = complete(
        "talk", _messages(), Said, adapter=adapter, model="m", cache=True, pricing=pricing
    ).record
    # Every token that went in, whatever it cost.
    assert record.input_tokens == 6040 and record.output_tokens == 30
    assert record.cache_write_tokens == 1000 and record.cache_read_tokens == 5000
    # 40 at $4, 1000 at $5, 5000 at $0.20, 30 out at $20, per million.
    assert record.usd == Decimal("0.00676")


def test_cache_tokens_cost_the_input_price_when_a_tier_names_no_cache_price():
    """Never guess a discount: a tier that names no cache prices is charged
    the full input price for those tokens, so the cap errs toward early."""
    pricing = Pricing(input_usd_per_mtok=Decimal("4"), output_usd_per_mtok=Decimal("20"))
    usage = Usage(
        input_tokens=40, output_tokens=30, cache_write_tokens=1000, cache_read_tokens=5000
    )
    assert pricing.usd(usage) == Decimal("0.02476")


def test_a_tier_names_its_cache_prices_in_the_tenants_own_file():
    cfg = TenantConfig.model_validate(
        {
            "identity": {"legal_name": "Cache Test Ministry", "slug": "demo"},
            "llm": {
                "tiers": {
                    "everyday": {
                        "adapter": "fixture",
                        "model": "everyday-model",
                        "pricing": {
                            "input_usd_per_mtok": "0.10",
                            "output_usd_per_mtok": "0.50",
                            "cache_write_usd_per_mtok": "0.125",
                            "cache_read_usd_per_mtok": "0.01",
                        },
                    }
                },
                "jobs": {"talk": "everyday"},
            },
        }
    )
    pricing = resolve(cfg.llm, "talk").pricing
    assert pricing.cache_write_usd_per_mtok == Decimal("0.125")
    assert pricing.cache_read_usd_per_mtok == Decimal("0.01")


def test_the_fixture_adapter_sees_whether_caching_was_asked():
    model = FixtureAdapter(['{"say": "Hi."}', '{"say": "Hi."}'])
    complete("talk", _messages(), Said, adapter=model, model="m", cache=True)
    complete("talk", _messages(), Said, adapter=model, model="m")
    assert [bundle.cache for bundle in model.calls] == [True, False]
