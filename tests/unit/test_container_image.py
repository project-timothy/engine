"""The container image, its compose file, and its entrypoint (row 7.21).

The image is the product's host (the 2026-09-11 decision): one
container, one data volume, `supercronic` reading the crontab row 7.21's first
half renders. This file is the contract the image cannot drift from without CI
saying so, and every assertion here is something that would otherwise be found
at 02:00 on somebody's VPS:

* the image installs WITHOUT the `[claude]` extra (row 7.26's exit criterion);
* supercronic is pinned by version AND by sha256, per architecture;
* the volume holds the tenant, the ledger, the auditor store, the reports and
  the logs, so `docker compose down` destroys nothing;
* the entrypoint renders the crontab, runs the doctor, and only then execs the
  scheduler, and it decrypts secrets into the environment BEFORE the doctor
  runs (the seam row 7.22 lands in);
* nothing in the image or the compose file carries a secret.

The cycle itself (supercronic firing the real scripts against the demo tenant)
is proven by the `container` job in .github/workflows/ci.yml and by the local
run recorded in the PR: it needs a Docker daemon, which the unit suite does
not assume.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO / "Dockerfile"
COMPOSE = REPO / "compose.yaml"
ENTRYPOINT = REPO / "host" / "entrypoint.sh"
DOCKERIGNORE = REPO / ".dockerignore"
INSTALL = REPO / "docs" / "install.md"
CHECKLIST = REPO / "docs" / "credentials-checklist.md"
CI = REPO / ".github" / "workflows" / "ci.yml"

SUPERCRONIC_VERSION = "v0.2.49"
SUPERCRONIC_SHA256 = {
    "amd64": "a53ae236602c7338aba3fbaff40bda6300eae3b9fedb8261eb06cfe3724430c1",
    "arm64": "02aa0cb229ba09050cba6638059dadb9eedc2276632ea43d6a57a2f8c1629dd5",
}


def _dockerfile() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


# ---- the image ----------------------------------------------------------------


def test_the_image_syncs_the_lockfile_without_the_claude_extra():
    """Row 7.26's exit criterion: API keys only, no Claude Code, no Max seat.
    A `--extra claude` here would put the SDK in the image and make the
    non-Mac proof a lie."""
    text = _dockerfile()
    syncs = [line.strip() for line in text.splitlines() if "uv sync" in line]
    assert syncs, "the image installs the locked environment"
    assert all("--locked" in line for line in syncs), syncs
    assert not any("--extra" in line for line in syncs), syncs
    live = [
        line
        for line in text.splitlines()
        if "claude" in line.lower() and not line.lstrip().startswith("#")
    ]
    assert live == [], f"a live line names the Claude extra: {live}"


def test_the_scripts_are_told_this_host_has_no_extras_and_no_optional_groups():
    """`uv run` syncs before it runs: without these the 08:00 job would ask uv
    to resolve packages the locked sync deliberately left out (the two knobs
    from scripts/lib/uv-run.sh). The group knob arrived with issue #280, which
    put the host group on every scheduled call; this image has no browser and
    must ask for none."""
    text = _dockerfile()
    assert re.search(r"^\s*ENGINE_UV_EXTRA=\s*(\\|$)", text, re.M), text
    assert re.search(r"^\s*ENGINE_UV_GROUP=\s*(\\|$)", text, re.M), text


def test_supercronic_is_pinned_by_version_and_by_checksum_per_architecture():
    text = _dockerfile()
    assert SUPERCRONIC_VERSION in text
    for arch, digest in SUPERCRONIC_SHA256.items():
        assert digest in text, f"no pinned sha256 for {arch}"
        assert arch in text
    assert "sha256sum -c" in text, "the checksum must be VERIFIED, not just recorded"
    assert "TARGETARCH" in text, "the architecture comes from the builder, never a guess"


def test_the_image_adds_no_python_dependency():
    """Invariant 9: the lockfile is the dependency list. The image may install
    OS packages; it may not pip install anything beside the lock."""
    text = _dockerfile()
    assert not re.search(r"\bpip install\b", text), text
    assert not re.search(r"uv (pip )?(add|install)\b", text), text


def test_the_image_declares_itself_to_the_freshness_guard():
    """There is no checkout in the image, so run-preflight.sh would refuse
    every job. ENGINE_IMAGE is how the image says the digest is the review
    (docs/decisions/2026-09-16-an-image-is-a-reviewed-checkout.md)."""
    assert "ENGINE_IMAGE" in _dockerfile()


def test_the_image_carries_no_other_tenant():
    """A distributed image must not carry one business's vendor list, paths,
    or Mac wiring. The tenant is rendered onto the volume at first boot."""
    ignored = DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
    lines = [line.strip() for line in ignored]
    assert "tenants/*" in lines, ignored
    assert {"!tenants/demo/", "!tenants/_templates/"} <= set(lines), ignored
    for noisy in (".git", ".venv", ".ledger", ".auditor"):
        assert any(line.strip().rstrip("/").endswith(noisy.lstrip(".")) for line in ignored), noisy


def test_the_entrypoint_is_the_image_entrypoint():
    assert re.search(r'ENTRYPOINT \["/app/host/entrypoint.sh"\]', _dockerfile())


# ---- the volume ----------------------------------------------------------------


def test_one_volume_holds_everything_that_must_survive_the_container():
    """The ledger, the auditor store, the reports, the logs and the tenant
    file itself. `docker compose down` must destroy nothing but the process."""
    text = COMPOSE.read_text(encoding="utf-8")
    assert re.search(r"^volumes:", text, re.M), text
    assert ":/data" in text, "the volume mounts at /data"
    for var in ("ENGINE_TENANT", "ENGINE_IMAGE", "ENGINE_UV_EXTRA", "ENGINE_UV_GROUP"):
        assert var in text, var


def test_the_compose_file_carries_no_secret():
    """Names only, never values (invariant: secrets resolve at runtime).
    The compose file is the thing people paste into a support thread."""
    text = COMPOSE.read_text(encoding="utf-8")
    for shape in ("sk-", "ANTHROPIC_API_KEY=sk", "hc-ping.com/"):
        assert shape not in text, f"{shape!r} in compose.yaml"
    assert "env_file" in text or "environment:" in text


@pytest.mark.skipif(shutil.which("docker") is None, reason="no docker on this host")
def test_the_compose_file_is_valid():
    result = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE), "config"],
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    assert result.returncode == 0, result.stderr


# ---- the entrypoint ------------------------------------------------------------


def _entrypoint() -> str:
    return ENTRYPOINT.read_text(encoding="utf-8")


def test_the_entrypoint_parses_under_bash():
    result = subprocess.run(["bash", "-n", str(ENTRYPOINT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_the_entrypoint_renders_the_crontab_then_execs_the_scheduler():
    """Order matters: a hand-edited crontab must not survive a restart, and
    supercronic must be exec'd (pid 1, so a `docker stop` reaches it)."""
    text = _entrypoint()
    # rindex: the header comment lists the same steps in the same order, so
    # the LAST occurrence of each is the line that actually runs.
    render = text.rindex("engine schedule")
    doctor = text.rindex("engine doctor")
    exec_line = text.rindex("exec supercronic")
    assert render < doctor < exec_line, "render, then report, then run"


