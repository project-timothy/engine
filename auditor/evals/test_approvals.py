"""Approval-hygiene lens evals: the queue is a checkpoint, not a parking lot."""

from __future__ import annotations

import base64
import hashlib
import json
import struct

import pytest

from auditor.lenses import approvals

from .fixtures import add_approval, add_event, add_invoice, make_context, make_ledger

FRESH = "2026-07-19T06:00:00+00:00"  # 2 days before fixture NOW
OLD = "2026-07-11T06:00:00+00:00"  # 10 days before fixture NOW
ANCIENT = "2026-06-20T06:00:00+00:00"  # 31 days before fixture NOW


def _conditions(tmp_path):
    ctx = make_context(tmp_path)
    with ctx.ledger:
        return sorted(f.condition for f in approvals.check(ctx))


def _findings(tmp_path):
    ctx = make_context(tmp_path)
    with ctx.ledger:
        return approvals.check(ctx)


def test_young_pending_card_is_quiet(tmp_path):
    conn = make_ledger(tmp_path)
    add_approval(conn, action_type="ap.payment_recommendation", created_at=FRESH)
    assert _conditions(tmp_path) == []


def test_stale_pending_card_is_a_finding(tmp_path):
    conn = make_ledger(tmp_path)
    add_approval(conn, action_type="ap.payment_recommendation", created_at=OLD)
    assert _conditions(tmp_path) == ["stale-pending"]


def test_fully_executed_batch_is_quiet(tmp_path):
    conn = make_ledger(tmp_path)
    a = add_invoice(conn, invoice_number="A", qbo_bill_id="77")
    b = add_invoice(conn, invoice_number="B", qbo_bill_id="78")
    add_approval(
        conn,
        params={"row_ids": f"{a},{b}"},
        status="approved",
        created_at=FRESH,
        resolved_at=FRESH,
    )
    assert _conditions(tmp_path) == []


def test_approved_batch_with_an_unexecuted_row_is_a_finding(tmp_path):
    conn = make_ledger(tmp_path)
    done = add_invoice(conn, invoice_number="A", qbo_bill_id="77")
    ghost = add_invoice(conn, invoice_number="B")  # approved, never written
    add_approval(
        conn,
        params={"row_ids": f"{done},{ghost}"},
        status="approved",
        created_at=FRESH,
        resolved_at=FRESH,
    )
    assert _conditions(tmp_path) == ["approved-not-executed"]


def test_row_parked_behind_a_mapping_card_is_accounted_for(tmp_path):
    conn = make_ledger(tmp_path)
    parked = add_invoice(conn, invoice_number="B")
    add_approval(
        conn,
        params={"row_ids": str(parked)},
        status="approved",
        created_at=FRESH,
        resolved_at=FRESH,
    )
    add_approval(
        conn,
        action_type="ap.qbo_map_vendor",
        params={"row_id": str(parked), "vendor": "V"},
        status="pending",
        created_at=FRESH,
    )
    assert _conditions(tmp_path) == []


def test_settled_row_counts_as_consumed(tmp_path):
    # Live false positive, 2026-07-20: an annual premium the owner paid in
    # full on the card, with an explicit no-bill note — the row settled to
    # Paid after the batch approval, closing the AP question by another
    # route. Whether the accounting system agrees is lens 7's question.
    conn = make_ledger(tmp_path)
    settled = add_invoice(conn, invoice_number="IUJ", status="Paid")
    add_approval(
        conn,
        params={"row_ids": str(settled)},
        status="approved",
        created_at=FRESH,
        resolved_at=FRESH,
    )
    assert _conditions(tmp_path) == []


def test_rejected_batches_carry_no_obligation(tmp_path):
    conn = make_ledger(tmp_path)
    ghost = add_invoice(conn, invoice_number="B")
    add_approval(
        conn,
        params={"row_ids": str(ghost)},
        status="rejected",
        created_at=FRESH,
        resolved_at=FRESH,
    )
    assert _conditions(tmp_path) == []


