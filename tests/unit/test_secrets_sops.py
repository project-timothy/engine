"""SOPS and age: where a container's secrets live (phase 7 row 7.22).

Row 7.21 put the engine in a container and left the operator passing keys in
through `compose.yaml` or a plain file on the volume. Both are readable by
anything that can read the box. This row gives the tenant an ENCRYPTED file
beside its `tenant.toml`:

    tenants/<slug>/tenant.secrets.enc.yaml

decrypted by the entrypoint with an age identity that lives only on the box,
exported into the process environment BEFORE anything else runs, and never
written anywhere. `resolve_secret` is untouched: it reads `os.environ`, the
way it always has, and knows nothing about sops. That is the whole design —
the encryption is a delivery mechanism for the environment, not a second
source of truth.

What the tests here pin, in the order the acceptance names them:

* the entrypoint decrypts a fixture encrypted with a throwaway age key, and
  `resolve_secret` returns the value on the other side;
* the value appears in no log line the entrypoint writes;
* `engine doctor`'s four states (skip, each half missing, a failed decrypt,
  and ok with a COUNT and never a value);
* and the Mac, which has neither file nor key, is unchanged: `skip`, not
  MISSING, for both tenants that live in this repository.

The cases that need the `sops` binary skip where it is absent (this Mac has
no sops and nothing may be installed on it system-wide); CI runs them with
the binaries lifted out of the image the container job just built, so the
version under test is the version that ships.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from core.engine.config import TenantConfig, load_tenant
from core.engine.doctor import MISSING, OK, SKIP, run_doctor
from core.engine.init import init_tenant
from core.engine.secrets import AGE_KEY_ENV, SECRETS_FILENAME, probe, secrets_path

REPO = Path(__file__).resolve().parents[2]
ENTRYPOINT = REPO / "host" / "entrypoint.sh"

# The fixture secret. It has no recognisable shape on purpose: what proves it
# arrived is `resolve_secret`, and what proves it did not leak is a grep.
FIXTURE_VALUE = "canary-value-do-not-print-7f3a"

needs_sops = pytest.mark.skipif(
    shutil.which("sops") is None or shutil.which("age-keygen") is None,
    reason="no sops/age on this host (they ship in the container image; CI runs these)",
)


# ---- helpers -------------------------------------------------------------------


def _age_key(tmp_path: Path) -> tuple[Path, str]:
    """A throwaway identity, generated here and thrown away with tmp_path."""
    key = tmp_path / "age" / "keys.txt"
    key.parent.mkdir(parents=True, exist_ok=True)
    out = subprocess.run(["age-keygen", "-o", str(key)], capture_output=True, text=True, check=True)
    key.chmod(0o600)
    recipient = ""
    for line in (out.stderr + "\n" + key.read_text()).splitlines():
        if "age1" in line:
            recipient = line.split()[-1]
            break
    assert recipient.startswith("age1"), out.stderr
    return key, recipient


def _encrypt(tenant_dir: Path, recipient: str, values: dict[str, str]) -> Path:
    """Write `tenant.secrets.enc.yaml` the way `docs/credentials-checklist.md`
    tells the operator to: plain YAML in, sops out, plaintext never stored."""
    plain = tenant_dir / "_plaintext_fixture.yaml"
    plain.write_text("\n".join(f"{k}: {v}" for k, v in values.items()) + "\n", encoding="utf-8")
    done = subprocess.run(
        ["sops", "--encrypt", "--age", recipient, str(plain)],
        capture_output=True,
        text=True,
    )
    plain.unlink()
    assert done.returncode == 0, done.stderr
    path = tenant_dir / SECRETS_FILENAME
    path.write_text(done.stdout, encoding="utf-8")
    return path


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A tenant rendered the way a first boot renders it (row 7.19)."""
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "tenants"
    monkeypatch.setenv("ENGINE_TENANTS_ROOT", str(root))
    monkeypatch.setenv("ENGINE_LEDGER_ROOT", str(tmp_path / "ledger"))
    monkeypatch.setenv("AUDITOR_STORE_ROOT", str(tmp_path / "store"))
    monkeypatch.delenv(AGE_KEY_ENV, raising=False)
    init_tenant("acme", archetype="A", root=root, run_audit=False)
    return tmp_path, root


def _named(report, name):
    return next(c for c in report.checks if c.name == name)


# ---- the file's place ----------------------------------------------------------


def test_the_encrypted_file_sits_beside_the_tenant_file(world):
    """Beside `tenant.toml`, on the volume, so an image update never carries
    or overwrites one business's keys."""
    _, root = world
    assert secrets_path("acme", root) == root / "acme" / SECRETS_FILENAME
    assert SECRETS_FILENAME == "tenant.secrets.enc.yaml"


