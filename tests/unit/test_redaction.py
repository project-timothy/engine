"""Token redaction on the way into the ledger (phase 7 row 7.22).

The W-9 lane's invariant is the model: a TIN is read in memory and never
reaches a card, an event, or a log. Row 7.22 gives the container a place to
keep provider keys, so the same discipline now has to cover TOKENS. A secret
that arrives inside a document, an adapter's error body, or a job's own
summary must not become a permanent line in a git-backed ledger, where it
outlives every rotation.

One redactor does it (``core.redact``, the module ``core.llm.transcript``
already used), applied at one place: the runner, to the ``JobOutput`` a job
hands back, before anything is stored. Four rules, in order:

1. the VALUE of every environment variable the tenant declares as a secret;
2. named token families (``sk-``, GitHub, Slack, AWS, Google, JWT, bearer);
3. TIN shapes (unchanged, the W-9 rule);
4. a whole string that is one padded base64 blob.

What it must NEVER touch is the engine's own identifiers. sha16 content
keys, sha256 digests, ``stmt:`` statement-line ids and run keys are hex, and
hex is never redacted at any length: redacting one would break a card's
memory, a file's provenance, or a run key. Event and approval KEYS are not
passed through the redactor at all, so idempotency cannot move.
"""

from __future__ import annotations

import json

import pytest

from core.engine import runner as runner_mod
from core.engine.contracts import ApprovalSpec, EventSpec, JobHandler, JobOutput
from core.engine.runner import resolve_ledger_root, run
from core.ledger import Ledger
from core.ledger.event_log import read_event_lines
from core.redact import redact, redact_text

# A key-shaped string that is not a real key.
FAKE_KEY = "sk-ant-api03-ZZZfakeZZZ0000000000000000000000000000000000"

# Identifiers the engine writes into events and cards every day. Not one of
# them may move. The long ones are here on purpose: an earlier draft of this
# row scored them for entropy and they landed ON TOP of random 32-character
# tokens (4.4 to 4.9 bits either way), which is why no entropy rule shipped.
BENIGN = [
    "b2c3d4e5f6a7b8c9",  # a sha16 content key
    "9f2b1c4d5e6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2c",  # sha256
    "stmt:1a2b3c4d5e6f7a8b",  # a statement-line id
    "ap/reconcile:5f6a7b8c9d0e1f2a",  # a namespaced run key
    "Invoice_2026-09-16_Acme_Fabrication_PO",
    "PurchaseOrder260903ABCAcmeFabrication",
    "COGS-Project_Expense-PN00_0412_Subaccount",
    "TimesheetWeekEnding20260710_hours_only",
    "Assembly_and_Inspection_Guide_Rev1_FINAL",
]


# ---- the redactor's own rules --------------------------------------------------


def test_a_declared_secrets_value_is_replaced_by_its_variable_name():
    """Rule 1, and the only airtight one: the tenant NAMES the variable, so
    the engine knows the exact string that must never land."""
    out = redact_text("posting with abc123secret", [("ACME_QBO_CLIENT_SECRET", "abc123secret")])
    assert "abc123secret" not in out
    assert "<redacted:ACME_QBO_CLIENT_SECRET>" in out


def test_the_longest_secret_value_wins_when_one_contains_another():
    values = [("LONG", "abcdefgh"), ("SHORT", "abcd")]
    out = redact_text("x abcdefgh y", values)
    assert out == "x <redacted:LONG> y"


@pytest.mark.parametrize(
    "planted",
    [
        FAKE_KEY,
        "sk-ZZZZZZZZZZZZZZZZZZZZ",
        "ghp_ZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ",
        "github_pat_ZZZZZZZZZZZZZZZZZZ_ZZZZZZZZZZZZ",
        "xoxb-0000000000-0000000000-ZZZZZZZZZZZZZZZZZZZZZZZZ",
        "AKIAZZZZZZZZZZZZZZZZ",
        "AIzaZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.ZZZZZZZZZZZZZZZZ",
        "Bearer ZZZZZZZZZZZZZZZZZZZZ",
        "bearer zzzzzzzzzzzzzzzzzzzz",
        "Basic ZZZZZZZZZZZZZZZZZZZZ",
    ],
)
def test_a_named_token_family_is_redacted_anywhere_in_prose(planted):
    """Rule 2. These are the shapes that arrive from somewhere else: an
    adapter's 401 body, a receipt's footer, a model's echo of its own prompt."""
    out = redact_text(f"the call failed with {planted} in the body")
    assert planted.split()[-1] not in out
    assert "<redacted:key>" in out


