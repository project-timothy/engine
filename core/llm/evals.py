"""Eval sets per model job, and the gate on a tier change (row 7.13).

A model job is a promise: "this model, on this contract, gets these
documents right". Rows 7.9 to 7.12 made the model a tenant SETTING, which
means one line in ``tenant.toml`` can repoint the 08:00 run at a model
nobody has ever tested. This module is the answer.

**The sets.** Each gated job owns a directory under ``eval_sets/``::

    core/llm/eval_sets/<job>/
        job.json            the reply contract (an import path) + the probe prompt
        cases/*.json        one case: a note, the document text, the expected fields
        documents/*.pdf     the file each case attaches (minimal_pdf of the case text)
        results/<id>.json   what one model scored, by model id

A case's ``expect`` is a SUBSET, all the way down (:func:`_matches`): it
states the part under test and stays silent about the rest, which is what a
reply holding a list of objects needs (row 7.13b, the combined-scan grouper:
the grouping is the answer, the vendor and amount only name a child file).
What a case cannot state is a REFUSAL. The code that reads a reply owns that
(``scan_split.validate_groups`` holds a scan whose groups miss a page), and
it is tested where it lives.

**The gate.** :func:`check_eval_gate` runs at ``load_tenant``: every
``[llm.jobs]`` assignment whose job is GATED must have a green
``results/<model_id>.json``. A missing file and a red one are both refused,
naming the exact ``engine evals run`` command. Three things are never gated:
a job with no ``cases/`` directory (no set has been written for it yet), a
tier no job uses, and a job the tenant calls ``deterministic``.

**The harness.** :func:`run_eval_set` runs one job's cases through
:func:`core.llm.complete` against whatever adapter it is handed, and
:func:`write_results` records the run. It touches no ledger, writes no
``llm_calls`` row, and needs no tenant: an eval run is a measurement, not a
job. The fixture adapter is seeded from each case's own expectation
(:func:`seeded_fixture_adapter`), which makes the fixture results file a
harness self-test rather than a model score, and says so in its own summary.

Design note: ``docs/model-seam-design.md``, "The eval gate". Decision:
``docs/decisions/2026-09-16-eval-gated-tier-changes.md``.
"""

from __future__ import annotations

import importlib
import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from core.engine.config import LLM_DETERMINISTIC
from core.llm.gateway import (
    Adapter,
    Attachment,
    GatewayError,
    Message,
    Pricing,
    complete,
)

EVAL_SETS_ROOT = Path(__file__).resolve().parent / "eval_sets"
"""Where the sets live: beside the harness, inside the package, so a deploy
clone and an installed wheel carry the evidence with the code."""

JOB_FILE = "job.json"
CASES_DIRNAME = "cases"
DOCUMENTS_DIRNAME = "documents"
RESULTS_DIRNAME = "results"
DOCUMENT_TEXT_TOKEN = "{document_text}"
"""The document's text layer, for a job whose live site inlines it. Plain
replacement, not ``str.format``: a prompt that shows the model a JSON example
has braces of its own and must not be a format string."""

PAGE_COUNT_TOKEN = "{page_count}"
"""The pages of THIS case's document, for a job whose live ask states the
count (the combined-scan grouper's does: "the attached 4-page scan"). The
``user`` template is one string for the whole set, so the per-case value has
to be a substitution rather than a line in the template."""

PAGE_BREAK = "\f"
"""One form feed in a case's text is one page break in its document, the rule
``conftest.minimal_pdf`` writes the PDF by."""

DEFAULT_TIMEOUT_S = 180

_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")
_MIMES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
}


class EvalSetError(ValueError):
    """A malformed or missing eval set: the harness refuses to guess."""


class EvalGateError(ValueError):
    """A tenant points a gated job at a model with no green eval results.

    A ``ValueError`` on purpose: ``load_tenant`` raises it, and every command
    already treats a config error as exit code 2.
    """


# ---- locations -------------------------------------------------------------------


def eval_sets_root(root: Path | None = None) -> Path:
    """The sets root, read at call time so a test can point it elsewhere."""
    return Path(root) if root is not None else EVAL_SETS_ROOT


def model_slug(model_id: str) -> str:
    """A model id as a filename. Provider ids carry slashes and colons
    (``vendor/model-x``); the gate and the writer share this one rule, so a
    results file is always found where it was written."""
    return _SLUG_RE.sub("-", model_id.strip()).strip("-") or "unnamed"


def job_dir(job: str, root: Path | None = None) -> Path:
    return eval_sets_root(root) / job


def results_path(job: str, model_id: str, root: Path | None = None) -> Path:
    return job_dir(job, root) / RESULTS_DIRNAME / f"{model_slug(model_id)}.json"


def case_paths(job: str, root: Path | None = None) -> list[Path]:
    return sorted((job_dir(job, root) / CASES_DIRNAME).glob("*.json"))


