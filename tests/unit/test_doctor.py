"""``engine doctor <tenant>``: what is missing before this host can run (7.21).

The container has no owner watching a terminal. Everything it needs is either
present at boot or the first scheduled run fails at 02:00 with a stack trace
in a log file nobody reads. ``engine doctor`` is the pre-flight for the whole
install: the tenant file loads, the secrets the tenant DECLARES are in the
environment, the folders exist and are writable, the ledger is a git repo with
a remote when the nightly push is scheduled, the scheduler and its commands
are present.

Two rules the tests pin:

* **A secret's VALUE never appears in the output.** Doctor reads the variable
  NAME from ``tenant.toml`` and reports set or unset. The W-9 TIN discipline,
  extended to tokens.
* **Unconfigured is not missing.** A tenant with no mailbox and no accounting
  connection is a legal tenant: the lane is out of scope, doctor says so on a
  ``skip`` line, and the exit code stays 0. Doctor only fails on something the
  tenant asked for and did not get.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from core.engine.cli import main
from core.engine.doctor import run_doctor
from core.engine.init import init_tenant


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A tenants root beside a data root, with a tenant rendered into it the
    way a first boot does (row 7.19), and every state root in the tmp tree."""
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "tenants"
    monkeypatch.setenv("ENGINE_TENANTS_ROOT", str(root))
    monkeypatch.setenv("ENGINE_LEDGER_ROOT", str(tmp_path / "ledger"))
    monkeypatch.setenv("AUDITOR_STORE_ROOT", str(tmp_path / "store"))
    init_tenant("acme", archetype="A", root=root, run_audit=False)
    return tmp_path, root


def _edit(root: Path, replace: dict[str, str]) -> None:
    """Rewrite lines of the rendered tenant.toml (the owner's own edit)."""
    path = root / "acme" / "tenant.toml"
    text = path.read_text()
    for old, new in replace.items():
        assert old in text, f"{old!r} not in the rendered tenant.toml"
        text = text.replace(old, new)
    path.write_text(text)


def _report(world, **env):
    tmp_path, root = world
    return run_doctor("acme", tenants_root=root, env={**os.environ, **env})


def _named(report, name: str):
    return next(c for c in report.checks if c.name == name)


def _missing(report) -> list[str]:
    return [c.name for c in report.missing]


# ---- the green case -----------------------------------------------------------


def _give_the_ledger_a_remote(tmp_path: Path) -> None:
    """The one thing a fresh tenant genuinely owes: somewhere for the 23:00
    push to go. Written straight into the repository config, so this helper
    needs no subprocess and no network."""
    config = tmp_path / "ledger" / "acme" / ".git" / "config"
    config.write_text(config.read_text() + '\n[remote "origin"]\n\turl = /srv/backup.git\n')


def test_a_freshly_created_tenant_owes_exactly_one_thing(world):
    """``engine init`` builds every folder and the ledger, and the archetype
    configures no mailbox and no accounting connection, so none of those are
    owed. What IS owed is a remote for the nightly ledger push: that job is
    scheduled by default and would fail every night from the day the box is
    installed. Doctor says so on day one instead."""
    report = _report(world)
    assert _missing(report) == ["ledger remote"]


def test_with_the_ledger_backed_up_a_fresh_tenant_is_green(world):
    """Exit 0 on the first boot once the one gap is closed: the owner is told
    when something is missing, not always."""
    _give_the_ledger_a_remote(world[0])
    report = _report(world)
    assert _missing(report) == []
    assert report.ok is True


def test_a_declared_but_unused_secret_is_reported_and_never_fails_the_install(world):
    """[secrets] is a registry of NAMES (secrets.ref carries the same list),
    not a requirement list: the archetype declares the accounting variables
    before anything on the host consumes them. Doctor reports each one and
    fails on none of them; what it does fail on is a lane that IS configured
    and short of what it needs."""
    check = _named(_report(world), "secret qbo_client_id")
    assert check.status == "skip"
    assert "ACME_QBO_CLIENT_ID" in check.detail


