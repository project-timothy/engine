"""Sender provenance at intake (issue #356, shadow stage).

The hole: intake binds an invoice to a vendor by the NAME in the PDF and
never consults the sender, so any mailbox whose PDF names a known vendor
files as that vendor. The 2026-10-06 calibration on the live ledger showed
the shape real mail takes: the vendor's own domain, an invoicing platform's
shared mailer, a forward from inside the business, and a free-mail address.
Each gets its own verdict; only a stranger's domain or an unlisted free-mail
address is a mismatch. Shadow: the verdict is an event, the invoice still
records exactly as before.
"""

from __future__ import annotations

import base64
import json
import shutil
from pathlib import Path

from core.agents.ap.provenance import MailOrigin, judge, mail_origins
from core.agents.ap.registry import VendorEntry, VendorRegistry
from core.engine.runner import resolve_ledger_root, run
from core.ledger import Ledger

LANDING = Path(__file__).resolve().parent / "fixtures" / "landing"

REGISTRY = VendorRegistry(
    entries={
        "alphaparts.example": VendorEntry(vendor="Alpha Parts"),
        "alphafab": VendorEntry(vendor="Alpha Fabrication"),
        "solo-welding": VendorEntry(vendor="Solo Welding", senders=["SoloWelds@gmail.com"]),
    }
)
POLICY = {
    "internal_domains": ["demotenant.example"],
    "platform_domains": ["notify.invoiceplatform.example"],
    "freemail_domains": ["gmail.com"],
}


def _judge(sender: str | None, vendor: str):
    origin = None
    if sender is not None:
        origin = MailOrigin(sender=sender, domain=sender.rsplit("@", 1)[-1])
    return judge(origin, vendor, REGISTRY, **POLICY)


# -- the verdict, one per shape of real mail ---------------------------------


def test_a_look_alike_domain_naming_a_known_vendor_is_a_mismatch():
    v = _judge("billing@alphaparts-billing.example", "Alpha Parts")
    assert (v.verdict, v.reason) == ("mismatch", "domain")


def test_the_vendors_own_domain_and_its_subdomains_match():
    assert _judge("ap@alphaparts.example", "Alpha Parts").verdict == "match"
    assert _judge("ap@mail.alphaparts.example", "Alpha Parts").verdict == "match"


def test_a_name_fragment_key_never_binds_a_domain_that_contains_it():
    # "alphafab" is a subject-matching key; a look-alike domain can contain it.
    v = _judge("pay@alphafab-remit.example", "Alpha Fabrication")
    assert (v.verdict, v.reason) == ("mismatch", "domain")


def test_no_mail_record_is_an_owner_drop():
    assert _judge(None, "Alpha Parts").verdict == "owner_drop"


def test_a_forward_from_inside_the_business_is_internal():
    assert _judge("owner@demotenant.example", "Alpha Parts").verdict == "internal_forward"


def test_a_platform_mailer_binds_nothing_and_says_so():
    v = _judge("quickpay@notify.invoiceplatform.example", "Alpha Parts")
    assert v.verdict == "platform"


def test_freemail_binds_only_an_exact_listed_address():
    assert _judge("solowelds@gmail.com", "Solo Welding").verdict == "match"
    v = _judge("solo.welds.billing@gmail.com", "Solo Welding")
    assert (v.verdict, v.reason) == ("mismatch", "freemail_unlisted")
    # a listed free-mail address speaks for ITS vendor only
    v = _judge("solowelds@gmail.com", "Alpha Parts")
    assert (v.verdict, v.reason) == ("mismatch", "freemail_unlisted")


def test_older_mail_records_join_by_the_hash_in_their_key():
    events = [
        {
            "event_type": "mail.attachment_saved",
            "idempotency_key": "mail.fetch.abc:evt:mailatt:" + "f" * 64,
            "payload": {"file": "inv.pdf", "sender_domain": "alphaparts.example"},
        }
    ]
    by_hash, by_name = mail_origins(events)
    assert by_hash["f" * 64].domain == "alphaparts.example"
    assert by_name["inv.pdf"].sender == ""