def gated_jobs(root: Path | None = None) -> list[str]:
    """Every job that owns at least one case. Gating is additive by design:
    writing a set for a job is what turns the gate on for it."""
    base = eval_sets_root(root)
    if not base.is_dir():
        return []
    return sorted(
        path.name
        for path in base.iterdir()
        if path.is_dir() and (path / CASES_DIRNAME).is_dir() and case_paths(path.name, root)
    )


def current_commit(repo: Path | None = None) -> str:
    """The commit the harness ran from, for the results file. Empty when git
    cannot answer (an installed wheel, a tarball); never an exception."""
    where = repo or Path(__file__).resolve().parents[2]
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=where,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except OSError:  # pragma: no cover - no git on the host
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


# ---- the set ---------------------------------------------------------------------


@dataclass(frozen=True)
class EvalCase:
    """One case: what the model is shown, and what it must answer."""

    name: str
    note: str
    text: str
    document: str
    expect: dict[str, Any]


@dataclass(frozen=True)
class EvalJob:
    """One job's eval set: the reply contract, the probe prompt, the cases."""

    job: str
    output_model_ref: str
    system: str
    user: str
    decimal_fields: tuple[str, ...]
    cases: tuple[EvalCase, ...]
    directory: Path

    def output_model(self) -> type[BaseModel]:
        """The pydantic model the job's site validates its replies into,
        named as ``module:attr`` so the set states the contract it measures
        instead of carrying a second copy of it."""
        ref = self.output_model_ref
        if ":" not in ref:
            raise EvalSetError(f"{self.job}: output_model must read 'module:attr', got {ref!r}")
        module_name, attr = ref.split(":", 1)
        try:
            model = getattr(importlib.import_module(module_name), attr)
        except (ImportError, AttributeError) as exc:
            raise EvalSetError(f"{self.job}: cannot import output_model {ref!r}: {exc}") from exc
        if not (isinstance(model, type) and issubclass(model, BaseModel)):
            raise EvalSetError(f"{self.job}: output_model {ref!r} is not a pydantic model")
        return model

    def document_path(self, case: EvalCase) -> Path | None:
        if not case.document:
            return None
        return self.directory / DOCUMENTS_DIRNAME / case.document


def load_job(job: str, root: Path | None = None) -> EvalJob:
    directory = job_dir(job, root)
    spec_path = directory / JOB_FILE
    if not spec_path.exists():
        raise EvalSetError(f"no eval set for job {job!r}: {spec_path} is missing")
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    cases: list[EvalCase] = []
    for path in case_paths(job, root):
        raw = json.loads(path.read_text(encoding="utf-8"))
        cases.append(
            EvalCase(
                name=str(raw.get("name") or path.stem),
                note=str(raw.get("note", "")),
                text=str(raw.get("text", "")),
                document=str(raw.get("document", "")),
                expect=dict(raw.get("expect") or {}),
            )
        )
    if not cases:
        raise EvalSetError(f"eval set {job!r} has no cases under {directory / CASES_DIRNAME}")
    return EvalJob(
        job=str(spec.get("job") or job),
        output_model_ref=str(spec.get("output_model", "")),
        system=str(spec.get("system", "")),
        user=str(spec.get("user", "")),
        decimal_fields=tuple(spec.get("decimal_fields") or ()),
        cases=tuple(sorted(cases, key=lambda c: c.name)),
        directory=directory,
    )


# ---- the run ---------------------------------------------------------------------


@dataclass(frozen=True)
class CheckResult:
    field: str
    expected: str
    actual: str
    passed: bool


@dataclass(frozen=True)
class CaseResult:
    case: str
    passed: bool
    error: str
    checks: tuple[CheckResult, ...]


