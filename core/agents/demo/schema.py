"""Input/output contract for the demo agent (the "input contract" rubric point).

Validating the fixture against these models is what turns a malformed input
into a clean, surfaced error instead of garbage flowing downstream.
"""

from __future__ import annotations

from pydantic import BaseModel


class DemoItem(BaseModel):
    id: str
    value: int = 0


class DemoBatch(BaseModel):
    batch_id: str
    items: list[DemoItem]
