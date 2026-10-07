"""What this host is running: the package version and the commit (public #7).

The commit comes from the checkout the code sits in. A container has no
checkout (``.dockerignore`` drops ``.git``), and there the image reference in
``ENGINE_IMAGE`` is the review (docs/decisions/2026-09-16-an-image-is-a-
reviewed-checkout.md), so it stands in. ``core/engine/version.py`` is the same
logic written twice on purpose: this package imports nothing from ``core``.
"""

from __future__ import annotations

import argparse
import os
import subprocess
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version
from pathlib import Path

DISTRIBUTION = "timothy-engine"
SOURCE = Path(__file__).resolve().parents[1]


def package_version() -> str:
    try:
        return _dist_version(DISTRIBUTION)
    except PackageNotFoundError:
        return "unknown"


def commit(source: Path = SOURCE) -> str:
    """``commit <sha>`` from the checkout, else ``image <ref>``, else ``commit unknown``."""
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=source,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        sha = ""
    if sha:
        return f"commit {sha}"
    image = os.environ.get("ENGINE_IMAGE", "").strip()
    return f"image {image}" if image else "commit unknown"


def describe(prog: str, source: Path = SOURCE) -> str:
    return f"{prog} {package_version()} ({commit(source)})"


class VersionAction(argparse.Action):
    """``--version``, resolved when asked for: an ordinary run never forks git."""

    def __init__(
        self, option_strings, dest=argparse.SUPPRESS, default=argparse.SUPPRESS, help=None
    ):
        super().__init__(option_strings, dest=dest, default=default, nargs=0, help=help)

    def __call__(self, parser, *_):
        print(describe(parser.prog))
        parser.exit()