# ---- the ask the queue dropped ----------------------------------------------
#
# The engine records a swallowed ask twice over: an ``engine.approval_swallowed``
# event and an anomaly on the run (tests/unit/test_runner_approval_swallow.py
# pins both, "so the ledger records the truth ... instead of invisible"). No
# lens has ever read either one, so the record reached nobody: a drop is
# visible in the ledger and silent on the report, which is the same place the
# owner looks. These evals are that delivery surface.


def _swallow(
    conn,
    *,
    key="subject:1",
    action_type="demo.park",
    existing_id=7,
    existing_status="rejected",
    created_at=FRESH,
):
    add_event(
        conn,
        event_type="engine.approval_swallowed",
        created_at=created_at,
        payload={
            "action_type": action_type,
            "key": key,
            "existing_id": existing_id,
            "existing_status": existing_status,
        },
    )


def test_a_dropped_ask_is_a_finding(tmp_path):
    conn = make_ledger(tmp_path)
    _swallow(conn)
    assert _conditions(tmp_path) == ["swallowed"]


def test_the_finding_names_the_ask_and_the_card_that_blocked_it(tmp_path):
    """The subject has to identify the ask (so one drop is one checklist item
    the owner can answer or mute), and the detail has to name the resolved
    card that absorbed it — without that id nobody can tell whether the drop
    was a stale rejection or a decision that still stands."""
    conn = make_ledger(tmp_path)
    _swallow(conn, key="inbox:abc123", action_type="demo.file_thing", existing_id=81)
    (finding,) = _findings(tmp_path)
    assert finding.severity == "WARN"
    assert "demo.file_thing" in finding.subject
    assert "inbox:abc123" in finding.subject
    assert "#81" in finding.detail
    assert "rejected" in finding.detail


def test_one_ask_dropped_many_times_is_one_finding_that_counts_them(tmp_path):
    """The lane re-asks whenever its input re-keys, and every re-ask hits the
    same resolved card. Three drops of one ask are one open question, not
    three, and the count is what tells the owner it keeps happening."""
    conn = make_ledger(tmp_path)
    for when in (OLD, "2026-07-15T06:00:00+00:00", FRESH):
        _swallow(conn, created_at=when)
    (finding,) = _findings(tmp_path)
    assert "3 times" in finding.detail
    assert "2026-07-11" in finding.detail  # first drop
    assert "2026-07-19" in finding.detail  # latest drop


def test_two_different_asks_are_two_findings(tmp_path):
    conn = make_ledger(tmp_path)
    _swallow(conn, key="subject:1")
    _swallow(conn, key="subject:2")
    assert [f.condition for f in _findings(tmp_path)] == ["swallowed", "swallowed"]


def test_a_drop_older_than_the_lookback_ages_out(tmp_path):
    """A drop is instantaneous, so it can never "resolve" on its own. Aging it
    out of the report is what lets the checklist reconciliation close the item
    once the lane stops re-asking — the rule the heartbeat lens applies to
    preflight markers."""
    conn = make_ledger(tmp_path)
    _swallow(conn, created_at=ANCIENT)
    assert _conditions(tmp_path) == []


def test_an_old_drop_that_happened_again_recently_still_reports(tmp_path):
    conn = make_ledger(tmp_path)
    _swallow(conn, created_at=ANCIENT)
    _swallow(conn, created_at=FRESH)
    assert _conditions(tmp_path) == ["swallowed"]


def test_an_unparseable_payload_still_reports_the_drop(tmp_path):
    """Never let a malformed payload turn a dropped ask back into silence."""
    conn = make_ledger(tmp_path)
    add_event(conn, event_type="engine.approval_swallowed", payload={}, created_at=FRESH)
    assert _conditions(tmp_path) == ["swallowed"]


def test_other_events_are_not_drops(tmp_path):
    conn = make_ledger(tmp_path)
    add_event(conn, event_type="ap.invoice.recorded", payload={"file": "x.pdf"}, created_at=FRESH)
    assert _conditions(tmp_path) == []


# -- human-only cards (#356 check 2) -----------------------------------------


