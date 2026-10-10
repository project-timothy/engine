"""The contracts package is real (2026-10-06).

CONTRIBUTING.md names ``core/contracts/`` as the Apache-2.0 hitch an
adapter is built against. That promise holds only while the directory
carries its own Apache LICENSE and imports nothing from the AGPL engine:
an import of engine behaviour would pull the adapter back under AGPL.
"""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2] / "core" / "contracts"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(("." * node.level) + (node.module or ""))
    return names


def test_the_package_carries_the_apache_license():
    licence = (PACKAGE / "LICENSE").read_text()
    assert "Apache License" in licence and "Version 2.0" in licence


def test_the_package_imports_nothing_from_the_engine():
    offenders = [
        f"{path.name}: {name}"
        for path in sorted(PACKAGE.glob("*.py"))
        for name in _imports(path)
        if name.startswith("core") or (name.startswith("..") and name != "..")
    ]
    assert offenders == [], offenders


def test_the_mail_shape_is_importable_on_its_own():
    from core.contracts.mail import MailClient

    assert {"list_messages", "get_body", "list_attachments", "download", "send_mail"} <= set(
        dir(MailClient)
    )


def test_the_model_adapter_shape_is_importable_on_its_own():
    """#344: the plug a provider adapter is written against."""
    from core.contracts.llm import (
        Adapter,
        Attachment,
        GatewayError,
        GatewayTransportError,
        Message,
        PromptBundle,
        RawReply,
        Usage,
    )

    assert "complete" in dir(Adapter)
    assert issubclass(GatewayTransportError, GatewayError)
    bundle = PromptBundle("t", "m", (Message("user", "hi"),), (), 30)
    assert bundle.turns() == (Message("user", "hi"),)
    assert RawReply("x").usage == Usage()
    assert Attachment.__dataclass_fields__.keys() == {"path", "mime"}


def test_the_gateway_re_exports_the_same_objects():
    """Old import paths keep working: the gateway's names ARE the contract's
    classes, so isinstance checks and except clauses see one type."""
    from core.contracts import llm
    from core.llm import gateway

    for name in (
        "Adapter",
        "Attachment",
        "GatewayError",
        "GatewayTransportError",
        "Message",
        "PromptBundle",
        "RawReply",
        "Role",
        "Usage",
    ):
        assert getattr(gateway, name) is getattr(llm, name), name
