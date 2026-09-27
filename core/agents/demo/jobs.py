"""Deterministic job implementations for the demo agent.

Each job is a :class:`JobHandler` of two functions: ``key`` (a cheap,
side-effect-free idempotency key the runner checks before doing any work) and
``run`` (the work, returning a ``JobOutput``). The runner owns persistence,
commits, and the no-op guarantee.
"""

from __future__ import annotations

from pathlib import Path

from ...engine.contracts import EventSpec, JobContext, JobHandler, JobOutput, RetryPolicy
from ...engine.runkey import RunKey
from .schema import DemoBatch

DEFAULT_FIXTURE = "fixtures/sample.json"

# demo/flaky: the cross-run retry fixture (phase 7 row 7.23). The ladder is
# short on purpose (a minute, then five) so a test can read it back.
FLAKY_POLICY = RetryPolicy(
    max_attempts=3, backoff_seconds=[60, 300], retry_on=["transport_error", "timeout"]
)
ATTEMPT_RECORD = "demo.flaky.attempt"
MOVE_RECORD = "demo.flaky.moved"


class DemoTransportError(RuntimeError):
    """A failure carrying the ``cause`` / ``transient`` taxonomy the AP
    extractor and the model gateway raise with; the runner reads both to
    decide whether a declared retry policy applies."""

    def __init__(self, message: str, *, cause: str, transient: bool = True) -> None:
        super().__init__(message)
        self.cause = cause
        self.transient = transient


def _fixture_path(ctx: JobContext) -> Path:
    override = ctx.params.get("fixture")
    if override:
        return Path(override)
    base = ctx.agent_dir or Path(__file__).resolve().parent
    return base / DEFAULT_FIXTURE


def _load_batch(ctx: JobContext) -> tuple[DemoBatch, bytes]:
    path = _fixture_path(ctx)
    raw = path.read_bytes()
    return DemoBatch.model_validate_json(raw), raw


def _ingest_key(ctx: JobContext) -> str:
    """Idempotency key = tenant slug + fixture bytes. Same input, same key."""
    _, raw = _load_batch(ctx)
    return RunKey(ctx, "ingest").add("fixture", raw).config("identity").digest()


def _ingest_run(ctx: JobContext) -> JobOutput:
    batch, _ = _load_batch(ctx)
    events = [
        EventSpec(
            key=f"item:{item.id}",
            event_type="demo.item.ingested",
            payload={"id": item.id, "value": item.value, "batch": batch.batch_id},
        )
        for item in batch.items
    ]
    actions = [f"ingested item {item.id}" for item in batch.items]
    legal_name = ctx.tenant.identity.legal_name
    summary = f"ingested {len(batch.items)} item(s) from batch {batch.batch_id} for {legal_name}"
    return JobOutput(status="ok", summary=summary, actions=actions, events=events)


def _flaky_key(ctx: JobContext) -> str:
    key = RunKey(ctx, "flaky").config("identity")
    key.param("fail_times")
    key.param("cause")
    key.param("transient")
    return key.digest()


def _flaky_run(ctx: JobContext) -> JobOutput:
    """Fail ``fail_times`` times with ``cause`` (default: twice, a transport
    error), then succeed. Every attempt records itself; the FIRST attempt
    also records one durable move before it dies, so a later attempt under
    the same run key finds the move already on the record and skips it
    (record-then-move, no double write)."""
    fail_times = int(ctx.params.get("fail_times", "2"))
    cause = str(ctx.params.get("cause", "transport_error"))
    transient = str(ctx.params.get("transient", "yes")).lower() not in ("no", "false", "0")
    mine = [r for r in ctx.records(ATTEMPT_RECORD) if r["run_key"] == ctx.run_key]
    attempt = len(mine) + 1
    ctx.record_now(f"attempt:{attempt}", ATTEMPT_RECORD, {"attempt": attempt})
    moved = ctx.record_now("move:sample", MOVE_RECORD, {"file": "sample.json", "attempt": attempt})
    move_note = "recorded the move" if moved else "the move was already on the record"
    if attempt <= fail_times:
        raise DemoTransportError(
            f"flaky: attempt {attempt} failed on purpose ({cause})",
            cause=cause,
            transient=transient,
        )
    legal_name = ctx.tenant.identity.legal_name
    return JobOutput(
        status="ok",
        summary=f"flaky: succeeded on attempt {attempt} for {legal_name}; {move_note}",
        actions=[f"attempt {attempt} succeeded"],
        events=[
            EventSpec(
                key="succeeded",
                event_type="demo.flaky.succeeded",
                payload={"attempt": attempt, "moved_this_attempt": moved},
            )
        ],
    )


JOBS: dict[str, JobHandler] = {
    "ingest": JobHandler(key=_ingest_key, run=_ingest_run),
    "flaky": JobHandler(key=_flaky_key, run=_flaky_run, retry=FLAKY_POLICY),
}