def test_the_cli_exits_zero_when_nothing_is_missing(world, capsys):
    _, root = world
    _give_the_ledger_a_remote(world[0])
    assert main(["doctor", "acme", "--root", str(root)]) == 0
    out = capsys.readouterr().out
    assert "0 missing" in out


def test_the_cli_exits_nonzero_and_names_each_missing_item(world, capsys):
    _, root = world
    _edit(root, {'token_file = ""': 'token_file = "/nope/qbo-token.json"'})
    assert main(["doctor", "acme", "--root", str(root)]) == 1
    out = capsys.readouterr().out
    assert "MISSING" in out
    assert "/nope/qbo-token.json" in out


def test_an_unknown_tenant_is_a_usage_error(world, capsys):
    _, root = world
    assert main(["doctor", "nosuch", "--root", str(root)]) == 2


# ---- secrets: names, never values ---------------------------------------------


def test_a_declared_secret_that_is_set_is_ok(world):
    report = _report(world, ACME_QBO_CLIENT_ID="x", ACME_QBO_CLIENT_SECRET="y")
    assert _named(report, "secret qbo_client_id").status == "ok"
    assert "secret qbo_client_id" not in _missing(report)


def test_no_secret_value_ever_appears_in_the_output(world):
    """The invariant. A doctor that printed values would put every token of
    the install into a terminal, a screenshot, and a support thread."""
    report = _report(world, ACME_QBO_CLIENT_ID="sk-live-DO-NOT-PRINT-ME")
    text = "\n".join(report.lines())
    assert "DO-NOT-PRINT-ME" not in text
    assert "ACME_QBO_CLIENT_ID" in text


PROVIDER_TIER = (
    'strong = { adapter = "anthropic_messages", model = "a-real-model", '
    'api_key_env = "ACME_MODEL_KEY", pricing = { input_usd_per_mtok = "3", '
    'output_usd_per_mtok = "15" } }'
)


def test_a_model_tier_that_calls_a_provider_needs_its_key(world):
    """The fixture tier needs no key (it calls nobody), so a fresh tenant is
    green offline. A tier with a real adapter is owed the variable it names,
    whether or not a job points at it yet: the key is what the host has to
    provision, and provisioning it is the install step doctor exists to
    name."""
    _edit(world[1], {"[llm.jobs]": f"{PROVIDER_TIER}\n\n[llm.jobs]"})
    report = _report(world)
    assert "model tier strong" in _missing(report)
    assert "ACME_MODEL_KEY" in _named(report, "model tier strong").detail


def test_a_provider_tier_whose_key_is_set_is_ok(world):
    _edit(world[1], {"[llm.jobs]": f"{PROVIDER_TIER}\n\n[llm.jobs]"})
    report = _report(world, ACME_MODEL_KEY="sk-ant-NOT-PRINTED")
    assert _named(report, "model tier strong").status == "ok"
    assert "NOT-PRINTED" not in "\n".join(report.lines())


def test_the_fixture_tier_is_skipped_not_missing(world):
    assert _named(_report(world), "model tier fixture").status == "skip"


# ---- integrations: unconfigured is out of scope --------------------------------


def test_an_unconfigured_accounting_connection_is_a_skip(world):
    check = _named(_report(world), "accounting connection")
    assert check.status == "skip"
    assert "token_file" in check.detail


def test_a_configured_accounting_token_file_that_is_absent_is_missing(world):
    _edit(world[1], {'token_file = ""': 'token_file = "/nope/qbo-token.json"'})
    assert "accounting connection" in _missing(_report(world))


def test_a_configured_accounting_token_file_that_exists_is_ok(world, tmp_path):
    token = tmp_path / "qbo-token.json"
    token.write_text("{}")
    _edit(world[1], {'token_file = ""': f'token_file = "{token}"'})
    assert _named(_report(world), "accounting connection").status == "ok"


