"""Shared fixtures for the eval suite."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

SEED_FIXTURES = Path(__file__).resolve().parent / "seed" / "fixtures"


@pytest.fixture
def seed_fixture():
    """Load a JSON fixture from the seed corpus by filename."""

    def _load(name: str):
        return json.loads((SEED_FIXTURES / name).read_text(encoding="utf-8"))

    return _load