def test_a_human_only_card_a_person_decided_is_quiet(tmp_path):
    conn = make_ledger(tmp_path)
    add_approval(
        conn,
        action_type="ap.new_vendor_decision",
        params={"human_only": "true", "decided_via": "terminal"},
        status="approved",
        created_at=FRESH,
        resolved_at=FRESH,
    )
    assert _conditions(tmp_path) == []


def test_a_human_only_card_decided_without_a_person_is_critical(tmp_path):
    conn = make_ledger(tmp_path)
    add_approval(
        conn,
        action_type="ap.new_vendor_decision",
        params={"human_only": "true", "extracted_vendor": "Look-Alike Co"},
        status="rejected",
        created_at=FRESH,
        resolved_at=FRESH,
    )
    (finding,) = _findings(tmp_path)
    assert finding.condition == "human-only-decided-by-agent"
    assert finding.severity == "CRITICAL"
    assert "Look-Alike Co" in finding.detail


def test_an_unstamped_card_and_a_pending_one_are_not_judged(tmp_path):
    conn = make_ledger(tmp_path)
    # decided before the stamp existed: no claim was made, so none is broken
    add_approval(
        conn,
        action_type="ap.new_vendor_decision",
        params={"extracted_vendor": "Old Co"},
        status="approved",
        created_at=FRESH,
        resolved_at=FRESH,
    )
    add_approval(
        conn,
        action_type="ap.new_vendor_decision",
        params={"human_only": "true"},
        created_at=FRESH,
    )
    assert _conditions(tmp_path) == []


# -- human-only cards decided by the door (#465) ------------------------------
# The owner's decision of 2026-10-09: a person's yes through the chat counts
# when Face ID on their own phone proves it, or the Face ID of the person
# the ministry named to vouch for them (the same weight). The auditor cannot
# check the signature math (that is the doorkeeper's, with a library the
# auditor does not take); it checks the tenant opened the door for this kind
# of card, the right person tapped, and the signed answer is about THIS
# decision: who, which card, which answer, the words they read.


DOOR_AUTHORITY = """
[money]
out = "human"

[people.carol-jennings]
roles = ["treasurer"]

[people.don-pruitt]
roles = ["treasurer"]

[presence]
door = ["vendor"]

[vouch]
carol-jennings = "don-pruitt"
"""


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _challenge(nonce: bytes, person: str, card: str, verb: str, summary: str) -> bytes:
    """The doorkeeper's decision challenge, written out again here so the
    format is pinned from the outside (doorkeeper/witness.py)."""
    h = hashlib.sha256(b"tim-witness-v1")
    for part in (nonce, person.encode(), card.encode(), verb.encode(), summary.encode()):
        h.update(struct.pack(">I", len(part)) + part)
    return h.digest()


def _evidence(card: str, person: str, verb: str = "approve", summary: str = "Approve it.") -> str:
    nonce = b"n" * 32
    client = {
        "type": "webauthn.get",
        "challenge": _b64u(_challenge(nonce, person, card, verb, summary)),
        "origin": "https://tim.example.org",
    }
    return json.dumps(
        {
            "nonce": _b64u(nonce),
            "credential_id": "cred",
            "public_key": "key",
            "client_data": _b64u(json.dumps(client).encode()),
            "authenticator_data": "auth",
            "signature": "sig",
            "summary": summary,
        }
    )


def _door_card(
    tmp_path,
    *,
    via="door",
    by="carol-jennings",
    vouched="",
    resource="vendor",
    status="approved",
    evidence_for=None,
    authority=DOOR_AUTHORITY,
    damage=None,
):
    tenants = tmp_path / "tenants"
    (tenants / "t").mkdir(parents=True, exist_ok=True)
    if authority is not None:
        (tenants / "t" / "authority.toml").write_text(authority)
    conn = make_ledger(tmp_path)
    params = {
        "human_only": "true",
        "extracted_vendor": "Marion Roofing",
        "decided_via": via,
        "decided_by": by,
        "witness_resource": resource,
        "witness_at": FRESH,
    }
    if vouched:
        params["vouched_by"] = vouched
    card = add_approval(
        conn,
        action_type="ap.new_vendor_decision",
        params=params,
        status=status,
        created_at=FRESH,
        resolved_at=FRESH,
    )
    tapper = vouched or by
    verb = "approve" if status == "approved" else "reject"
    witness = json.loads(_evidence(*(evidence_for or (str(card), tapper, verb))))
    if damage:
        damage(witness)
    params["witness"] = json.dumps(witness)
    conn.execute(
        "UPDATE approval_queue SET params_json = ? WHERE id = ?", (json.dumps(params), card)
    )
    conn.commit()
    ctx = make_context(tmp_path, tenants_dir=tenants)
    with ctx.ledger:
        return approvals.check(ctx)


