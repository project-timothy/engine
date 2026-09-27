"""Bank-reconciliation checks over the three-way verification state.

Two detectors, both born from dated incidents:

- ``orphan_checks`` (2026-05-19): a check that cleared the bank with no AP
  ledger row is an unlogged payment, not a search miss. The ledger is
  downstream of whoever writes the checks; it is not the complete record.
- ``status_lag_anomalies`` (2026-05-21): a ledger row still reading payable
  while its payment is already committed (queued) or cleared is exactly the
  false "still owes" signal that nearly drove the double-payment.
"""

from __future__ import annotations

from typing import Any

from ..ap.registry import VendorRegistry
from ..ap.status import is_payable_eligible
from ..ap.verify import _match_by_evidence

Row = dict[str, Any]


def orphan_checks(state: Row, registry: VendorRegistry | None = None) -> list[Row]:
    """Cleared bank lines that resolve to no ledger row."""
    orphans: list[Row] = []
    for line in state.get("cleared_bank", []):
        if _match_by_evidence(state, line, registry) is None:
            orphans.append(dict(line))
    return orphans


def status_lag_anomalies(state: Row, registry: VendorRegistry | None = None) -> list[Row]:
    """Ledger rows whose recorded status trails the external evidence.

    A row in a payable-eligible status that matches a bill-pay-queue entry or
    a cleared bank line is committed or settled money wearing a payable label.
    Each anomaly carries the evidence kind so the report can say why.
    """
    anomalies: list[Row] = []
    for row in state.get("ap_ledger", []):
        if not is_payable_eligible(str(row.get("status", ""))):
            continue
        evidence: str | None = None
        for entry in state.get("billpay_queue", []):
            if _match_by_evidence(state, entry, registry) is row:
                evidence = "billpay_queue"
                break
        if evidence is None:
            for line in state.get("cleared_bank", []):
                if _match_by_evidence(state, line, registry) is row:
                    evidence = "cleared_bank"
                    break
        if evidence is not None:
            anomalies.append(
                {
                    "invoice_ref": str(row.get("invoice_ref")),
                    "payee": row.get("payee"),
                    "status": row.get("status"),
                    "evidence": evidence,
                }
            )
    return anomalies
