"""Row 7.11: the receipt-inbox classifier runs through the model gateway.

``ClaudeInboxClassifier`` held the label-only contract with one hard-coded
provider (the Agent SDK, its own turn bound, its own buffer). It becomes
:class:`GatewayInboxClassifier`, and which model answers is
``[llm.tiers]`` plus ``[llm.jobs].inbox_classify``.

What moves is the transport. What does NOT move is the guardrail: the
owner's 2026-08-12 decision (a non-receipt's label is the bare boolean plus
a confidence, never content) lives in :func:`sanitize`, in code, and it is
applied to every gateway reply.

What this eval pins:

- one gateway call per image, recorded in ``llm_calls`` (row 7.9's
  telemetry), including on the fixture adapter, so the row count is the
  proof a call happened and the proof it happened ONCE;
- the LABEL-ONLY invariant against a hostile reply: a model that ignores
  the prompt and returns vendor, amount, date, and a prose description for
  a personal photo leaves none of it in the ledger, the event log, the
  cards, or the run output;
- the same invariant when the hostile reply does not even VALIDATE, which
  is the sharper case: the validation error quotes the model's own words,
  so the ``llm_calls`` detail is withheld for this job and the classifier
  returns the safe label instead of letting the error escape;
- ``--param classifier=`` keeps ``fixture`` and ``claude`` as aliases for
  one release, plus the new ``tier:<name>``, and an unknown value fails
  loudly naming what is legal;
- the inbox run key changes when the resolved tier changes.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from core.agents.expenses.inbox import (
    INBOX_CLASSIFY_JOB,
    FixtureInboxClassifier,
    GatewayInboxClassifier,
    InboxLabel,
    build_classifier,
    resolved_tier,
    sanitize,
)
from core.agents.expenses.jobs import JOBS
from core.engine.config import TenantConfig
from core.engine.contracts import JobContext
from core.engine.runner import resolve_ledger_root, run
from core.ledger import Ledger
from core.llm import policy
from core.llm.adapters.fixture import FixtureAdapter

PERSON = "Pat Owner"

RECEIPT_REPLY = json.dumps(
    {
        "receipt": True,
        "confidence": 0.93,
        "vendor": "Roadside Coffee",
        "amount": "12.34",
        "expense_date": "2026-08-20",
    }
)

# A model that ignores the label-only prompt: it says "not a receipt" and
# then describes the photo anyway, with a name, an amount, a date, and an
# extra field the schema never asked for.
LEAK = "SIESTA KEY FAMILY BEACH DAY"
HOSTILE_REPLY = json.dumps(
    {
        "receipt": False,
        "confidence": 0.31,
        "vendor": LEAK,
        "amount": "999.99",
        "expense_date": "2026-01-01",
        "description": f"a personal photo: {LEAK}, four people on the sand",
    }
)

# The same misbehaviour one step worse: the reply does not validate, and the
# validation error quotes what the model said.
HOSTILE_INVALID_REPLY = json.dumps({"receipt": f"no, this is {LEAK}", "confidence": 0.4})


def _tenant_data(*, seat_model: str = "seat-model", with_local: bool = True) -> dict:
    """A tenant shaped like the live one: a flat-rate seat tier serving the
    inbox classifier, an opt-in local tier. Both run the fixture adapter, so
    the eval needs no network."""
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
        "identity": {"legal_name": "Inbox Test Co", "slug": "demo"},
        "llm": {
            "tiers": tiers,
            "jobs": {"inbox_classify": "seat", "scan_group": "seat"},
            "budget": {"monthly_usd": "25"},
        },
    }


def _ctx(tmp_path: Path, data: dict | None = None, **params) -> JobContext:
    tenant = TenantConfig.model_validate(data or _tenant_data())
    ledger = Ledger.open(tmp_path / "ledger")
    return JobContext(
        tenant=tenant,
        tenant_slug="demo",
        ledger=ledger,
        agent="expenses",
        job="inbox",
        params=params,
        run_key="demo.expenses.inbox.k1",
    )


def _photo(tmp_path: Path, name: str = "IMG_0001.jpg") -> Path:
    path = tmp_path / name
    path.write_bytes(b"\xff\xd8\xff\xd9")
    return path


def _rows(ctx: JobContext) -> list[dict]:
    return [
        dict(r) for r in ctx.ledger.conn.execute("SELECT * FROM llm_calls ORDER BY id").fetchall()
    ]


# ---- one call, one row, one sanitized label ----------------------------------


def test_classification_goes_through_the_policy_and_records_one_llm_call(tmp_path):
    ctx = _ctx(tmp_path)
    adapter = FixtureAdapter({"*": RECEIPT_REPLY})

    label = GatewayInboxClassifier(ctx, adapter=adapter).classify(_photo(tmp_path))

    assert isinstance(label, InboxLabel)
    assert label.receipt is True
    assert label.vendor == "Roadside Coffee"
    assert label.amount == "12.34"
    rows = _rows(ctx)
    assert len(rows) == 1, "one gateway call, one llm_calls row (row 7.9's telemetry)"
    assert rows[0]["job_type"] == INBOX_CLASSIFY_JOB
    assert rows[0]["tier"] == "seat"
    assert rows[0]["adapter"] == "fixture"
    assert rows[0]["model"] == "seat-model"
    assert rows[0]["status"] == "ok"


def test_the_photo_rides_as_an_attachment_and_the_prompt_carries_the_guardrail(tmp_path):
    ctx = _ctx(tmp_path)
    adapter = FixtureAdapter({"*": RECEIPT_REPLY})
    photo = _photo(tmp_path)

    GatewayInboxClassifier(ctx, adapter=adapter).classify(photo)

    bundle = adapter.calls[0]
    assert bundle.job_type == INBOX_CLASSIFY_JOB
    assert bundle.model == "seat-model"
    assert [(a.path, a.mime) for a in bundle.attachments] == [(photo, "image/jpeg")]
    system = bundle.system_text().lower()
    assert "do not describe" in system and "transcribe" in system


# ---- the label-only invariant against a hostile reply -------------------------


def test_a_hostile_reply_is_stripped_to_the_boolean_and_the_confidence(tmp_path):
    """The owner's 2026-08-12 guardrail, now against the GATEWAY: a model
    that answers "not a receipt" and then describes the photo anyway leaves
    the classifier with the boolean and the confidence, nothing else."""
    ctx = _ctx(tmp_path)

    label = GatewayInboxClassifier(ctx, adapter=FixtureAdapter({"*": HOSTILE_REPLY})).classify(
        _photo(tmp_path)
    )

    assert label.receipt is False
    assert label.confidence == 0.31
    assert label.vendor == "" and label.amount == "" and label.expense_date == ""
    assert LEAK not in label.model_dump_json()


def test_a_reply_that_fails_validation_returns_the_safe_label_and_records_no_content(tmp_path):
    """The sharper case: the reply does not validate, and pydantic's error
    quotes the model's own words. The classifier answers "not a receipt"
    (the pre-gateway behaviour for an unparseable reply) and the telemetry
    row records THAT the call failed, never WHAT it said."""
    ctx = _ctx(tmp_path)
    adapter = FixtureAdapter([HOSTILE_INVALID_REPLY, HOSTILE_INVALID_REPLY])

    label = GatewayInboxClassifier(ctx, adapter=adapter).classify(_photo(tmp_path))

    assert label.receipt is False and label.confidence == 0.0
    (row,) = _rows(ctx)
    assert row["status"] == "validation_failed"
    assert LEAK not in json.dumps(dict(row))


# ---- the same invariant end to end, through a real run ------------------------


def _params(tmp_path, **extra):
    return {
        "inbox_dir": str(tmp_path / "inbox"),
        "inbox_person": PERSON,
        "drop_dir": str(tmp_path / "drop"),
        "filing_dir": str(tmp_path / "filing"),
        "classifier": "claude",
        "extractor": "fixture",
        **extra,
    }


def _seed_photo(tmp_path, name: str, body: bytes) -> str:
    inbox = tmp_path / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / name).write_bytes(body)
    return hashlib.sha256(body).hexdigest()


def _run_inbox(tmp_path, monkeypatch, reply: str, **extra):
    """The demo tenant with a SEEDED adapter standing in for the model: the
    policy still resolves the tier, prices it, and records the call."""
    adapter = FixtureAdapter({"*": reply})
    monkeypatch.setattr(policy, "build_adapter", lambda resolved: adapter)
    result = run(
        "demo",
        "expenses",
        "inbox",
        params=_params(tmp_path, **extra),
        ledger_dir=tmp_path / "ledger",
    )
    return result, adapter


def _ledger_bytes(tmp_path) -> bytes:
    """Every byte the ledger holds: the sqlite file, the event log, the
    working tree. The planted string must appear in none of it."""
    root = resolve_ledger_root("demo", tmp_path / "ledger")
    blob = b""
    for path in sorted(root.rglob("*")):
        if path.is_file():
            blob += path.read_bytes()
    return blob


def _cards(tmp_path, action_type):
    root = resolve_ledger_root("demo", tmp_path / "ledger")
    with Ledger.open(root) as ledger:
        return [
            c
            for c in ledger.list_approvals("demo", status="pending")
            if c["action_type"] == action_type
        ]


def _events(tmp_path, event_type):
    root = resolve_ledger_root("demo", tmp_path / "ledger")
    with Ledger.open(root) as ledger:
        return [e for e in ledger.read_event_log() if e["event_type"] == event_type]


def test_a_hostile_reply_leaves_no_content_in_the_ledger_cards_or_run_output(tmp_path, monkeypatch):
    _seed_photo(tmp_path, "IMG_2002.jpg", b"beach-photo")

    result, adapter = _run_inbox(tmp_path, monkeypatch, HOSTILE_REPLY)

    (card,) = _cards(tmp_path, "expenses.inbox_skip")
    assert card["params"]["files"] == "IMG_2002.jpg"
    (event,) = _events(tmp_path, "expense.inbox_classified")
    assert "vendor" not in event["payload"]
    assert LEAK not in json.dumps(card["params"])
    assert LEAK not in json.dumps(event["payload"])
    assert LEAK not in result.model_dump_json()
    assert LEAK.encode() not in _ledger_bytes(tmp_path), "the ledger holds no photo content"
    assert len(adapter.calls) == 1, "one image, one model look"


def test_a_hostile_invalid_reply_leaves_no_content_either(tmp_path, monkeypatch):
    """The validation-error path end to end: the model's words reach neither
    the run output (the classifier never lets the error escape) nor the
    ``llm_calls`` detail (withheld for this job)."""
    _seed_photo(tmp_path, "IMG_2003.jpg", b"another-beach-photo")

    result, _ = _run_inbox(tmp_path, monkeypatch, HOSTILE_INVALID_REPLY)

    assert result.status in {"ok", "needs_approval"}
    assert LEAK not in result.model_dump_json()
    assert LEAK.encode() not in _ledger_bytes(tmp_path)


def test_one_gateway_call_per_image_and_never_a_second_look(tmp_path, monkeypatch):
    """Classification is event memory: the second run reads the event, and
    the model is never asked about the same image twice."""
    _seed_photo(tmp_path, "IMG_2004.jpg", b"receipt-photo")

    _run_inbox(tmp_path, monkeypatch, RECEIPT_REPLY)
    _, second = _run_inbox(tmp_path, monkeypatch, RECEIPT_REPLY)

    assert second.calls == [], "the second run classifies nothing"
    root = resolve_ledger_root("demo", tmp_path / "ledger")
    with Ledger.open(root) as ledger:
        rows = ledger.conn.execute("SELECT job_type FROM llm_calls").fetchall()
    assert [r["job_type"] for r in rows] == [INBOX_CLASSIFY_JOB]


# ---- the aliases -------------------------------------------------------------


def test_the_claude_alias_maps_to_the_tier_the_policy_names_for_the_job(tmp_path):
    ctx = _ctx(tmp_path)
    assert resolved_tier(ctx, "claude") == ("seat", "fixture", "seat-model")
    assert isinstance(build_classifier("claude", ctx), GatewayInboxClassifier)


def test_a_tier_can_be_named_directly(tmp_path):
    ctx = _ctx(tmp_path)
    assert resolved_tier(ctx, "tier:local") == ("local", "fixture", "local-coder")
    assert isinstance(build_classifier("tier:local", ctx), GatewayInboxClassifier)


def test_the_fixture_alias_needs_no_tenant_policy_at_all(tmp_path):
    assert isinstance(build_classifier("fixture"), FixtureInboxClassifier)
    assert resolved_tier(_ctx(tmp_path), "fixture") is None


def test_an_unknown_classifier_fails_loudly_with_the_list(tmp_path):
    ctx = _ctx(tmp_path)
    with pytest.raises(ValueError) as caught:
        build_classifier("gpt5", ctx)
    message = str(caught.value)
    for legal in ("fixture", "claude", "tier:"):
        assert legal in message


def test_a_tier_that_does_not_exist_fails_loudly_naming_the_tenant_tiers(tmp_path):
    ctx = _ctx(tmp_path)
    with pytest.raises(ValueError) as caught:
        build_classifier("tier:ghost", ctx)
    message = str(caught.value)
    assert "ghost" in message
    assert "seat" in message and "local" in message


def test_a_live_classifier_without_a_context_says_so(tmp_path):
    with pytest.raises(ValueError, match="tenant"):
        build_classifier("claude")


# ---- the sanitizer is still the boundary, whatever the transport --------------


def test_sanitize_still_strips_a_non_receipt_label():
    dirty = InboxLabel(
        receipt=False, confidence=0.5, vendor=LEAK, amount="1.00", expense_date="2026-01-01"
    )
    clean = sanitize(dirty)
    assert clean.receipt is False and clean.confidence == 0.5
    assert clean.vendor == "" and clean.amount == "" and clean.expense_date == ""


# ---- the run key -------------------------------------------------------------


def _inbox_key(tmp_path: Path, data: dict) -> str:
    inbox = tmp_path / "inbox"
    inbox.mkdir(exist_ok=True)
    (inbox / "IMG_9001.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    ctx = _ctx(
        tmp_path,
        data,
        inbox_dir=str(inbox),
        inbox_person=PERSON,
        drop_dir=str(tmp_path / "drop"),
        classifier="claude",
    )
    return JOBS["inbox"].key(ctx)


def test_the_inbox_run_key_changes_when_the_resolved_tier_changes(tmp_path):
    same = _inbox_key(tmp_path, _tenant_data())
    assert same == _inbox_key(tmp_path, _tenant_data()), "same policy, same key, no work"
    assert _inbox_key(tmp_path, _tenant_data(seat_model="a-different-model")) != same


def test_the_inbox_run_key_changes_when_the_job_is_pointed_at_another_tier(tmp_path):
    same = _inbox_key(tmp_path, _tenant_data())
    data = _tenant_data()
    data["llm"]["jobs"]["inbox_classify"] = "local"
    assert _inbox_key(tmp_path, data) != same