def test_an_unconfigured_mailbox_is_a_skip(world):
    assert _named(_report(world), "mailbox").status == "skip"


def test_a_half_configured_mailbox_names_the_keys_it_still_needs(world):
    _edit(world[1], {'client_id = ""': 'client_id = "11111111-2222-3333-4444-555555555555"'})
    check = _named(_report(world), "mailbox")
    assert check.status == "missing"
    assert "keychain_service" in check.detail


# ---- folders ------------------------------------------------------------------


def test_every_folder_the_tenant_names_is_checked(world):
    report = _report(world)
    names = [c.name for c in report.checks if c.name.startswith("folder ")]
    assert any("inbox" in n for n in names)
    assert any("_auditor" in n for n in names)
    assert all(c.status == "ok" for c in report.checks if c.name.startswith("folder "))


def test_a_folder_the_tenant_names_but_nobody_created_is_missing(world):
    tmp_path, root = world
    _edit(root, {'landing_dir = "acme-data/inbox"': 'landing_dir = "acme-data/nowhere"'})
    report = _report(world)
    assert "folder acme-data/nowhere" in _missing(report)
    assert "does not exist" in _named(report, "folder acme-data/nowhere").detail


def test_doctor_never_creates_a_folder_unless_asked(world):
    tmp_path, root = world
    _edit(root, {'landing_dir = "acme-data/inbox"': 'landing_dir = "acme-data/nowhere"'})
    _report(world)
    assert not (tmp_path / "acme-data" / "nowhere").exists()


def test_create_folders_makes_every_missing_folder_and_says_so(world):
    # Issue #3 (2026-10-06): a plain checkout had no tree and no way to get
    # one short of hand mkdirs; doctor already knows the list.
    tmp_path, root = world
    _edit(root, {'landing_dir = "acme-data/inbox"': 'landing_dir = "acme-data/nowhere"'})
    report = run_doctor("acme", tenants_root=root, create_folders=True)
    assert (tmp_path / "acme-data" / "nowhere").is_dir()
    check = _named(report, "folder acme-data/nowhere")
    assert check.status == "ok"
    assert "created" in check.detail


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the mode bits")
def test_a_folder_that_cannot_be_written_is_missing(world):
    tmp_path, _ = world
    folder = tmp_path / "acme-data" / "inbox"
    folder.chmod(0o500)
    try:
        report = _report(world)
        assert "folder acme-data/inbox" in _missing(report)
        assert "not writable" in _named(report, "folder acme-data/inbox").detail
    finally:
        folder.chmod(0o700)


# ---- the ledger and its backup remote ------------------------------------------


def test_the_ledger_is_a_git_repo(world):
    assert _named(_report(world), "ledger").status == "ok"


def test_a_ledger_root_that_does_not_exist_is_missing(world, tmp_path, monkeypatch):
    monkeypatch.setenv("ENGINE_LEDGER_ROOT", str(tmp_path / "gone"))
    assert "ledger" in _missing(_report(world))


def test_the_nightly_push_needs_a_remote(world):
    """``ledger-backup.sh`` runs ``git push origin main`` at 23:00. With the
    entry scheduled and no remote configured, that job fails every night from
    the day the box is installed; doctor says so on day one."""
    check = _named(_report(world), "ledger remote")
    assert check.status == "missing"
    assert "23" in check.detail or "ledger_backup" in check.detail


def test_a_ledger_with_a_remote_is_ok(world, tmp_path):
    _give_the_ledger_a_remote(tmp_path)
    assert _named(_report(world), "ledger remote").status == "ok"


def test_with_the_nightly_push_unscheduled_the_remote_is_out_of_scope(world):
    _edit(world[1], {'ledger_backup = "0 23 * * *"': 'ledger_backup = ""'})
    assert _named(_report(world), "ledger remote").status == "skip"


