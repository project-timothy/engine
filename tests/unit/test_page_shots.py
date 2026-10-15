"""The pages CI photographs at a phone's width (tests/browser/pages.py,
structure review proposal 4) all render, every one Tim's own, so the photo
job never breaks for a reason a unit test could have caught."""

from __future__ import annotations

import importlib.util
from pathlib import Path

PAGES = Path(__file__).resolve().parents[1] / "browser" / "pages.py"


def _pages() -> dict[str, str]:
    spec = importlib.util.spec_from_file_location("page_shots", PAGES)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.pages()


def test_every_page_a_person_sees_renders_for_the_photos():
    pages = _pages()
    assert set(pages) == {"receipt", "catch-up", "month", "month-empty", "month-timed-out"}
    for name, html in pages.items():
        assert '<svg class="mark"' in html and "for Grace Fellowship" in html, name


def test_the_month_photo_shows_a_full_month_and_its_button():
    month = _pages()["month"]
    assert "NPR 4,300.00" in month and "Approve with Face ID" in month
    assert "Waiting on what it was for" in month
