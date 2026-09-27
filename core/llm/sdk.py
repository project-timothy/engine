"""The Claude Agent SDK is the optional extra ``[claude]`` (row 7.15).

``pip install .[claude]`` (``uv sync --extra claude``) installs it; the core
never imports it at module level. Every site that drives the SDK (the AP
extractor, the expenses inbox classifier and scan grouper, the runner's SDK
adapter) calls :func:`import_sdk` inside the function that needs it, so a
host running on API keys alone (``docs/model-seam-design.md``, the container proof)
loads every module and raises one typed, non-transient error only when a
job is actually pointed at the SDK.

The auditor keeps its own two-line copy of this seam
(``auditor/advisory/draft.py``): it imports nothing from ``core``.
"""

from __future__ import annotations

from types import ModuleType

EXTRA = "claude"
INSTALL_HINT = f"install it with the [{EXTRA}] extra: uv sync --extra {EXTRA}"


class SdkMissing(ModuleNotFoundError):
    """``claude_agent_sdk`` is not installed in this environment.

    A ``ModuleNotFoundError`` subclass so the runner's existing SKIPPED
    precondition and any ``except ModuleNotFoundError`` keep their meaning;
    the message names the extra to install. Never transient: no redial
    installs a package.
    """

    def __init__(self, site: str) -> None:
        super().__init__(
            f"{site} needs the Claude Agent SDK, which is not installed; {INSTALL_HINT}"
        )
        self.site = site


def import_sdk(site: str) -> ModuleType:
    """Import ``claude_agent_sdk`` for ``site`` (a short name for the error
    text) or raise :class:`SdkMissing`."""
    try:
        import claude_agent_sdk
    except ModuleNotFoundError as exc:
        raise SdkMissing(site) from exc
    return claude_agent_sdk