# -- end to end: mail fetch, then intake -------------------------------------


def _deliver(tmp_path: Path, sender: str, name: str, sidecar: str) -> Path:
    """Run the real mail fetch for one attachment, then give the landed file
    its extraction sidecar (the fixture extractor reads it)."""
    landing = tmp_path / "landing"
    body = (LANDING / sidecar).read_bytes() + sender.encode()  # unique content
    messages = tmp_path / "messages.json"
    messages.write_text(
        json.dumps(
            [
                {
                    "id": "m1",
                    "sender": sender,
                    "date": "2026-10-06T12:00:00Z",
                    "attachments": [
                        {"id": "a-0", "name": name, "content_b64": base64.b64encode(body).decode()}
                    ],
                }
            ]
        )
    )
    fetched = run(
        "demo",
        "mail",
        "fetch",
        params={"messages_file": str(messages), "landing_dir": str(landing)},
        ledger_dir=tmp_path / "data",
    )
    assert fetched.status == "ok", fetched.summary
    shutil.copy(LANDING / (sidecar + ".extract.json"), landing / (name + ".extract.json"))
    return landing


def _intake(tmp_path: Path, landing: Path):
    return run(
        "demo",
        "ap",
        "intake",
        shadow=True,
        params={"landing_dir": str(landing), "extractor": "fixture"},
        ledger_dir=tmp_path / "data",
    )


def _events(tmp_path: Path, event_type: str) -> list[dict]:
    with Ledger.open(resolve_ledger_root("demo", tmp_path / "data")) as ledger:
        return [e for e in ledger.read_event_log() if e["event_type"] == event_type]


def test_a_spoofed_sender_is_recorded_as_a_mismatch_and_nothing_else_changes(tmp_path):
    landing = _deliver(
        tmp_path, "billing@alphaparts-billing.example", "inv.pdf", "inv_alpha_1001.pdf"
    )
    result = _intake(tmp_path, landing)

    (prov,) = _events(tmp_path, "ap.provenance.recorded")
    assert prov["payload"]["verdict"] == "mismatch"
    assert prov["payload"]["reason"] == "domain"
    assert prov["payload"]["vendor"] == "Alpha Parts"
    assert prov["payload"]["sender"] == "billing@alphaparts-billing.example"
    # shadow stage: the invoice records exactly as it did before #356
    assert "NEW 1" in result.summary
    assert len(_events(tmp_path, "ap.invoice.recorded")) == 1


def test_the_vendors_own_mail_is_recorded_as_a_match(tmp_path):
    landing = _deliver(tmp_path, "AP <ap@alphaparts.example>", "inv.pdf", "inv_alpha_1001.pdf")
    _intake(tmp_path, landing)
    (prov,) = _events(tmp_path, "ap.provenance.recorded")
    assert prov["payload"]["verdict"] == "match"
    assert prov["payload"]["sender"] == "ap@alphaparts.example"


def test_a_file_nobody_mailed_is_an_owner_drop(tmp_path):
    landing = tmp_path / "landing"
    landing.mkdir()
    for name in ("inv_alpha_1001.pdf", "inv_alpha_1001.pdf.extract.json"):
        shutil.copy(LANDING / name, landing / name)
    _intake(tmp_path, landing)
    (prov,) = _events(tmp_path, "ap.provenance.recorded")
    assert prov["payload"]["verdict"] == "owner_drop"
    assert prov["payload"]["sender"] is None


def test_provenance_is_never_read_as_a_disposition(tmp_path):
    # The retry set and the unprocessed pile read ap.intake.* / ap.invoice.*
    # events as a file's disposition; provenance must stay out of both.
    landing = _deliver(tmp_path, "ap@alphaparts.example", "inv.pdf", "inv_alpha_1001.pdf")
    _intake(tmp_path, landing)
    (prov,) = _events(tmp_path, "ap.provenance.recorded")
    assert not prov["event_type"].startswith(("ap.intake.", "ap.invoice."))