def test_resolve_secret_still_reads_only_the_environment():
    """The row's own constraint. sops delivers the environment; it is never a
    second place `resolve_secret` looks, or a tenant would resolve differently
    on a box with a file than on a box without one."""
    import inspect

    source = inspect.getsource(TenantConfig.resolve_secret)
    assert "os.environ" in source
    for word in ("sops", "decrypt", "yaml", "age_key"):
        assert word not in source.lower(), f"resolve_secret must not know about {word}"


# ---- doctor's four states ------------------------------------------------------


def test_neither_file_nor_key_is_a_skip_not_a_missing(world):
    """A tenant that keeps its secrets in the environment the ordinary way is
    a legal tenant, and this is every host that is not a container."""
    check = _named(run_doctor("acme", tenants_root=world[1], env={}), "secrets file")
    assert check.status == SKIP
    assert SECRETS_FILENAME in check.detail


def test_a_file_with_no_key_names_the_key(world, tmp_path):
    _, root = world
    (root / "acme" / SECRETS_FILENAME).write_text("sops: {}\n")
    check = _named(run_doctor("acme", tenants_root=root, env={}), "secrets file")
    assert check.status == MISSING
    assert AGE_KEY_ENV in check.detail


def test_a_key_with_no_file_names_the_file(world, tmp_path):
    _, root = world
    key = tmp_path / "keys.txt"
    key.write_text("# AGE-SECRET-KEY placeholder\n")
    check = _named(
        run_doctor("acme", tenants_root=root, env={AGE_KEY_ENV: str(key)}), "secrets file"
    )
    assert check.status == MISSING
    assert SECRETS_FILENAME in check.detail


@needs_sops
def test_a_file_that_will_not_decrypt_reports_the_first_line_of_the_error(world, tmp_path):
    """The one failure an operator actually hits: the wrong key on the box, or
    a file encrypted for somebody else. Doctor says which, in one line, and
    prints no file content."""
    _, root = world
    key, recipient = _age_key(tmp_path)
    _encrypt(root / "acme", recipient, {"ACME_QBO_CLIENT_SECRET": FIXTURE_VALUE})
    other, _ = _age_key(tmp_path / "other")
    check = _named(
        run_doctor("acme", tenants_root=root, env={AGE_KEY_ENV: str(other)}), "secrets file"
    )
    assert check.status == MISSING
    assert check.detail.strip() != ""
    assert "\n" not in check.detail, "one line, never a stack trace"
    assert FIXTURE_VALUE not in check.detail


@needs_sops
def test_a_file_that_decrypts_is_ok_with_a_count_and_no_values(world, tmp_path):
    _, root = world
    key, recipient = _age_key(tmp_path)
    _encrypt(
        root / "acme",
        recipient,
        {"ACME_QBO_CLIENT_ID": "id-42", "ACME_QBO_CLIENT_SECRET": FIXTURE_VALUE},
    )
    report = run_doctor("acme", tenants_root=root, env={AGE_KEY_ENV: str(key)})
    check = _named(report, "secrets file")
    assert check.status == OK
    assert "2" in check.detail, "the COUNT of keys, which is the useful number"
    text = "\n".join(report.lines())
    assert FIXTURE_VALUE not in text
    assert "id-42" not in text


@needs_sops
def test_doctor_names_a_declared_variable_the_file_does_not_carry(world, tmp_path):
    """The check the row asks for beyond the file itself: every variable
    `tenant.toml` declares has to come from somewhere, the environment or the
    file. A name in neither is an adapter that fails at 08:00."""
    _, root = world
    key, recipient = _age_key(tmp_path)
    _encrypt(root / "acme", recipient, {"ACME_QBO_CLIENT_ID": "id-42"})
    report = run_doctor("acme", tenants_root=root, env={AGE_KEY_ENV: str(key)})
    check = _named(report, "secrets coverage")
    assert check.status == MISSING
    assert "ACME_QBO_CLIENT_SECRET" in check.detail
    assert "ACME_QBO_CLIENT_ID" not in check.detail, "only what is missing is listed"


@needs_sops
def test_a_variable_already_in_the_environment_counts_as_covered(world, tmp_path):
    _, root = world
    key, recipient = _age_key(tmp_path)
    _encrypt(root / "acme", recipient, {"ACME_QBO_CLIENT_ID": "id-42"})
    report = run_doctor(
        "acme",
        tenants_root=root,
        env={AGE_KEY_ENV: str(key), "ACME_QBO_CLIENT_SECRET": "already-here"},
    )
    assert _named(report, "secrets coverage").status == OK


