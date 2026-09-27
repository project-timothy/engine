"""Engine entry-script evals: the scripts the schedule runs, wherever it runs.

The launchd host layer that ran these on one Mac moved to the tenant's own
repository on 2026-09-22 (gate 1 of the extraction) and its evals went with
it. What stays here pins the engine side of that seam: the off-box dead-man
library every entry script uses, the 08:00 daily script's stages, entry
and entry scripts deriving HOME from the environment.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
HC_LIB = SCRIPTS / "lib" / "hc-ping.sh"


# ---- Off-box dead-man (scripts/lib/hc-ping.sh, 2026-08-18) ------------------

ENTRY_SLUGS = {
    "auditor-nightly.sh": "acme-auditor-nightly",
    "engine-ap-daily.sh": "acme-engine-ap-daily",
    "ledger-backup.sh": "acme-ledger-backup",
}


def test_hc_ping_library_is_committed_and_entry_scripts_use_it():
    """Every scheduled entry script sources the ping library, pings start, and
    finishes with its exit code under a stable slug. The slugs are the contract
    with the healthchecks project; renaming one silently orphans the check.

    Row 7.20 built the slug from the tenant so a container can carry its own,
    so this now pins the two halves that reproduce the string. The URLs the
    library actually pings are pinned end to end in
    tests/unit/test_scheduled_scripts_linux.py."""
    assert HC_LIB.is_file()
    for name, slug in ENTRY_SLUGS.items():
        text = (SCRIPTS / name).read_text()
        suffix = slug.removeprefix("acme-")
        assert 'source "$REPO/scripts/lib/hc-ping.sh"' in text, name
        assert "require_env ENGINE_TENANT" in text, name
        assert 'TENANT="$ENGINE_TENANT"' in text, name
        assert f'HC_SLUG="$TENANT-{suffix}"' in text, name
        assert 'hc_ping "$HC_SLUG" start' in text, name
        assert 'hc_ping "$HC_SLUG" "$rc"' in text, name


class _Recorder(BaseHTTPRequestHandler):
    hits: list[tuple[str, str, bytes]] = []

    def _record(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        type(self).hits.append((self.command, self.path, body))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    do_GET = _record
    do_POST = _record

    def log_message(self, *_):  # silence
        pass


@pytest.fixture
def ping_server():
    _Recorder.hits = []
    server = HTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", _Recorder.hits
    finally:
        server.shutdown()


def _run_hc(script: str, env: dict) -> subprocess.CompletedProcess:
    full_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ["HOME"], **env}
    return subprocess.run(
        ["zsh", "-c", f'source "{HC_LIB}"; {script}'],
        capture_output=True,
        text=True,
        env=full_env,
        timeout=30,
    )


needs_zsh = pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh not on this runner")


@needs_zsh
def test_hc_ping_start_success_and_failure_urls(ping_server):
    base, hits = ping_server
    env = {"HC_PING_BASE": base + "/KEY", "ENGINE_HOST_ENV": "/nonexistent/host.env"}
    r = _run_hc(
        'hc_ping demo start; hc_ping demo 0 "all good"; hc_ping demo 78 "refused"; echo rc=$?', env
    )
    assert r.returncode == 0 and "rc=0" in r.stdout, r.stderr
    assert [(m, p) for m, p, _ in hits] == [
        ("GET", "/KEY/demo/start"),
        ("POST", "/KEY/demo"),
        ("POST", "/KEY/demo/78"),
    ]
    assert hits[1][2] == b"all good" and hits[2][2] == b"refused"


@needs_zsh
def test_hc_ping_is_a_silent_noop_without_configuration(ping_server, tmp_path):
    base, hits = ping_server
    env = {"ENGINE_HOST_ENV": str(tmp_path / "missing.env")}
    r = _run_hc("hc_ping demo start; hc_ping demo 1; echo rc=$?", env)
    assert r.returncode == 0 and "rc=0" in r.stdout
    assert r.stderr == ""
    assert hits == []


@needs_zsh
def test_hc_ping_reads_the_host_env_file(ping_server, tmp_path):
    base, hits = ping_server
    env_file = tmp_path / "host.env"
    env_file.write_text(f"HC_PING_BASE={base}/FROMFILE\n")
    r = _run_hc("hc_ping job 0; echo rc=$?", {"ENGINE_HOST_ENV": str(env_file)})
    assert r.returncode == 0 and "rc=0" in r.stdout
    assert [p for _, p, _ in hits] == ["/FROMFILE/job"]


@needs_zsh
def test_hc_ping_never_fails_the_caller_when_unreachable():
    env = {"HC_PING_BASE": "http://127.0.0.1:9/dead", "ENGINE_HOST_ENV": "/nonexistent"}
    r = _run_hc("hc_ping job 0; echo rc=$?", env)
    assert r.returncode == 0 and "rc=0" in r.stdout
    assert "hc-ping: could not reach" in r.stderr


# ---- 7.3: payment records and the statement file join the 08:00 run ----------


def test_daily_script_runs_payment_records_and_passes_the_statement_folder():
    """Row 7.3 takes 7.2 out of dark mode: ``qbo-push-payments`` runs between
    ``qbo-push`` and ``reconcile`` with its own exit code, and reconcile gets
    the tenant's statement folder (resolved from config, never a literal
    path) — the whole folder since row 7.4, not the newest export."""
    text = (SCRIPTS / "engine-ap-daily.sh").read_text()
    # The slug is "$TENANT", which the host names (require_env).
    push = text.index('engine run "$TENANT" ap qbo-push;')
    payments = text.index('engine run "$TENANT" ap qbo-push-payments')
    reconcile = text.index('engine run "$TENANT" ap reconcile')
    assert push < payments < reconcile
    assert "rc_qbopushpay=$?" in text
    assert "$rc_qbopushpay -eq 0" in text
    assert "qbopushpay=$rc_qbopushpay" in text
    assert "bank_csv.statement_dir" in text
    assert "statement_dir=" in text  # the folder is config; no bank is named here


# ---- 2026-09-13: the statement folder is read through the interpreter -------
#
# The daily run is /bin/zsh under launchd and the statement folder lives
# under ~/Desktop, a tree macOS TCC gates per program identity: stat by name
# works, readdir does not, and the denial is silent. The zsh glob #249
# shipped came back empty on the tier's first live run and reconcile logged
# the benign "no bank_csv param". Row 7.4 moved the listing INTO the job
# (2026-09-16), which runs under the interpreter the engine already reads
# that tree with; scripts/statement-export.py, the stopgap that did the
# listing from the shell, retired with it. A refused listing is now an
# ap.reconcile.statement_unreadable anomaly under a key that never replays
# (core/agents/ap/evals/test_reconcile_statement_folder.py).


def test_daily_script_hands_reconcile_the_whole_statement_folder():
    """No zsh glob over the Desktop tree anywhere in the daily run, and the
    folder comes from tenant config, never a literal path."""
    text = (SCRIPTS / "engine-ap-daily.sh").read_text()
    assert "*.csv(" not in text, "a zsh glob over the statement folder is the 2026-09-13 bug"
    assert "statement-export.py" not in text, "the stopgap retired with row 7.4"
    assert "statement_dir=$STATEMENT_DIR" in text
    assert "rc_statement=" in text
    assert "statement=$rc_statement" in text
    assert "$rc_statement -eq 0" in text
    assert not (SCRIPTS / "statement-export.py").exists()


# ---- Entry scripts derive HOME ----------------------------------------------


@pytest.mark.parametrize("name", ["auditor-nightly.sh", "engine-ap-daily.sh", "ledger-backup.sh"])
def test_entry_scripts_pin_home_from_the_environment(name: str):
    """HOME comes from launchd (or a container's cron), and every state path
    derives from it: no host name is typed into a script (row 7.25). Row 7.20
    replaced the per-user home fallback of a macOS layout, which was the last macOS
    layout assumption left in these three files. Since extraction the state
    paths are the host's to name too: no script carries a tenant's path."""
    text = (SCRIPTS / name).read_text()
    assert 'if [ -z "${HOME:-}" ]; then HOME=' in text
    assert "export HOME" in text
    assert "/Users/" not in text  # bleedthrough: allow (asserts absence)
    assert "ENGINE_TENANT:-" not in text  # no default tenant; the host names it
    assert "require_env ENGINE_TENANT" in text
