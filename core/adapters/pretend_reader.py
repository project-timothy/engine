"""A pretend photo reader: reads an amount written into the photo's own
bytes after ``MARK`` ("43.00 USD"), for practice boxes and tests, until a
ministry chooses a real reader (core/contracts/reader.py). Any other photo
reads as nothing, so Tim asks how much. Standard library only."""

from __future__ import annotations

import re

from ..contracts.reader import Reading

MARK = b"TIM-READS:"
_READ = re.compile(rb"(\d+\.\d{2}) ([A-Z]{3})")


class PretendReader:
    def read(self, photo: bytes) -> Reading | None:
        at = photo.find(MARK)
        found = _READ.match(photo, at + len(MARK)) if at >= 0 else None
        return Reading(found[1].decode(), found[2].decode()) if found else None
