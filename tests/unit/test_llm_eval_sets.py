"""Eval sets per model job, and the gate that refuses an unproven model.

Phase 7 row 7.13 (issue #222), ``docs/model-seam-design.md`` section "The
eval gate". Two halves, both proved here:

- **The sets.** Each gated model job owns ``core/llm/eval_sets/<job>/``: a
  ``job.json`` (the reply contract plus the probe prompt), ``cases/`` (the
  inputs and the expected outputs), ``documents/`` (the attachment each case
  hands the model), and ``results/<model_id>.json`` (what a model scored).
- **The gate.** ``load_tenant`` refuses a tenant whose ``[llm.jobs]`` points a
  GATED job at a model with no green results file, naming the exact
  ``engine evals run`` command. A job with no ``cases/`` directory is not
  gated; a tier no job uses is not gated; ``deterministic`` is never gated.

The live tenants must keep loading unchanged: the first test is the one that
would fail at 08:00 tomorrow if this row got the gate wrong.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import minimal_pdf
from core.engine.cli import main as cli_main
from core.engine.config import load_tenant
from core.llm import evals as eval_mod
from core.llm.adapters.fixture import FixtureAdapter
from core.llm.evals import (
    EvalGateError,
    check_eval_gate,
    eval_sets_root,
    gated_jobs,
    load_job,
    model_slug,
    results_path,
    run_eval_set,
    seeded_fixture_adapter,
    write_results,
)
from core.llm.gateway import RawReply

REPO = Path(__file__).resolve().parents[2]
TENANTS = REPO / "tenants"

PROVEN = (
    'proven = { adapter = "fixture", model = "fixture-model", '
    'pricing = { input_usd_per_mtok = "0", output_usd_per_mtok = "0" } }'
)
UNPROVEN = (
    'ghost = { adapter = "openai_compat", model = "ghost-9", '
    'pricing = { input_usd_per_mtok = "1", output_usd_per_mtok = "2" } }'
)


def _tenant_file(root: Path, slug: str, *, tiers: str, jobs: str) -> Path:
    directory = root / slug
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "tenant.toml").write_text(
        "[identity]\n"
        'legal_name = "Test Co"\n'
        f'slug = "{slug}"\n'
        'timezone = "America/New_York"\n\n'
        f"[llm.tiers]\n{tiers}\n\n"
        f"[llm.jobs]\n{jobs}\n",
        encoding="utf-8",
    )
    return directory / "tenant.toml"


def _seed_job(root: Path, job: str = "invoice_extract") -> Path:
    """A three-case eval set in a scratch root: the harness under test with
    no shipped data involved."""
    directory = root / job
    (directory / "cases").mkdir(parents=True, exist_ok=True)
    (directory / "job.json").write_text(
        json.dumps(
            {
                "job": job,
                "output_model": "core.agents.ap.extraction:ExtractionReply",
                "decimal_fields": ["amount"],
                "system": "Answer about the document.",
                "user": "The text is: {document_text}",
            }
        ),
        encoding="utf-8",
    )
    for index, doc_type in enumerate(("invoice", "po", "receipt")):
        (directory / "cases" / f"c{index}.json").write_text(
            json.dumps(
                {
                    "name": f"c{index}",
                    "note": "seeded",
                    "text": f"a {doc_type}",
                    "expect": {"doc_type": doc_type, "amount": "10.00"},
                }
            ),
            encoding="utf-8",
        )
    return directory


def _seed_nested_job(root: Path, job: str = "scan_group") -> Path:
    """A set whose reply contract holds a LIST OF OBJECTS: the grouper's
    shape (row 7.13b). Only part of each object is under test, which is what
    a nested expectation is for."""
    directory = root / job
    (directory / "cases").mkdir(parents=True, exist_ok=True)
    (directory / "job.json").write_text(
        json.dumps(
            {
                "job": job,
                "output_model": "core.agents.expenses.scan_split:GroupingReply",
                "system": "Group the pages of the scan.",
                "user": "The attached scan has {page_count} pages.",
            }
        ),
        encoding="utf-8",
    )
    (directory / "cases" / "c0.json").write_text(
        json.dumps(
            {
                "name": "c0",
                "note": "seeded",
                "text": "page one\fpage two\fpage three",
                "expect": {"groups": [{"pages": [1, 2]}, {"pages": [3]}]},
            }
        ),
        encoding="utf-8",
    )
    return directory


NESTED_REPLY = json.dumps(
    {
        "groups": [
            {"pages": [1, 2], "vendor": "Corner Hardware", "amount": "12.34", "date": "2026-06-04"},
            {"pages": [3], "vendor": "Cedar Lot Parking", "amount": "12.00", "date": "2026-06-04"},
        ]
    }
)


# ---- the live tenants still load -------------------------------------------------


@pytest.mark.parametrize("slug", ["demo"])
def test_every_shipped_tenant_loads_with_the_gate_on(slug: str):
    """The gate ships green: today's tenant files load untouched, so the
    08:00 run and the nightly auditor see no change."""
    cfg = load_tenant(slug, tenants_root=TENANTS)
    assert cfg.identity.slug == slug
    assert cfg.llm.jobs, "the tenant says which jobs call a model"


def test_the_init_template_names_a_model_every_gated_job_has_results_for():
    """``engine init`` (row 7.19) renders [llm.jobs] onto the fixture tier; a
    tenant created tomorrow must pass the gate the moment it is created."""
    template = (TENANTS / "_templates" / "tenant.toml.tmpl").read_text(encoding="utf-8")
    assert 'model = "fixture-model"' in template
    for job in gated_jobs():
        assert results_path(job, "fixture-model").exists(), (
            f"the template points {job} at 'fixture-model'; ship that model's results file"
        )


def test_the_shipped_tenants_pass_the_gate_as_its_own_clause():
    cfg = load_tenant("demo", tenants_root=TENANTS)
    check_eval_gate(cfg.llm, tenant="demo")


# ---- the gate ---------------------------------------------------------------------


def test_a_tier_change_to_an_unknown_model_is_refused_at_config_load(tmp_path: Path):
    _tenant_file(tmp_path, "t1", tiers=f"{PROVEN}\n{UNPROVEN}", jobs='invoice_extract = "ghost"')
    with pytest.raises(EvalGateError) as err:
        load_tenant("t1", tenants_root=tmp_path)
    message = str(err.value)
    assert "invoice_extract" in message
    assert "ghost-9" in message
    assert "uv run engine evals run invoice_extract" in message
    assert "--tier ghost" in message
    assert "--tenant t1" in message
    assert "ghost-9.json" in message


def test_the_refusal_is_a_config_error_so_the_cli_exits_two():
    assert issubclass(EvalGateError, ValueError)


def test_a_job_with_no_cases_directory_is_not_gated(tmp_path: Path):
    """The rule: a job is gated when it owns cases. ``draft_advisory`` and
    ``audit_triage`` own none (the auditor's vendored client and the runner
    lane), so pointing them at any model loads."""
    assert "draft_advisory" not in gated_jobs()
    _tenant_file(tmp_path, "t2", tiers=UNPROVEN, jobs='draft_advisory = "ghost"')
    cfg = load_tenant("t2", tenants_root=tmp_path)
    assert cfg.llm.jobs["draft_advisory"] == "ghost"


def test_a_tier_no_job_uses_is_not_gated(tmp_path: Path):
    _tenant_file(tmp_path, "t3", tiers=f"{PROVEN}\n{UNPROVEN}", jobs='invoice_extract = "proven"')
    cfg = load_tenant("t3", tenants_root=tmp_path)
    assert "ghost" in cfg.llm.tiers


def test_a_gated_job_set_to_deterministic_is_not_gated(tmp_path: Path):
    _tenant_file(tmp_path, "t4", tiers=UNPROVEN, jobs='invoice_extract = "deterministic"')
    cfg = load_tenant("t4", tenants_root=tmp_path)
    assert cfg.llm.jobs["invoice_extract"] == "deterministic"


def test_red_results_are_refused_as_loudly_as_missing_ones(tmp_path: Path, monkeypatch):
    sets = tmp_path / "sets"
    _seed_job(sets)
    monkeypatch.setattr(eval_mod, "EVAL_SETS_ROOT", sets)
    path = results_path("invoice_extract", "ghost-9")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "job": "invoice_extract",
                "model_id": "ghost-9",
                "cases": 3,
                "passed": 1,
                "failed": 2,
                "ran_at": "2026-09-16T12:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    _tenant_file(tmp_path, "t5", tiers=UNPROVEN, jobs='invoice_extract = "ghost"')
    with pytest.raises(EvalGateError) as err:
        load_tenant("t5", tenants_root=tmp_path)
    assert "2 of 3" in str(err.value)


def test_a_seeded_fixture_score_does_not_open_the_gate_for_a_real_adapter(
    tmp_path: Path, monkeypatch
):
    """The hole a seeded fixture run would otherwise leave: it answers every
    case from the case's own expectation, so it is evidence about the
    HARNESS. The gate accepts a results file only from the adapter the tier
    actually runs."""
    sets = tmp_path / "sets"
    _seed_job(sets)
    monkeypatch.setattr(eval_mod, "EVAL_SETS_ROOT", sets)
    report = run_eval_set(
        "invoice_extract",
        model="ghost-9",
        adapter_name="fixture",
        make_adapter=seeded_fixture_adapter,
        engine_commit="abc1234",
    )
    write_results(report)
    _tenant_file(tmp_path, "t7", tiers=UNPROVEN, jobs='invoice_extract = "ghost"')
    with pytest.raises(EvalGateError) as err:
        load_tenant("t7", tenants_root=tmp_path)
    assert "scored it on adapter 'fixture'" in str(err.value)


def test_the_shipped_seat_results_are_not_seeded():
    """The live tier's evidence is a real model run, not a harness self-test."""
    for job in gated_jobs():
        path = results_path(job, "default")
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["seeded"] is False
        assert data["adapter"] != "fixture"
        assert data["provider_models"], "a live run records what actually answered"


