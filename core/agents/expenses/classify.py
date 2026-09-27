"""Reference-vs-receipt detection.

The 2026-05-18 lesson: a single image with an empty body (a bucketing map,
a machine photo) is reference material, not a receipt; OCR'ing it as a
transaction invents money. The rule is deliberately narrow and deterministic;
anything it does not positively identify stays a receipt candidate and takes
the normal extraction path, where the extractor's own ``reference`` doc_type
is the second net.
"""

from __future__ import annotations


def is_reference_material(item: dict) -> bool:
    """True when the item is the reference-material shape, never a guess.

    A lone image carrying no body text is reference material. A scan with
    content — even a bad one — is not; it flows to extraction and review.
    """
    return bool(item.get("single_image")) and bool(item.get("body_empty"))