def test_secrets_reach_the_environment_before_the_doctor_runs():
    """Row 7.22's seam. Doctor's whole job is to report what is missing; if
    the secrets arrived after it, it would report every one of them missing on
    a box that has them."""
    text = _entrypoint()
    assert "7.22" in text, "the seam says which row lands in it"
    assert text.index("secrets") < text.index("engine doctor")


def test_the_entrypoint_never_prints_a_secret():
    """`set -x` would put every decrypted value into docker logs."""
    assert not re.search(r"^\s*set -[a-z]*x", _entrypoint(), re.M)
    assert 'echo "$ENGINE_SECRETS' not in _entrypoint()


def test_a_doctor_that_finds_something_missing_does_not_stop_the_box():
    """A container that exits on a missing optional item is a crashloop, and
    the fix for most items is editing the tenant file the box is serving.
    Doctor reports into the container log and the loop still starts."""
    text = _entrypoint()
    assert re.search(r"engine doctor[^\n]*\|\|", text), text


def test_the_tenant_is_created_on_the_volume_at_first_boot():
    """`engine init` (row 7.19) is the first-boot step, and the tenant file
    lands on the volume so the owner can edit it."""
    text = _entrypoint()
    assert "engine init" in text
    assert "ENGINE_TENANTS_ROOT" in text


def test_the_fast_cycle_is_a_flag_not_a_second_entrypoint():
    """CI and a person watching a first install use the same entrypoint and
    the same scripts; only the cron expressions move (--every-minute)."""
    assert "--every-minute" in _entrypoint()


# ---- CI and the docs ------------------------------------------------------------


def test_ci_builds_the_image_and_runs_one_cycle():
    text = CI.read_text(encoding="utf-8")
    assert re.search(r"^\s{2}container:", text, re.M), "a job that builds the image"
    assert "docker compose" in text
    assert "engine doctor" in text, "CI asserts the doctor is green inside the container"
    assert "auditor-nightly.log" in text and "engine-ap-daily.log" in text, (
        "CI asserts both scheduled jobs actually ran"
    )
    # The first CI run of this job waited for a log FILE and caught a daily run
    # one second old, three stages in. A cycle is finished when each job has
    # written its own done line, not when its log exists.
    wait = text[text.index("Wait for one whole cycle") : text.index("The daily loop ran")]
    assert "=== done: mail=" in wait and "=== done: auditor=" in wait, wait
    assert "test -s" not in wait, "waiting on a file's existence is the race that failed"