def test_the_gate_reads_the_results_file_by_the_model_id_slug():
    assert model_slug("vendor/model-x") == "vendor-model-x"
    assert model_slug("default") == "default"
    assert model_slug("a b:c") == "a-b-c"


# ---- the shipped sets -------------------------------------------------------------


def test_every_gated_job_has_cases_a_job_file_and_results():
    jobs = gated_jobs()
    assert jobs, "the row ships at least one gated job"
    for job in jobs:
        spec = load_job(job)
        assert 3 <= len(spec.cases) <= 5, f"{job}: keep the set small and readable"
        assert spec.system and spec.user
        results = sorted((eval_sets_root() / job / "results").glob("*.json"))
        assert results, f"{job}: no results file shipped"


def test_every_committed_results_file_is_green_and_well_formed():
    for job in gated_jobs():
        spec = load_job(job)
        for path in sorted((eval_sets_root() / job / "results").glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            assert data["job"] == job
            assert model_slug(data["model_id"]) == path.stem
            assert data["failed"] == 0, f"{path}: red results never ship"
            assert data["cases"] == len(spec.cases)
            assert data["summary"]
            assert data["engine_commit"], "a results file says which engine ran it"
            names = [r["case"] for r in data["results"]]
            assert names == sorted(names), "case order is deterministic"
            for row in data["results"]:
                fields = [c["field"] for c in row["checks"]]
                assert fields == sorted(fields), "check order is deterministic"


def test_every_case_document_is_the_pdf_of_the_case_text():
    """The attachment and the inlined text are one document: each file is
    ``minimal_pdf(case.text)``, so a case cannot drift from what the model
    actually reads."""
    for job in gated_jobs():
        spec = load_job(job)
        for case in spec.cases:
            if not case.document:
                continue
            path = eval_sets_root() / job / "documents" / case.document
            assert path.read_bytes() == minimal_pdf(case.text), f"{job}/{case.name}"


def test_each_eval_set_declares_the_reply_contract_its_site_uses():
    from core.agents.ap.extraction import ExtractionReply
    from core.agents.expenses.inbox import InboxLabel

    assert load_job("invoice_extract").output_model() is ExtractionReply
    assert load_job("receipt_extract").output_model() is ExtractionReply
    assert load_job("inbox_classify").output_model() is InboxLabel


# ---- the harness ------------------------------------------------------------------


def test_a_seeded_fixture_run_passes_every_case_and_writes_the_results_file(tmp_path: Path):
    _seed_job(tmp_path)
    report = run_eval_set(
        "invoice_extract",
        model="fixture-model",
        adapter_name="fixture",
        tier="fixture",
        make_adapter=seeded_fixture_adapter,
        root=tmp_path,
        engine_commit="abc1234",
        ran_at="2026-09-16T12:00:00+00:00",
    )
    assert report.failed == 0
    assert report.cases == 3
    assert report.seeded is True
    assert "3 of 3" in report.summary
    path = write_results(report, root=tmp_path)
    assert path == tmp_path / "invoice_extract" / "results" / "fixture-model.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["adapter"] == "fixture"
    assert data["engine_commit"] == "abc1234"
    assert data["ran_at"] == "2026-09-16T12:00:00+00:00"


def test_a_wrong_reply_fails_its_case_and_records_expected_against_actual(tmp_path: Path):
    _seed_job(tmp_path)
    report = run_eval_set(
        "invoice_extract",
        model="m",
        adapter_name="fixture",
        tier="t",
        make_adapter=lambda case: FixtureAdapter({"*": '{"doc_type": "quote", "amount": "10.00"}'}),
        root=tmp_path,
        engine_commit="abc1234",
    )
    assert report.failed == 3
    wrong = [r for r in report.results if not r.passed][0]
    check = [c for c in wrong.checks if c.field == "doc_type"][0]
    assert check.actual == "quote"
    assert check.expected != "quote"


def test_money_is_compared_as_a_decimal_not_as_text(tmp_path: Path):
    """A declared money field is compared as a number: 10.0 and 10.00 are the
    same answer, and a case should not fail on trailing zeros. The dollar
    sign is stripped on both sides for the fields that are plain text
    (``InboxLabel.amount``); a ``DecimalString`` field never carries one,
    because the gateway refuses the reply first."""
    _seed_job(tmp_path)
    report = run_eval_set(
        "invoice_extract",
        model="m",
        adapter_name="fixture",
        tier="t",
        make_adapter=lambda case: FixtureAdapter(
            {"*": json.dumps({"doc_type": case.expect["doc_type"], "amount": "10.0"})}
        ),
        root=tmp_path,
        engine_commit="abc1234",
    )
    assert report.failed == 0, "10.0 and 10.00 are the same money"


def test_a_reply_that_does_not_validate_fails_the_case_with_the_error(tmp_path: Path):
    _seed_job(tmp_path)
    report = run_eval_set(
        "invoice_extract",
        model="m",
        adapter_name="fixture",
        tier="t",
        make_adapter=lambda case: FixtureAdapter({"*": "no json here"}),
        root=tmp_path,
        engine_commit="abc1234",
    )
    assert report.failed == 3
    assert all(r.error for r in report.results)


def test_an_expectation_may_name_only_part_of_a_nested_reply(tmp_path: Path):
    """Row 7.13b. ``expect`` is a subset at the top level, and the same rule
    reads downward: a case on the grouper's contract states the GROUPING and
    says nothing about vendor, amount, and date, which are naming claims a
    model fills in however it reads the page."""
    _seed_nested_job(tmp_path)
    report = run_eval_set(
        "scan_group",
        model="m",
        adapter_name="openai_compat",
        tier="t",
        make_adapter=lambda case: FixtureAdapter({"*": NESTED_REPLY}),
        root=tmp_path,
        engine_commit="abc1234",
    )
    assert report.failed == 0, "the pages are under test; the filename claims are not"


def test_a_nested_expectation_fails_when_the_part_it_names_differs(tmp_path: Path):
    _seed_nested_job(tmp_path)
    wrong = json.dumps({"groups": [{"pages": [1]}, {"pages": [2, 3]}]})
    report = run_eval_set(
        "scan_group",
        model="m",
        adapter_name="openai_compat",
        tier="t",
        make_adapter=lambda case: FixtureAdapter({"*": wrong}),
        root=tmp_path,
        engine_commit="abc1234",
    )
    assert report.failed == 1
    check = report.results[0].checks[0]
    assert check.field == "groups"
    assert '"pages": [1, 2]' in check.expected, "a container reads as JSON in the record"
    assert '"vendor": ""' in check.actual, "the whole reply is recorded, not the projection"


def test_a_nested_case_seeds_the_fixture_adapter_from_its_own_expectation(tmp_path: Path):
    """What keeps a second field out of the case file: a nested ``expect`` is
    still a VALID partial reply, so the seeded fixture run answers with it
    and the contract fills the rest in."""
    _seed_nested_job(tmp_path)
    report = run_eval_set(
        "scan_group",
        model="fixture-model",
        adapter_name="fixture",
        make_adapter=seeded_fixture_adapter,
        root=tmp_path,
        engine_commit="abc1234",
    )
    assert report.failed == 0


def test_the_user_template_names_the_page_count_of_the_case_document(tmp_path: Path):
    """A job whose live ask states the page count (the grouper's does) gets
    it per case: ``{page_count}`` is the pages of that case's document, which
    the case text says with a form feed per page break."""
    _seed_nested_job(tmp_path)
    seen: list[str] = []

    class _Recorder:
        name = "fixture"

        def complete(self, bundle, schema):
            seen.append("\n".join(turn.content for turn in bundle.turns()))
            return RawReply(text=NESTED_REPLY)

    report = run_eval_set(
        "scan_group",
        model="m",
        adapter_name="openai_compat",
        tier="t",
        make_adapter=lambda case: _Recorder(),
        root=tmp_path,
        engine_commit="abc1234",
    )
    assert report.failed == 0
    assert "The attached scan has 3 pages." in seen[0]


# ---- the CLI ----------------------------------------------------------------------


def test_the_cli_runs_a_job_against_a_model_and_writes_the_results_file(
    tmp_path: Path, monkeypatch, capsys
):
    _seed_job(tmp_path)
    monkeypatch.setattr(eval_mod, "EVAL_SETS_ROOT", tmp_path)
    code = cli_main(["evals", "run", "invoice_extract", "--model", "fixture-model"])
    assert code == 0
    out = capsys.readouterr().out
    assert "3 of 3" in out
    assert (tmp_path / "invoice_extract" / "results" / "fixture-model.json").exists()


def test_the_cli_exits_non_zero_when_a_case_fails(tmp_path: Path, monkeypatch):
    directory = _seed_job(tmp_path)
    # an expectation the reply contract itself refuses: the seeded reply comes
    # back, pydantic rejects it, the case fails, the command fails.
    (directory / "cases" / "c0.json").write_text(
        json.dumps({"name": "c0", "text": "a thing", "expect": {"doc_type": "not-a-doc-type"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(eval_mod, "EVAL_SETS_ROOT", tmp_path)
    assert cli_main(["evals", "run", "invoice_extract", "--model", "fixture-model"]) == 1


def test_the_cli_lists_the_sets_and_counts_their_cases(tmp_path: Path, monkeypatch, capsys):
    _seed_job(tmp_path)
    monkeypatch.setattr(eval_mod, "EVAL_SETS_ROOT", tmp_path)
    assert cli_main(["evals", "list"]) == 0
    out = capsys.readouterr().out
    assert "invoice_extract" in out
    assert "3 case" in out


def test_the_eval_command_loads_a_tenant_the_gate_would_refuse(tmp_path: Path, monkeypatch):
    """The chicken and the egg: the command that produces the evidence must
    read the tenant whose model has none, so it loads that file UNGATED."""
    sets = tmp_path / "sets"
    _seed_job(sets)
    monkeypatch.setattr(eval_mod, "EVAL_SETS_ROOT", sets)
    tenants = tmp_path / "tenants"
    _tenant_file(
        tenants,
        "t6",
        tiers=UNPROVEN.replace("openai_compat", "fixture"),
        jobs='invoice_extract = "ghost"',
    )
    code = cli_main(
        [
            "evals",
            "run",
            "invoice_extract",
            "--tenant",
            "t6",
            "--tier",
            "ghost",
            "--tenants-root",
            str(tenants),
        ]
    )
    assert code == 0
    assert (sets / "invoice_extract" / "results" / "ghost-9.json").exists()
    assert load_tenant("t6", tenants_root=tenants).llm.jobs["invoice_extract"] == "ghost"