def test_a_human_only_card_decided_by_the_persons_own_face_id_is_quiet(tmp_path):
    assert _door_card(tmp_path) == []


def test_a_vouched_yes_counts_the_same_as_face_id(tmp_path):
    assert _door_card(tmp_path, via="door-vouched", vouched="don-pruitt") == []


def test_a_door_no_is_judged_as_a_no(tmp_path):
    assert _door_card(tmp_path, status="rejected") == []


def _critical(findings) -> str:
    (finding,) = findings
    assert finding.condition == "human-only-decided-by-agent"
    assert finding.severity == "CRITICAL"
    return finding.detail


def test_a_tenant_that_never_opened_the_door_is_critical(tmp_path):
    no_door = DOOR_AUTHORITY.replace('door = ["vendor"]', "door = []")
    assert "door" in _critical(_door_card(tmp_path, authority=no_door))


def test_with_no_authority_file_a_door_decision_is_critical(tmp_path):
    assert _critical(_door_card(tmp_path, authority=None))


def test_a_door_opened_to_another_kind_of_card_is_critical(tmp_path):
    other = DOOR_AUTHORITY.replace('door = ["vendor"]', 'door = ["expense.report"]')
    assert _critical(_door_card(tmp_path, authority=other))


def test_a_vouch_the_ministry_never_named_is_critical(tmp_path):
    detail = _critical(
        _door_card(tmp_path, via="door-vouched", by="don-pruitt", vouched="carol-jennings")
    )
    assert "vouch" in detail


def test_someone_not_on_the_list_is_critical(tmp_path):
    assert _critical(_door_card(tmp_path, by="mallory"))


@pytest.mark.parametrize(
    "evidence_for",
    [
        ("999", "carol-jennings", "approve"),  # another card
        ("{card}", "don-pruitt", "approve"),  # another person's tap
        ("{card}", "carol-jennings", "reject"),  # another answer
    ],
)
def test_evidence_about_another_decision_is_critical(tmp_path, evidence_for):
    # the card id is 1 in a fresh fixture ledger with one card
    filled = tuple(x.replace("{card}", "1") for x in evidence_for)
    assert "evidence" in _critical(_door_card(tmp_path, evidence_for=filled))


def test_garbled_evidence_is_critical(tmp_path):
    tenants = tmp_path / "tenants"
    (tenants / "t").mkdir(parents=True)
    (tenants / "t" / "authority.toml").write_text(DOOR_AUTHORITY)
    conn = make_ledger(tmp_path)
    add_approval(
        conn,
        action_type="ap.new_vendor_decision",
        params={
            "human_only": "true",
            "decided_via": "door",
            "decided_by": "carol-jennings",
            "witness_resource": "vendor",
            "witness": "signed, honest",
        },
        status="approved",
        created_at=FRESH,
        resolved_at=FRESH,
    )
    ctx = make_context(tmp_path, tenants_dir=tenants)
    with ctx.ledger:
        assert _critical(approvals.check(ctx))


def _no_signature(ev: dict) -> None:
    ev["signature"] = ""


def _a_registration(ev: dict) -> None:
    client = json.loads(base64.urlsafe_b64decode(ev["client_data"] + "=="))
    client["type"] = "webauthn.create"
    ev["client_data"] = _b64u(json.dumps(client).encode())


@pytest.mark.parametrize("damage", [_no_signature, _a_registration])
def test_evidence_that_signs_nothing_is_critical(tmp_path, damage):
    assert "evidence" in _critical(_door_card(tmp_path, damage=damage))
