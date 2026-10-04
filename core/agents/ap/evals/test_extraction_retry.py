"""Incident regression: 2026-06-23 a real invoice dropped to review on a
transient extraction failure.

D&E 20174 failed inside a batch intake with "extraction failed" but extracted
cleanly in 7.6s standalone (docs/lessons.md, "Transient is not terminal"). The failure was
transient (a one-off transport blip, or the 120s timeout firing on a
momentarily slow transport), not content. A transient failure must be retried
before the file is flagged; a terminal failure (oversize, unparseable reply)
must still flag immediately, since retrying cannot change it. Each failure also
carries a structured ``cause`` so a drop is diagnosable from the ledger.

Retargeted in phase 7 row 7.10, intent preserved: the two classification
cases that drove ``ClaudeExtractor`` now drive ``GatewayExtractor``, whose
provider comes from the tenant policy. The retry wrapper is unchanged.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.agents.ap.extraction import (
    ExtractionError,
    GatewayExtractor,
    RetryingExtractor,
)
from core.agents.ap.schema import ExtractedDocument
from core.engine.config import TenantConfig
from core.engine.contracts import JobContext
from core.ledger import Ledger
from core.llm.adapters.fixture import FixtureAdapter

_TENANT = {
    "identity": {"legal_name": "Retry Test Co", "slug": "demo"},
    "llm": {
        "tiers": {
            "seat": {
                "adapter": "fixture",
                "model": "seat-model",
                "pricing": {"input_usd_per_mtok": "0", "output_usd_per_mtok": "0"},
            }
        },
        "jobs": {"invoice_extract": "seat"},
    },
}


def _ctx(tmp_path: Path) -> JobContext:
    return JobContext(
        tenant=TenantConfig.model_validate(_TENANT),
        tenant_slug="demo",
        ledger=Ledger.open(tmp_path / "ledger"),
        agent="ap",
        job="intake",
        run_key="demo.ap.intake.k1",
    )


class _FlakyExtractor:
    """Fails a set number of times, then succeeds. Counts its calls."""

    def __init__(self, *, fail_times: int, transient: bool = True, cause: str = "timeout") -> None:
        self._fail_times = fail_times
        self._transient = transient
        self._cause = cause
        self.calls = 0

    def extract(self, path: Path) -> ExtractedDocument:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise ExtractionError(
                f"simulated {self._cause}", cause=self._cause, transient=self._transient
            )
        return ExtractedDocument(doc_type="invoice", confidence=0.98, warnings=[])


def _recorder() -> tuple[list[float], object]:
    slept: list[float] = []
    return slept, slept.append


def test_transient_failure_is_retried_and_recovers():
    # Two blips, then the third try connects: the D&E 20174 shape.
    inner = _FlakyExtractor(fail_times=2)
    slept, sleeper = _recorder()
    ex = RetryingExtractor(inner, attempts=3, backoff_s=(2.0, 5.0), sleeper=sleeper)

    doc = ex.extract(Path("DE_20174.pdf"))

    assert doc.doc_type == "invoice"
    assert inner.calls == 3  # redialed twice, succeeded on the third
    assert slept == [2.0, 5.0]  # backoff waited between tries


def test_terminal_failure_flags_immediately_without_retry():
    inner = _FlakyExtractor(fail_times=1, transient=False, cause="bad_reply")
    slept, sleeper = _recorder()
    ex = RetryingExtractor(inner, attempts=3, sleeper=sleeper)

    with pytest.raises(ExtractionError) as caught:
        ex.extract(Path("garbage.pdf"))

    assert caught.value.cause == "bad_reply"
    assert inner.calls == 1  # no point retrying a file that cannot parse
    assert slept == []  # never waited


def test_exhausted_retries_reraise_the_last_transient_error():
    inner = _FlakyExtractor(fail_times=99)  # never recovers
    slept, sleeper = _recorder()
    ex = RetryingExtractor(inner, attempts=3, backoff_s=(2.0, 5.0), sleeper=sleeper)

    with pytest.raises(ExtractionError) as caught:
        ex.extract(Path("DE_20174.pdf"))

    assert caught.value.transient is True
    assert caught.value.cause == "timeout"
    assert inner.calls == 3  # spent the full budget before giving up
    assert slept == [2.0, 5.0]


def test_oversize_failure_is_classified_terminal(tmp_path):
    f = tmp_path / "huge.pdf"
    f.write_bytes(b"x" * 50)
    ex = GatewayExtractor(_ctx(tmp_path), max_file_bytes=10)

    with pytest.raises(ExtractionError) as caught:
        ex.extract(f)

    assert caught.value.cause == "oversize"
    assert caught.value.transient is False


def test_timeout_failure_is_classified_transient(tmp_path):
    path = tmp_path / "stalls.pdf"
    path.write_bytes(b"%PDF-1.4 stub")
    ex = GatewayExtractor(
        _ctx(tmp_path),
        timeout_s=1,
        adapter=FixtureAdapter(TimeoutError("the transport never answered")),
        text_reader=lambda _p: "text",
    )
    with pytest.raises(ExtractionError) as caught:
        ex.extract(path)

    assert caught.value.cause == "timeout"
    assert caught.value.transient is True
