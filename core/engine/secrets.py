"""The encrypted secrets file, and what ``engine doctor`` can say about it
(phase 7 row 7.22).

Row 7.21 put the engine in a container and left the operator handing keys in
through ``compose.yaml`` or a plain file on the data volume. Both are readable
by anything that can read the box, and the second one sits beside the ledger
that is pushed to a backup remote every night. This row gives a tenant an
encrypted file instead:

    tenants/<slug>/tenant.secrets.enc.yaml

encrypted with `sops <https://github.com/getsops/sops>`_ to an `age
<https://github.com/FiloSottile/age>`_ recipient, decrypted by the container
entrypoint into the process environment before anything else runs, with the
age identity living only on the box (``SOPS_AGE_KEY_FILE``, mode 0600, never
in the image and never in a repository).

Three properties hold this design together:

* **``resolve_secret`` does not change and never will.** It reads
  ``os.environ``. The encrypted file is a delivery mechanism FOR the
  environment, not a second place to look, or the same tenant would resolve
  differently on a box that has a file than on one that does not. Every host
  that already provisions its variables the ordinary way (this engine's first
  host among them) is untouched by the whole row.
* **A value never crosses back into Python from here except to be exported.**
  :func:`probe` hands the doctor a status, a one-line detail, and the NAMES
  the file carries. There is no accessor that returns the mapping, so no
  future doctor line can print one by accident.
* **Nothing is installed to make this work.** ``sops`` is a binary in the
  container image; the engine shells out to it and reads its exit code. A
  host without ``sops`` and without a file is not broken, it is a host that
  does not use this lane, and :func:`probe` says ``skip``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import tenant_dir

SECRETS_FILENAME = "tenant.secrets.enc.yaml"
"""Beside ``tenant.toml``, on the volume. Encrypted, so it is safe in a
repository; the key that opens it is what never leaves the box."""

AGE_KEY_ENV = "SOPS_AGE_KEY_FILE"
"""sops' own variable, named by sops and not by this engine: whatever the
operator already knows about sops keeps working."""

SOPS = "sops"

OK = "ok"
MISSING = "missing"
SKIP = "skip"

_TIMEOUT_SECONDS = 20


class SopsUnavailable(RuntimeError):
    """``sops`` is not on this host's PATH."""


class SopsFailed(RuntimeError):
    """``sops`` ran and refused. ``first_line`` is its first line of stderr,
    which names the cause (wrong key, not a sops file, malformed) without
    carrying any file content."""

    def __init__(self, first_line: str) -> None:
        super().__init__(first_line)
        self.first_line = first_line


@dataclass(frozen=True)
class SecretsProbe:
    """What the doctor is allowed to know: a state, one line, and the NAMES.
    Never the values, by construction rather than by discipline."""

    status: str
    detail: str
    names: tuple[str, ...] = ()


def secrets_path(slug: str, tenants_root: str | Path | None = None) -> Path:
    root = Path(tenants_root) if tenants_root is not None else None
    return tenant_dir(slug, tenants_root=root) / SECRETS_FILENAME


def age_key_path(env: dict[str, str] | None = None) -> Path | None:
    """The age identity this host would decrypt with, or ``None`` when the
    variable is unset. Existence is a separate question."""
    environment = os.environ if env is None else env
    named = environment.get(AGE_KEY_ENV, "")
    return Path(named).expanduser() if named else None