def test_the_coverage_check_is_out_of_scope_without_a_file(world):
    """No file, nothing to cover: the per-secret lines already say which
    variables are set, and this host is not using the encrypted lane."""
    check = _named(run_doctor("acme", tenants_root=world[1], env={}), "secrets coverage")
    assert check.status == SKIP
    assert f"no {SECRETS_FILENAME}" in check.detail


@needs_sops
def test_a_file_that_did_not_open_is_not_reported_as_no_file_at_all(world, tmp_path):
    """Found by running the container. Two different silences, and saying the
    wrong one is a lie an operator acts on: a host with no encrypted file, and
    a host whose file is right there and would not open."""
    _, root = world
    _, recipient = _age_key(tmp_path)
    _encrypt(root / "acme", recipient, {"ACME_QBO_CLIENT_SECRET": FIXTURE_VALUE})
    other, _ = _age_key(tmp_path / "other")
    check = _named(
        run_doctor("acme", tenants_root=root, env={AGE_KEY_ENV: str(other)}), "secrets coverage"
    )
    assert check.status == SKIP
    assert f"no {SECRETS_FILENAME}" not in check.detail
    assert "did not open" in check.detail


@needs_sops
def test_an_env_dict_without_a_path_still_finds_sops(world, tmp_path):
    """Found by CI. `env` is the host's VARIABLES, and doctor is routinely
    handed a narrow one; where to find a binary is a property of the process.
    Reading PATH out of that dict made every probe report "sops is not on this
    host" on a box where sops sits in /usr/local/bin, which is exactly where
    the image puts it."""
    _, root = world
    key, recipient = _age_key(tmp_path)
    _encrypt(root / "acme", recipient, {"ACME_QBO_CLIENT_SECRET": FIXTURE_VALUE})
    result = probe("acme", tenants_root=root, env={AGE_KEY_ENV: str(key)})
    assert result.status == OK, result.detail
    assert "not on this host" not in result.detail


@needs_sops
def test_probe_hands_back_names_and_never_values(world, tmp_path):
    """The boundary that keeps a value out of the doctor by construction:
    what crosses back is names and a count, not a mapping."""
    _, root = world
    key, recipient = _age_key(tmp_path)
    _encrypt(root / "acme", recipient, {"ACME_QBO_CLIENT_SECRET": FIXTURE_VALUE})
    result = probe("acme", tenants_root=root, env={AGE_KEY_ENV: str(key)})
    assert result.names == ("ACME_QBO_CLIENT_SECRET",)
    assert FIXTURE_VALUE not in repr(result)


@needs_sops
def test_a_per_secret_line_says_when_the_value_comes_from_the_file(world, tmp_path):
    """Found by running the container: `docker compose exec engine uv run
    engine doctor demo` reported every variable "declared, not set on this
    host" on a box whose scheduled jobs had all of them, because an exec does
    not inherit the entrypoint's environment. The file is the honest answer to
    where the value comes from, so the line says so."""
    _, root = world
    key, recipient = _age_key(tmp_path)
    _encrypt(root / "acme", recipient, {"ACME_QBO_CLIENT_SECRET": FIXTURE_VALUE})
    check = _named(
        run_doctor("acme", tenants_root=root, env={AGE_KEY_ENV: str(key)}),
        "secret qbo_client_secret",
    )
    assert check.status == OK
    assert SECRETS_FILENAME in check.detail
    assert FIXTURE_VALUE not in check.detail
    # And the one the file does NOT carry keeps the old answer.
    unset = _named(
        run_doctor("acme", tenants_root=root, env={AGE_KEY_ENV: str(key)}), "secret qbo_client_id"
    )
    assert unset.status == SKIP


# ---- the acceptance: the entrypoint boots and the value arrives -----------------