def test_a_tin_shape_is_still_redacted():
    """Rule 3, the W-9 rule this row extends and must not weaken."""
    assert redact_text("EIN 12-3456789 on the form") == "EIN <redacted:tin> on the form"
    assert redact_text("SSN 123-45-6789") == "SSN <redacted:tin>"


def test_a_whole_string_that_is_one_padded_base64_blob_is_redacted():
    """Rule 4, deliberately the narrowest generic rule that exists: base64
    padding and ``+`` are characters no identifier this engine writes ever
    contains, so a field whose ENTIRE value is such a blob is a secret or
    nothing."""
    blob = "YWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXoxMjM0NTY3ODkw+w=="
    assert redact_text(blob) == "<redacted:token>"


def test_the_blob_rule_fires_on_a_field_value_and_never_inside_a_sentence():
    """Deliberate and stated out loud: the blob rule reads a WHOLE field
    value, never a substring of prose. A sentence is not a token, and
    scanning prose for opaque runs is what turns a redactor into a shredder
    (the named families in rule 2 are what cover prose)."""
    blob = "YWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXoxMjM0NTY3ODkw+w=="
    assert blob in redact_text(f"the body was {blob} which failed")


@pytest.mark.parametrize("identifier", BENIGN)
def test_the_engines_own_identifiers_are_never_redacted(identifier):
    """The rule that matters most in practice. A redactor that ate a sha16
    card key would make a card re-park every night; one that ate a sha256
    would break file provenance; one that ate a run key would replay a run."""
    assert redact_text(identifier) == identifier
    assert redact(identifier) == identifier


def test_redaction_is_a_fixed_point():
    """Cheap and deterministic: running it twice changes nothing, so a
    replayed or re-recorded payload is stable."""
    once = redact_text(f"key {FAKE_KEY} and EIN 12-3456789")
    assert redact_text(once) == once


def test_redact_walks_nested_payloads_and_leaves_non_strings_alone():
    payload = {"a": [{"b": FAKE_KEY}], "n": 3, "f": None, "t": True}
    out = redact(payload)
    assert out["a"][0]["b"] == "<redacted:key>"
    assert out["n"] == 3 and out["f"] is None and out["t"] is True


# ---- the runner applies it once, to everything a job hands back ------------------

PLANTING = JobHandler(
    key=lambda ctx: "planting",
    run=lambda ctx: JobOutput(
        status="ok",
        summary=f"recorded the vendor key {FAKE_KEY}",
        actions=[f"stored {FAKE_KEY}"],
        events=[
            EventSpec(
                key="b2c3d4e5f6a7b8c9",
                event_type="demo.thing",
                payload={
                    "token": FAKE_KEY,
                    "sha256": BENIGN[1],
                    "statement_id": BENIGN[2],
                    "count": 2,
                },
            )
        ],
        approvals=[
            ApprovalSpec(
                key="b2c3d4e5f6a7b8c9",
                action_type="demo.decide",
                params={"auth": FAKE_KEY, "file": BENIGN[4]},
                reason=f"the vendor sent {FAKE_KEY} in the email",
            )
        ],
        anomalies=[
            runner_mod.Anomaly(code="demo.noticed", detail=f"upstream said {FAKE_KEY}"),
        ],
    ),
)


@pytest.fixture
def planted(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_mod, "get_job", lambda agent, job: PLANTING)
    result = run("demo", "demo", "planting", ledger_dir=tmp_path)
    root = resolve_ledger_root("demo", tmp_path)
    return result, root


def test_a_planted_key_is_redacted_in_the_card_summary_and_params(planted):
    result, root = planted
    card = result.approvals_needed[0]
    assert FAKE_KEY not in card.reason
    assert FAKE_KEY not in json.dumps(card.params)
    assert card.params["file"] == BENIGN[4], "the card's other params are untouched"
    with Ledger.open(root) as ledger:
        row = ledger.conn.execute("SELECT params_json FROM approval_queue").fetchone()
    assert FAKE_KEY not in row["params_json"]