@dataclass(frozen=True)
class EvalReport:
    """One eval set against one model: the evidence the gate reads."""

    job: str
    model_id: str
    adapter: str
    tier: str
    ran_at: str
    engine_commit: str
    seeded: bool
    results: tuple[CaseResult, ...]
    provider_models: tuple[str, ...] = ()

    @property
    def cases(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def failed(self) -> int:
        return self.cases - self.passed

    @property
    def summary(self) -> str:
        where = f"adapter {self.adapter}"
        if self.tier:
            where += f", tier {self.tier}"
        line = (
            f"{self.job}: {self.passed} of {self.cases} cases passed "
            f"on model {self.model_id!r} ({where})"
        )
        if self.seeded:
            line += "; replies seeded from the cases, so this scores the harness, not a model"
        return line

    def to_dict(self) -> dict[str, Any]:
        return {
            "job": self.job,
            "model_id": self.model_id,
            "adapter": self.adapter,
            "tier": self.tier,
            "seeded": self.seeded,
            "ran_at": self.ran_at,
            "engine_commit": self.engine_commit,
            "provider_models": list(self.provider_models),
            "cases": self.cases,
            "passed": self.passed,
            "failed": self.failed,
            "summary": self.summary,
            "results": [
                {
                    "case": r.case,
                    "passed": r.passed,
                    "error": r.error,
                    "checks": [
                        {
                            "field": c.field,
                            "expected": c.expected,
                            "actual": c.actual,
                            "passed": c.passed,
                        }
                        for c in sorted(r.checks, key=lambda c: c.field)
                    ],
                }
                for r in sorted(self.results, key=lambda r: r.case)
            ],
        }


def _text(value: Any) -> str:
    """How a value reads in a check. ``None`` and an empty string read the
    same: a field a model left out and a field it left blank are one answer
    to the reader of a diff."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _money(value: Any) -> Decimal | None:
    text = _text(value).replace("$", "").replace(",", "").strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _readable(value: Any) -> str:
    """How a value reads in a committed results file. A container reads as
    JSON rather than as a Python repr, because a check on a nested field
    records the whole reply and a reader diffs it by eye."""
    if isinstance(value, dict | list):
        return json.dumps(value, sort_keys=True)
    return _text(value)


def _matches(expected: Any, actual: Any, *, as_money: bool) -> bool:
    """``expect`` is a SUBSET, and the rule reads all the way down.

    At the top level that is row 7.13's rule: only the fields a case names
    are checked. A reply holding a list of objects (the grouper's
    ``GroupingReply``) needs the same rule one level deeper, because a case
    about page grouping must not also assert the vendor, amount, and date,
    which the brief calls claims and lets a model leave empty. So a dict
    expectation checks the keys it names and ignores the rest, and a list
    expectation checks position by position with the length under test.
    """
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            _matches(want, actual.get(key), as_money=as_money) for key, want in expected.items()
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(expected) == len(actual)
            and all(
                _matches(want, got, as_money=as_money)
                for want, got in zip(expected, actual, strict=True)
            )
        )
    if as_money:
        want, got = _money(expected), _money(actual)
        if want is not None and got is not None:
            return want == got
    return _text(expected) == _text(actual)


def _check(field_name: str, expected: Any, actual: Any, *, as_money: bool) -> CheckResult:
    return CheckResult(
        field_name,
        _readable(expected),
        _readable(actual),
        _matches(expected, actual, as_money=as_money),
    )


def document_pages(text: str) -> int:
    """Pages in the document a case's text builds: one per form feed."""
    return text.count(PAGE_BREAK) + 1


def _messages(spec: EvalJob, case: EvalCase) -> list[Message]:
    ask = spec.user.replace(DOCUMENT_TEXT_TOKEN, case.text)
    ask = ask.replace(PAGE_COUNT_TOKEN, str(document_pages(case.text)))
    return [Message("system", spec.system), Message("user", ask)]


def _attachments(spec: EvalJob, case: EvalCase) -> list[Attachment]:
    path = spec.document_path(case)
    if path is None:
        return []
    if not path.exists():
        raise EvalSetError(f"{spec.job}/{case.name}: no document at {path}")
    return [Attachment(path, _MIMES.get(path.suffix.lower(), "application/octet-stream"))]


def seeded_fixture_adapter(case: EvalCase) -> Adapter:
    """A fixture adapter answering this case's own expectation.

    It proves the case set parses, the contract validates, and the checks
    line up. It proves nothing about a model, which is why every results
    file it produces says ``seeded`` and says so in its summary line.
    """
    from core.llm.adapters.fixture import FixtureAdapter

    return FixtureAdapter({"*": json.dumps(case.expect, sort_keys=True)})


def run_eval_set(
    job: str,
    *,
    model: str,
    adapter_name: str,
    make_adapter: Callable[[EvalCase], Adapter],
    tier: str = "",
    root: Path | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    engine_commit: str = "",
    ran_at: str | None = None,
    seeded: bool | None = None,
) -> EvalReport:
    """Run one job's cases against one model and report, case by case.

    Every case is one :func:`core.llm.complete` call: the job's prompt, the
    case's text, the case's document as an attachment, the job's reply
    contract. A gateway failure (transport, validation) fails that case with
    its message and the run continues, because the point is to score a model
    across the set, not to stop at the first bad reply.
    """
    spec = load_job(job, root)
    output_model = spec.output_model()
    money = set(spec.decimal_fields)
    results: list[CaseResult] = []
    providers: set[str] = set()
    for case in spec.cases:
        try:
            outcome = complete(
                spec.job,
                _messages(spec, case),
                output_model,
                adapter=make_adapter(case),
                model=model,
                attachments=_attachments(spec, case),
                timeout_s=timeout_s,
                pricing=Pricing(Decimal("0"), Decimal("0")),
            )
        except GatewayError as exc:
            results.append(CaseResult(case.name, False, f"{type(exc).__name__}: {exc}", ()))
            continue
        if outcome.record.provider_model:
            providers.add(outcome.record.provider_model)
        answered = outcome.output.model_dump()
        checks = tuple(
            _check(name, want, answered.get(name), as_money=name in money)
            for name, want in sorted(case.expect.items())
        )
        results.append(CaseResult(case.name, all(c.passed for c in checks), "", checks))
    return EvalReport(
        job=spec.job,
        model_id=model,
        adapter=adapter_name,
        tier=tier,
        ran_at=ran_at or datetime.now(UTC).isoformat(),
        engine_commit=engine_commit,
        seeded=adapter_name == "fixture" if seeded is None else seeded,
        results=tuple(results),
        provider_models=tuple(sorted(providers)),
    )


def write_results(report: EvalReport, root: Path | None = None) -> Path:
    """Write the results file for this (job, model). Deterministic ordering
    everywhere, so a re-run against the same model is a readable diff."""
    path = results_path(report.job, report.model_id, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")
    return path


# ---- the gate --------------------------------------------------------------------


def tier_model(settings: Any, name: str):
    """A tier as the gateway needs it, for the eval command's ``--tier``.

    Kept here rather than in :mod:`core.llm.policy` because an eval run has
    no job type to resolve: it names the tier directly.
    """
    from core.llm.policy import ResolvedModel

    tier = settings.tiers.get(name)
    if tier is None:
        known = ", ".join(sorted(settings.tiers)) or "none"
        raise EvalSetError(f"[llm.tiers] has no tier {name!r} (tiers: {known})")
    return ResolvedModel(
        tier=name,
        adapter=tier.adapter,
        model=tier.model,
        base_url=tier.base_url,
        api_key_env=tier.api_key_env,
        pricing=Pricing(tier.pricing.input_usd_per_mtok, tier.pricing.output_usd_per_mtok),
        fallback=tuple(tier.fallback),
    )


def _display(path: Path) -> str:
    try:
        return str(path.relative_to(Path(__file__).resolve().parents[2]))
    except ValueError:
        return str(path)


def _command(job: str, tier: str, tenant: str) -> str:
    parts = ["uv run engine evals run", job]
    if tenant:
        parts.append(f"--tenant {tenant}")
    parts.append(f"--tier {tier}")
    return " ".join(parts)


def check_eval_gate(settings: Any, *, tenant: str = "", root: Path | None = None) -> None:
    """Refuse a tenant that points a GATED job at an unproven model.

    Called from ``load_tenant`` (``core/engine/config.py``), so the refusal
    lands where a tier is changed rather than at 08:00 the next morning.
    ``settings`` is the tenant's ``LlmSettings``; every reference in it has
    already been checked, so an unknown tier name is somebody else's error.
    """
    base = eval_sets_root(root)
    gated = set(gated_jobs(base))
    for job, tier_name in sorted(settings.jobs.items()):
        if tier_name == LLM_DETERMINISTIC or job not in gated:
            continue
        tier = settings.tiers.get(tier_name)
        if tier is None:  # the reference check already refused this file
            continue
        path = results_path(job, tier.model, base)
        command = _command(job, tier_name, tenant)
        if not path.exists():
            raise EvalGateError(
                f"[llm.jobs].{job} = {tier_name!r} runs model {tier.model!r} on adapter "
                f"{tier.adapter!r}, which has no eval results for this job. Run the set "
                f"and commit the results file:\n  {command}\n"
                f"expected at {_display(path)}"
            )
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            failed = int(data["failed"])
            cases = int(data.get("cases", failed + int(data.get("passed", 0))))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise EvalGateError(
                f"[llm.jobs].{job} = {tier_name!r}: the eval results at {_display(path)} "
                f"cannot be read ({exc}). Run the set again:\n  {command}"
            ) from exc
        if failed:
            when = data.get("ran_at", "")
            raise EvalGateError(
                f"[llm.jobs].{job} = {tier_name!r} runs model {tier.model!r}, whose eval "
                f"results are red: {failed} of {cases} cases failed "
                f"({_display(path)}, run {when}). Fix the job or run the set again:\n"
                f"  {command}"
            )
        scored_on = str(data.get("adapter", ""))
        if scored_on and scored_on != tier.adapter:
            # Closes the obvious hole: a seeded fixture run answers every case
            # from the case's own expectation, so it is evidence about the
            # HARNESS. It opens the gate for a fixture tier and for nothing else.
            raise EvalGateError(
                f"[llm.jobs].{job} = {tier_name!r} runs model {tier.model!r} on adapter "
                f"{tier.adapter!r}, but {_display(path)} scored it on adapter "
                f"{scored_on!r}. Score it where it runs:\n  {command}"
            )