# ---- the scheduler -------------------------------------------------------------


def test_outside_a_container_supercronic_is_out_of_scope(world):
    """The Mac runs launchd and must never be told it is missing a Linux cron
    runner. The check applies when the host declares itself an image."""
    assert _named(_report(world), "supercronic").status == "skip"


def test_inside_an_image_supercronic_must_be_on_the_path(world):
    report = _report(world, ENGINE_IMAGE="engine:test", PATH="/nonexistent")
    assert "supercronic" in _missing(report)


def test_inside_an_image_a_present_supercronic_is_ok(world, tmp_path):
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "supercronic").write_text("#!/bin/sh\n")
    (fake / "supercronic").chmod(0o755)
    report = _report(world, ENGINE_IMAGE="engine:test", PATH=str(fake))
    assert _named(report, "supercronic").status == "ok"
    assert "engine:test" in _named(report, "image").detail


def test_every_scheduled_command_must_exist(world):
    """A crontab line naming a script that is not in the image is a job that
    fails forever, silently, once a day."""
    report = _report(world)
    assert _named(report, "schedule engine").status == "ok"
    _edit(
        world[1],
        {'build = ""': 'build = "0 4 * * *"', 'build_command = ""': 'build_command = "/nope/b.sh"'},
    )
    assert "schedule build" in _missing(_report(world))


# ---- the dead-man --------------------------------------------------------------


def test_dead_man_pings_are_out_of_scope_until_the_host_asks_for_them(world):
    assert _named(_report(world), "dead-man pings").status == "skip"


def test_a_host_that_asks_for_pings_needs_the_ping_base(world):
    _edit(world[1], {"healthchecks = false": "healthchecks = true"})
    check = _named(_report(world), "dead-man pings")
    assert check.status == "missing"
    assert "HC_PING_BASE" in check.detail


def test_the_ping_base_is_never_printed(world):
    _edit(world[1], {"healthchecks = false": "healthchecks = true"})
    report = _report(world, HC_PING_BASE="https://hc-ping.com/SECRET-PING-KEY")
    assert _named(report, "dead-man pings").status == "ok"
    assert "SECRET-PING-KEY" not in "\n".join(report.lines())


# ---- row 7.13's eval gate ------------------------------------------------------


def test_the_gated_model_jobs_are_reported(world):
    """The gate itself lives in ``load_tenant`` (row 7.13): reaching a report
    at all means every gated job's tier has green results. Doctor names which
    jobs that covered, so "it loaded" is not the only evidence."""
    check = _named(_report(world), "eval results")
    assert check.status == "ok"
    assert "invoice_extract" in check.detail


def test_a_tier_with_no_eval_results_is_one_named_item_not_a_stack_trace(world):
    """Pointing a gated job at an unproven model refuses the tenant file.
    Doctor is the command an installer runs when something is wrong, so it
    reports that as a missing item carrying the eval command, and exits 1."""
    _edit(world[1], {'model = "fixture-model"': 'model = "unproven-model"'})
    report = _report(world)
    assert _missing(report) == ["tenant config"]
    assert "engine evals run" in _named(report, "tenant config").detail


def test_the_cli_reports_an_unloadable_tenant_as_a_missing_item(world, capsys):
    _, root = world
    _edit(root, {'model = "fixture-model"': 'model = "unproven-model"'})
    assert main(["doctor", "acme", "--root", str(root)]) == 1
    assert "MISSING" in capsys.readouterr().out


def test_the_cli_names_create_folders_when_a_folder_is_missing(world, capsys):
    tmp_path, root = world
    _edit(root, {'landing_dir = "acme-data/inbox"': 'landing_dir = "acme-data/nowhere"'})
    assert main(["doctor", "acme"]) == 1
    assert "engine doctor acme --create-folders" in capsys.readouterr().err