def test_the_install_page_is_steps_only_and_fits_on_a_page():
    """The user-manual rule: numbered steps, no internals, no commands a
    person does not type."""
    text = INSTALL.read_text(encoding="utf-8")
    steps = re.findall(r"^\d+\. ", text, re.M)
    assert len(steps) >= 5, "an install is a numbered list"
    assert len(text.splitlines()) < 160, "one page"
    mac_home = "/Users/"  # bleedthrough: allow (asserts absence)
    assert mac_home not in text, "no host path from this Mac"
    assert "docker compose up -d" in text
    assert "engine doctor" in text


def test_an_argument_runs_that_command_instead_of_the_scheduler():
    """The ops door: `docker compose run --rm engine uv run engine queue list
    <tenant>` gets the same environment the scheduled jobs get, and rewrites
    no crontab."""
    text = _entrypoint()
    assert 'exec "$@"' in text
    assert text.index('exec "$@"') < text.rindex("engine schedule")


# ---- what the container found (the local cycle, 2026-09-16) ----------------------


def test_the_state_roots_are_in_the_image_environment_not_only_the_entrypoint():
    """Found by running the thing: `docker compose exec engine uv run engine
    doctor demo` reported the ledger MISSING at /app/.ledger/demo, because an
    exec does not go through the entrypoint and the entrypoint was the only
    place that exported the roots. Every process in the container has to see
    the same volume, or an operator's own command lies to them."""
    text = _dockerfile()
    for var in (
        "ENGINE_TENANTS_ROOT=/data/tenants",
        "ENGINE_LEDGER_ROOT=/data/ledger",
        "AUDITOR_STORE_ROOT=/data/auditor",
        "AUDITOR_TENANTS_DIR=/data/tenants",
        "LOG_DIR=/data/logs",
    ):
        assert var in text, f"{var} is not in the image environment"


# ---- row 7.22: sops and age, and the secrets the entrypoint decrypts -------------

SOPS_VERSION = "v3.13.3"
SOPS_SHA256 = {
    "amd64": "e5bec3346a873ae91d871550f3e698c1aad962aff462a080e40f25fde17fef6b",
    "arm64": "53b0abacd38ef1b12a66d6c100956691b9cefce018d91f81e73ddf7438b94d77",
}
AGE_VERSION = "v1.3.2"
AGE_SHA256 = {
    "amd64": "cbe24006683f8eb669266162894b9a522a1af52f2665fbc63a4bb032ed26ac10",
    "arm64": "6b8dc4333c53a5a57c9e5834e3a48f92605d7154014cd07269ff3327db5d37f4",
}


def test_sops_and_age_are_pinned_by_version_and_checksum_per_architecture():
    """The rule supercronic got: a retagged or replaced release fails the
    build instead of decrypting somebody's month-end with an unknown binary.
    sops publishes its own checksums.txt and both values here match it."""
    text = _dockerfile()
    assert SOPS_VERSION in text and AGE_VERSION in text
    for arch in ("amd64", "arm64"):
        assert SOPS_SHA256[arch] in text, f"no pinned sops sha256 for {arch}"
        assert AGE_SHA256[arch] in text, f"no pinned age sha256 for {arch}"
    verified = [line for line in text.splitlines() if "sha256sum -c" in line]
    assert len(verified) >= 3, "each download VERIFIES its checksum, never just records it"


def test_the_two_new_binaries_are_the_only_ones_this_row_adds():
    """A new external dependency is something the PR flags (CLAUDE.md). Two,
    both named by the plan row, both container-only: nothing on the Mac and
    nothing in pyproject.toml changes."""
    text = _dockerfile()
    assert "/usr/local/bin/sops" in text
    assert "age-keygen" in text, "the operator generates the key inside the container"
    assert not re.search(r"\bpip install\b", text)


def test_the_age_key_path_is_in_the_image_environment():
    """The 7.21 lesson again: `docker compose exec engine uv run engine doctor
    demo` does not go through the entrypoint, so a doctor that could not find
    the key would report a healthy box as broken."""
    assert "SOPS_AGE_KEY_FILE=/data/age/keys.txt" in _dockerfile()


def test_the_entrypoint_decrypts_with_sops_before_the_doctor_runs():
    text = _entrypoint()
    assert "sops" in text
    assert "tenant.secrets.enc.yaml" in text
    assert text.index("sops") < text.rindex("engine doctor")