@needs_sops
def test_the_entrypoint_decrypts_into_the_environment_and_resolve_secret_finds_it(world, tmp_path):
    """The row's acceptance, end to end against the real `host/entrypoint.sh`:
    encrypt a fixture with a throwaway key, boot, and ask the tenant config
    for the secret on the other side. `exec "$@"` is the ops door row 7.21
    built, so the command below runs with exactly the environment the
    scheduled jobs get.

    The probe compares a DIGEST, not the value. Putting the expected value in
    the command line would have put it in the entrypoint's own `running: ...`
    line, and then the leak grep below would have been asserting against a
    string this test planted (found by CI, 2026-09-16)."""
    _, root = world
    key, recipient = _age_key(tmp_path)
    _encrypt(root / "acme", recipient, {"ACME_QBO_CLIENT_SECRET": FIXTURE_VALUE})
    expected = hashlib.sha256(FIXTURE_VALUE.encode()).hexdigest()

    probe_script = (
        "import hashlib, sys;"
        "from core.engine.config import load_tenant;"
        f"cfg = load_tenant('acme', tenants_root=__import__('pathlib').Path({str(root)!r}));"
        "value = cfg.resolve_secret('qbo_client_secret');"
        f"sys.exit(0 if hashlib.sha256(value.encode()).hexdigest() == {expected!r} else 9)"
    )
    done = subprocess.run(
        [str(ENTRYPOINT), sys.executable, "-c", probe_script],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "ENGINE_TENANT": "acme",
            "ENGINE_REPO": str(REPO),
            "ENGINE_DATA_ROOT": str(tmp_path / "data"),
            "ENGINE_TENANTS_ROOT": str(root),
            "ENGINE_LEDGER_ROOT": str(tmp_path / "ledger"),
            AGE_KEY_ENV: str(key),
            "PYTHONPATH": str(REPO),
        },
    )
    assert done.returncode == 0, f"{done.returncode}\n{done.stdout}\n{done.stderr}"
    assert FIXTURE_VALUE not in done.stdout, done.stdout
    assert FIXTURE_VALUE not in done.stderr, done.stderr
    # The NAME is in the boot log on purpose: "names, never values" is the
    # contract, and an operator reading `docker compose logs` has to be able to
    # see which variables the box got.
    assert "exported: ACME_QBO_CLIENT_SECRET" in done.stdout


@needs_sops
def test_the_entrypoint_writes_no_plaintext_anywhere_under_the_data_root(world, tmp_path):
    """ "Decrypts into the environment" means exactly that. A temporary file
    holding the plaintext would outlive the boot on the volume, which is the
    thing the encryption exists to prevent."""
    _, root = world
    key, recipient = _age_key(tmp_path)
    _encrypt(root / "acme", recipient, {"ACME_QBO_CLIENT_SECRET": FIXTURE_VALUE})
    data = tmp_path / "data"
    subprocess.run(
        [str(ENTRYPOINT), "true"],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "ENGINE_TENANT": "acme",
            "ENGINE_REPO": str(REPO),
            "ENGINE_DATA_ROOT": str(data),
            "ENGINE_TENANTS_ROOT": str(root),
            AGE_KEY_ENV: str(key),
        },
        check=True,
    )
    found = [
        p
        for p in data.rglob("*")
        if p.is_file() and FIXTURE_VALUE in p.read_text(encoding="utf-8", errors="replace")
    ]
    assert found == [], found


@needs_sops
def test_a_decrypt_failure_does_not_stop_the_box(world, tmp_path):
    """Same rule row 7.21 set for the doctor: a container that exits on a bad
    input is a crashloop, and the fix is on the volume the box is serving.
    The boot says so and carries on; doctor is where the operator reads it."""
    _, root = world
    _, recipient = _age_key(tmp_path)
    _encrypt(root / "acme", recipient, {"ACME_QBO_CLIENT_SECRET": FIXTURE_VALUE})
    other, _ = _age_key(tmp_path / "other")
    done = subprocess.run(
        [str(ENTRYPOINT), "true"],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "ENGINE_TENANT": "acme",
            "ENGINE_REPO": str(REPO),
            "ENGINE_DATA_ROOT": str(tmp_path / "data"),
            "ENGINE_TENANTS_ROOT": str(root),
            AGE_KEY_ENV: str(other),
        },
    )
    assert done.returncode == 0, done.stderr
    assert "secrets" in (done.stdout + done.stderr).lower()
    assert FIXTURE_VALUE not in done.stdout + done.stderr


# ---- nothing on this Mac changes -----------------------------------------------


@pytest.mark.parametrize("slug", ["demo"])
def test_the_tenants_in_this_repository_have_no_encrypted_file_and_doctor_skips(slug):
    """The row's hard constraint. This Mac has no secrets file and no age
    key; every launchd job keeps reading secrets from the environment exactly
    as before, and doctor must say `skip`, never MISSING, or a green host
    would start failing its own pre-flight."""
    root = REPO / "tenants"
    assert not (root / slug / SECRETS_FILENAME).exists()
    result = probe(slug, tenants_root=root, env={})
    assert result.status == SKIP
    assert result.names == ()
    # And with the variable set the way the image sets it, but no key on disk:
    # still nothing to do, because there is no file either.
    assert probe(slug, tenants_root=root, env={AGE_KEY_ENV: "/nope/keys.txt"}).status == SKIP


@pytest.mark.parametrize("slug", ["demo"])
def test_both_tenants_still_load(slug):
    """Cheap and blunt: the row touches `core/engine/config.py`, and the two
    tenant files in this repository must parse exactly as before."""
    assert load_tenant(slug, tenants_root=REPO / "tenants").secrets
