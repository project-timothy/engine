"""Unit tests for idempotency-key conflict detection.

Invariant 3 means "re-running THIS job with this key is a no-op". A second
write that reuses a key for a DIFFERENT tenant/agent/job is a programming
error, not an idempotent replay, and must raise instead of silently returning
the other job's row.
"""

from __future__ import annotations

import pytest

from core.ledger import IdempotencyKeyConflict, Ledger


def test_same_key_same_job_is_idempotent_not_conflict(tmp_path):
    with Ledger.open(tmp_path) as ledger:
        first = ledger.record_run(
            idempotency_key="k1",
            tenant="t",
            agent="demo",
            job="ingest",
            status="ok",
            shadow=False,
            result_json="{}",
            summary="",
        )
        replay = ledger.record_run(
            idempotency_key="k1",
            tenant="t",
            agent="demo",
            job="ingest",
            status="ok",
            shadow=False,
            result_json="{}",
            summary="",
        )
        assert first.is_new and not replay.is_new
        assert replay.id == first.id


def test_same_key_different_job_raises(tmp_path):
    with Ledger.open(tmp_path) as ledger:
        ledger.record_run(
            idempotency_key="k1",
            tenant="t",
            agent="demo",
            job="ingest",
            status="ok",
            shadow=False,
            result_json="{}",
            summary="",
        )
        with pytest.raises(IdempotencyKeyConflict):
            ledger.record_run(
                idempotency_key="k1",
                tenant="t",
                agent="demo",
                job="other-job",
                status="ok",
                shadow=False,
                result_json="{}",
                summary="",
            )


def test_same_key_different_tenant_raises(tmp_path):
    with Ledger.open(tmp_path) as ledger:
        ledger.record_run(
            idempotency_key="k1",
            tenant="t1",
            agent="demo",
            job="ingest",
            status="ok",
            shadow=False,
            result_json="{}",
            summary="",
        )
        with pytest.raises(IdempotencyKeyConflict):
            ledger.record_run(
                idempotency_key="k1",
                tenant="t2",
                agent="demo",
                job="ingest",
                status="ok",
                shadow=False,
                result_json="{}",
                summary="",
            )
