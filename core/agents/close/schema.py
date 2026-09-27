"""Close agent contracts: check results and the preflight report."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

CheckStatus = Literal["OK", "WARN", "BLOCK", "TODO"]

MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

# Canonical check order (docs/closer-design.md). TODO entries hold the slot
# for checks that land in later build steps so the report never silently
# narrows.
CHECK_ORDER = [
    "machinery",
    "statement-anchor",
    "feed-acceptance",
    "ap-tie-out",
    "payroll",
    "owner-transactions",
    "categorization",
    "expenses",
    "ar-snapshot",
]


class CheckResult(BaseModel):
    name: str
    status: CheckStatus
    summary: str  # one evidence line
    details: list[str] = Field(default_factory=list)  # the to-do items, if any


class PreflightReport(BaseModel):
    tenant: str
    month: str  # YYYY-MM
    ran_at: str  # ISO timestamp
    checks: list[CheckResult] = Field(default_factory=list)

    @property
    def worst(self) -> CheckStatus:
        statuses = {c.status for c in self.checks}
        if "BLOCK" in statuses:
            return "BLOCK"
        if "WARN" in statuses:
            return "WARN"
        return "OK"

    @property
    def exit_code(self) -> int:
        return {"OK": 0, "WARN": 1, "BLOCK": 2}[self.worst]

    def counts(self) -> dict[str, int]:
        out = {"OK": 0, "WARN": 0, "BLOCK": 0, "TODO": 0}
        for check in self.checks:
            out[check.status] += 1
        return out
