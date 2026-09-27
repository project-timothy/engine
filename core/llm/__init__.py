"""The model seam (phase 7, section C).

Everything that touches a language model or a model-driven session lives
here. ``skill_harness`` is the fixture-driven regression harness for prose
skills (row 7.14; needs no model at all). ``core.llm.complete`` (row 7.8) is
the one call every model job makes; adapters under ``core.llm.adapters``
speak the wire formats. ``core.llm.policy`` (row 7.9) resolves a job type
through the tenant's ``[llm]`` tables and wraps ``complete`` so every call is
priced, recorded in ``llm_calls`` (``core.llm.telemetry``), and refused past
the monthly budget. Still unwired: rows 7.10 to 7.12 move the call sites
over. See ``docs/model-seam-design.md``.
"""

from .gateway import (
    Adapter,
    Attachment,
    CallRecord,
    DecimalString,
    GatewayError,
    GatewayResult,
    GatewaySchemaError,
    GatewayTransportError,
    GatewayValidationError,
    Message,
    Pricing,
    PromptBundle,
    RawReply,
    Usage,
    complete,
)

__all__ = [
    "Adapter",
    "Attachment",
    "CallRecord",
    "DecimalString",
    "GatewayError",
    "GatewayResult",
    "GatewaySchemaError",
    "GatewayTransportError",
    "GatewayValidationError",
    "Message",
    "Pricing",
    "PromptBundle",
    "RawReply",
    "Usage",
    "complete",
]