def test_the_entrypoint_never_lets_the_plaintext_touch_disk():
    """Redirecting the decrypt into a file on the volume would leave every key
    of the install sitting in cleartext beside the ledger."""
    text = _entrypoint()
    decrypt = [line for line in text.splitlines() if "--decrypt" in line]
    assert decrypt, "the entrypoint decrypts"
    for line in decrypt:
        # Every stdout redirection on a sops line, if any, goes to /dev/null.
        # `2>` and `>&` are not stdout and the lookbehind steps over them.
        targets = re.findall(r"(?<![0-9&])>\s*(\S+)", line)
        assert all(target == "/dev/null" for target in targets), line
        assert " --output " not in line and " -o " not in line, line


def test_the_decrypted_names_are_printed_but_never_the_values():
    """A boot log is the first thing an operator pastes into a support
    thread."""
    text = _entrypoint()
    assert not re.search(r"^\s*set -[a-z]*x", text, re.M)
    assert not re.search(r"(echo|printf|say)[^\n]*\$\{?decrypted", text)


def test_ci_exercises_the_encrypted_lane_inside_the_image():
    """This Mac has no sops and nothing may be installed on it, so the cases
    that need the binaries skip here. CI is where they run, against the
    versions the image just pinned."""
    text = CI.read_text(encoding="utf-8")
    block = text[text.index("  container:") :]
    assert "age-keygen" in block, "CI generates a throwaway key"
    assert "sops" in block
    assert "test_secrets_sops.py" in block, "the skipped cases actually run somewhere"


# ---- what a real Linux host found (docs/non-mac-proof-2026-09-16.md) ----------

# The base image's own shell and coreutils. Everything else an operator is told
# to type has to be a binary the Dockerfile puts there, which is why the check
# below reads the Dockerfile instead of carrying a second list that can rot.
BASE_BINARIES = frozenset(
    {
        "sh",
        "cat",
        "chmod",
        "cp",
        "echo",
        "grep",
        "ls",
        "mkdir",
        "printf",
        "rm",
        "sed",
        "test",
    }
)


def _typed_in_the_container(page: Path) -> list[str]:
    """Every command a page tells someone to run inside the container, with
    shell line continuations folded first so the command word is the command
    word and not a backslash."""
    text = re.sub(r"\\\n\s*", " ", page.read_text(encoding="utf-8"))
    return re.findall(
        r"docker compose (?:exec|run --rm)\s+(?:-T\s+)?engine\s+(\S+)",
        text,
    )


def _the_image_has(binary: str) -> bool:
    return binary in BASE_BINARIES or bool(re.search(rf"\b{re.escape(binary)}\b", _dockerfile()))


def test_the_docs_never_type_a_binary_the_image_does_not_have():
    """Found by installing from this page on a Linux host that is not this Mac
    (docs/non-mac-proof-2026-09-16.md): step 3, the step that names the
    business, said `docker compose exec engine vi /data/tenants/demo/tenant.toml`.
    The image ships no editor, so the step died at the exec:

        OCI runtime exec failed: ... exec: "vi": executable file not found in $PATH

    A page may only tell an operator to type what the image can run, and the
    image is the Dockerfile, so that is what this reads."""
    for page in (INSTALL, CHECKLIST):
        for command in _typed_in_the_container(page):
            assert _the_image_has(command), (
                f"{page.name} tells an operator to run `{command}` in the container, "
                "and the Dockerfile does not put it there"
            )


def test_the_docs_never_document_a_git_remote_the_image_cannot_reach():
    """The same install, step 5: the ledger's remote was documented as
    `git@example.com:you/ledger.git`. git shells out to ssh for that form and
    the image has no ssh client, so the nightly push a fresh box is told to
    configure could never run:

        error: cannot run ssh: No such file or directory
        fatal: unable to fork

    Until openssh-client is in the image, the documented remote is https."""
    if re.search(r"\bopenssh-client\b", _dockerfile()):
        pytest.skip("the image carries an ssh client now; the ssh form is documentable again")
    for page in (INSTALL, CHECKLIST):
        text = page.read_text(encoding="utf-8")
        assert "git@" not in text, f"{page.name} documents an ssh remote"
        assert "ssh://" not in text, f"{page.name} documents an ssh remote"


def test_the_install_page_says_what_a_half_filled_secrets_file_does():
    """The same install again. Encrypting ONE of the demo tenant's two declared
    variables, which is what "store the keys the lanes you want need" invites,
    turned a green doctor red:

        MISSING  secrets coverage: declared in tenant.toml, in neither the
                 environment nor the encrypted file: DEMO_QBO_CLIENT_SECRET

    That is the doctor working as designed (coverage is the check that costs a
    business its 08:00 run), so the page is what has to say it."""
    text = INSTALL.read_text(encoding="utf-8")
    assert "secrets coverage" in text, "the page names the check an operator will watch go red"