def decrypt(path: Path, env: dict[str, str] | None = None) -> dict[str, str]:
    """``NAME -> value`` for every entry in the encrypted file.

    Shells out to ``sops --decrypt --output-type dotenv``. The plaintext lives
    in this process's memory and nowhere else: no temporary file, no shell
    history, no log line. Callers export it and drop it.

    ``env`` is the host's VARIABLES (which age identity, which names are
    already set). Where to find a binary is a property of this process, not of
    that dict, so ``PATH`` comes from the process when the caller did not pass
    one. Without that, a doctor handed a narrow environment reports "sops is
    not on this host" on a box where sops is in ``/usr/local/bin``, which is
    where the image puts it (found by CI, 2026-09-16).
    """
    environment = dict(os.environ if env is None else env)
    search = environment.get("PATH") or os.environ.get("PATH") or os.defpath
    binary = shutil.which(SOPS, path=search)
    if binary is None:
        raise SopsUnavailable(f"{SOPS} is not on this host")
    done = subprocess.run(
        [binary, "--decrypt", "--output-type", "dotenv", str(path)],
        capture_output=True,
        text=True,
        env={**environment, "PATH": search},
        timeout=_TIMEOUT_SECONDS,
    )
    if done.returncode != 0:
        first = next((ln.strip() for ln in done.stderr.splitlines() if ln.strip()), "")
        raise SopsFailed(first or f"{SOPS} exited {done.returncode}")
    values: dict[str, str] = {}
    for line in done.stdout.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        values[name.strip()] = value
    return values


def probe(
    slug: str,
    *,
    tenants_root: str | Path | None = None,
    env: dict[str, str] | None = None,
) -> SecretsProbe:
    """The four states ``engine doctor`` reports, and nothing else.

    ``skip``    neither the file nor the key is here: this host provisions its
                variables the ordinary way, which is every host that is not a
                container and any container whose operator prefers an env file.
    ``missing`` exactly one of the two is here, naming the other. Half a setup
                is the shape an install actually fails in.
    ``missing`` the pair is here and sops refused, carrying sops' own first
                line so the operator knows whether it is the wrong key or not
                a sops file at all.
    ``ok``      the count of names the file carries.
    """
    path = secrets_path(slug, tenants_root)
    key = age_key_path(env)
    has_file = path.is_file()
    has_key = key is not None and key.is_file()

    if not has_file and not has_key:
        # Precise on purpose: the image SETS ``SOPS_AGE_KEY_FILE`` to the path
        # an identity would live at, so "the variable is unset" would be a lie
        # on every fresh container. What is absent is the identity itself.
        where = str(key) if key is not None else f"the path {AGE_KEY_ENV} names"
        return SecretsProbe(
            SKIP,
            f"no {SECRETS_FILENAME} and no age identity at {where}: this host "
            "takes its secrets from the environment",
        )
    if has_file and not has_key:
        where = f" at {key}" if key is not None else ""
        return SecretsProbe(
            MISSING,
            f"{path} is here but the age identity {AGE_KEY_ENV}{where} is not: "
            "nothing on this host can open it",
        )
    if has_key and not has_file:
        return SecretsProbe(
            MISSING,
            f"an age identity is here but {SECRETS_FILENAME} is not, at {path}: "
            "encrypt one or unset " + AGE_KEY_ENV,
        )

    try:
        names = tuple(sorted(decrypt(path, env)))
    except SopsUnavailable as exc:
        return SecretsProbe(MISSING, f"{path} is here but {exc}")
    except SopsFailed as exc:
        return SecretsProbe(MISSING, f"{path} did not decrypt: {exc.first_line}")
    except subprocess.TimeoutExpired:
        return SecretsProbe(MISSING, f"{path} did not decrypt: sops timed out")
    return SecretsProbe(OK, f"{path} decrypts, carrying {len(names)} variable(s)", names=names)


def load_into(
    slug: str,
    *,
    tenants_root: str | Path | None = None,
    env: dict[str, str] | None = None,
) -> tuple[str, ...]:
    """Decrypt and export into ``os.environ``, returning the NAMES set.

    Not used by the container (its entrypoint does this in shell, before
    Python starts, so every scheduled process inherits it). It is here for a
    host that wants the same lane from inside a single command, and it never
    overwrites a variable the environment already carries: an operator's
    explicit ``-e NAME=value`` outranks the file.
    """
    values = decrypt(secrets_path(slug, tenants_root), env)
    set_names = []
    for name, value in values.items():
        if not os.environ.get(name):
            os.environ[name] = value
            set_names.append(name)
    return tuple(sorted(set_names))


__all__ = [
    "AGE_KEY_ENV",
    "SECRETS_FILENAME",
    "SecretsProbe",
    "SopsFailed",
    "SopsUnavailable",
    "age_key_path",
    "decrypt",
    "load_into",
    "probe",
    "secrets_path",
]