def test_a_planted_key_is_redacted_in_an_anomaly_detail_and_the_summary(planted):
    result, _ = planted
    assert FAKE_KEY not in result.anomalies[0].detail
    assert "<redacted:key>" in result.anomalies[0].detail
    assert FAKE_KEY not in result.summary
    assert FAKE_KEY not in " ".join(result.actions)


def test_a_planted_key_is_redacted_in_an_event_payload(planted):
    _, root = planted
    with Ledger.open(root) as ledger:
        row = ledger.conn.execute(
            "SELECT payload_json FROM events WHERE event_type = 'demo.thing'"
        ).fetchone()
    payload = json.loads(row["payload_json"])
    assert payload["token"] == "<redacted:key>"
    assert payload["sha256"] == BENIGN[1], "a file digest is provenance, not a token"
    assert payload["statement_id"] == BENIGN[2]
    assert payload["count"] == 2


def test_nothing_secret_reaches_the_append_only_event_log(planted):
    """The JSONL file is the git-diffable copy: it is pushed to a backup
    remote every night, so a token there is a token in a second repository."""
    _, root = planted
    text = json.dumps(read_event_lines(root))
    assert FAKE_KEY not in text
    assert "<redacted:key>" in text


def test_the_stored_result_carries_nothing_secret(planted):
    _, root = planted
    with Ledger.open(root) as ledger:
        row = ledger.conn.execute("SELECT result_json, summary FROM runs").fetchone()
    assert FAKE_KEY not in row["result_json"]
    assert FAKE_KEY not in row["summary"]


def test_idempotency_keys_are_never_passed_through_the_redactor(planted):
    """Event and approval keys are content fingerprints: a redactor that
    touched one would re-park a decided card or re-record an event."""
    _, root = planted
    with Ledger.open(root) as ledger:
        event = ledger.conn.execute(
            "SELECT idempotency_key FROM events WHERE event_type = 'demo.thing'"
        ).fetchone()
        card = ledger.conn.execute("SELECT idempotency_key FROM approval_queue").fetchone()
    assert event["idempotency_key"].endswith(":evt:b2c3d4e5f6a7b8c9")
    assert card["idempotency_key"].endswith(":b2c3d4e5f6a7b8c9")


def test_a_declared_secrets_value_is_redacted_from_an_event_by_name(tmp_path, monkeypatch):
    """The tenant names the variable; the runner reads the value once and
    replaces it wherever the job put it. This is the rule that catches a key
    with no recognisable shape at all."""
    monkeypatch.setenv("DEMO_QBO_CLIENT_SECRET", "plain-looking-value-42")
    handler = JobHandler(
        key=lambda ctx: "leak",
        run=lambda ctx: JobOutput(
            status="ok",
            summary="leaked",
            events=[
                EventSpec(key="k", event_type="demo.leak", payload={"s": "plain-looking-value-42"})
            ],
        ),
    )
    monkeypatch.setattr(runner_mod, "get_job", lambda agent, job: handler)
    run("demo", "demo", "leak", ledger_dir=tmp_path)
    root = resolve_ledger_root("demo", tmp_path)
    with Ledger.open(root) as ledger:
        row = ledger.conn.execute("SELECT payload_json FROM events").fetchone()
    assert json.loads(row["payload_json"])["s"] == "<redacted:DEMO_QBO_CLIENT_SECRET>"


def test_a_raised_exception_carrying_a_token_lands_redacted(tmp_path, monkeypatch):
    """An adapter's 401 becomes a job exception, and the failure trace (#172)
    is durable. The token in it must not be."""

    def boom(ctx):
        raise RuntimeError(f"401 from the provider: {FAKE_KEY}")

    monkeypatch.setattr(
        runner_mod, "get_job", lambda a, j: JobHandler(key=lambda ctx: "boom", run=boom)
    )
    result = run("demo", "demo", "boom", ledger_dir=tmp_path)
    assert result.status == "error"
    assert FAKE_KEY not in result.summary
    assert FAKE_KEY not in " ".join(a.detail for a in result.anomalies)


def test_the_transcript_redactor_is_the_same_one(tmp_path):
    """One redactor, not two: ``core.llm.transcript`` keeps its names and
    imports them from ``core.redact``."""
    from core.llm import transcript

    assert transcript.redact_text is redact_text
    assert transcript.redact is redact
