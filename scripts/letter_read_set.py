"""Build the interview letter reader's eval set: made-up IRS letters and what
each should read (Standalone Tim card R2, core/tools/letter_read.py).

    uv run python scripts/letter_read_set.py            # write the set
    uv run python scripts/letter_read_set.py --check    # rebuild in memory, compare (Mac)

Every letter below is invented: made-up ministries, made-up numbers (each
under a prefix the IRS issues, none a real organization's). Each is drawn as
a picture with no text layer, the way a phone sends it: clean, crooked,
faded, a letter that runs to a second page, and a letter to a business
rather than a nonprofit, which the screen must never offer as hers. The
drawing is ``scripts/picture_set.py``'s, shared with the receipt set; the
pictures are Mac-built and held by ``manifest.json`` (see there).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from picture_set import _STREAM, MANIFEST, ROOT, document, drawn, picture  # noqa: E402, F401
from picture_set import main as write_or_check  # noqa: E402

from core.tools.letter_read import JOB, SYSTEM, USER  # noqa: E402

SET = ROOT / "core" / "llm" / "eval_sets" / JOB
WIDTH = 400  # points: a letter's lines, in a typewriter face
MINIMUM = 5
"""Cases the set must hold (tests/unit/test_llm_eval_sets.py)."""

_IRS = [
    "Internal Revenue Service      Department of the Treasury",
    "P.O. Box 2508, Cincinnati OH 45201",
]


def _determination(date: str, ein: str, name: str, street: str, town: str) -> list[str]:
    return [
        *_IRS,
        f"Date: {date}",
        f"Employer Identification Number: {ein}",
        "",
        name,
        street,
        town,
        "",
        "Dear Applicant:",
        "We're pleased to tell you we determined you're exempt",
        "from federal income tax under Internal Revenue Code",
        "(IRC) Section 501(c)(3). Donors can deduct",
        "contributions they make to you under IRC Section 170.",
        "Letter 947 (Rev. 2-2020)",
    ]


# (name, look, the letter's lines (a "\f" line breaks the page), expect)
LETTERS: list[tuple[str, str, list[str], dict]] = [
    (
        "determination_clean",
        "plain",
        _determination(
            "March 14, 2024",
            "84-1234567",
            "Hope Springs Ministries Inc.",
            "1420 Juniper Lane",
            "Lebanon, TN 37087",
        ),
        {
            "organization": "nonprofit",
            "legal_name": "Hope Springs Ministries Inc.",
            "ein": "84-1234567",
        },
    ),
    (
        "ein_notice_nonprofit",
        "plain",
        [
            *_IRS,
            "Date of this notice: 06-02-2025",
            "Employer Identification Number: 47-5550123",
            "Form: SS-4   Type of organization: Nonprofit corporation",
            "",
            "Juniper Well Outreach, Inc.",
            "88 Mill Street",
            "Dayton, OH 45402",
            "",
            "WE ASSIGNED YOU AN EMPLOYER IDENTIFICATION NUMBER",
            "Thank you for applying for an Employer Identification",
            "Number (EIN). We assigned you EIN 47-5550123. If you",
            "plan to apply for tax-exempt status, file Form 1023.",
            "Notice CP 575",
        ],
        {
            "organization": "nonprofit",
            "legal_name": "Juniper Well Outreach, Inc.",
            "ein": "47-5550123",
        },
    ),
    (
        "determination_tilted",
        "tilted",
        _determination(
            "August 3, 2023",
            "61-2345678",
            "Quiet Harbor Mission",
            "PO Box 311",
            "Asheville, NC 28801",
        ),
        {"organization": "nonprofit", "legal_name": "Quiet Harbor Mission", "ein": "61-2345678"},
    ),
    (
        "determination_faded",
        "faded",
        _determination(
            "January 9, 2022",
            "27-3456789",
            "Tallgrass Lamp Ministries",
            "2207 Cedar Court",
            "Salina, KS 67401",
        ),
        {
            "organization": "nonprofit",
            "legal_name": "Tallgrass Lamp Ministries",
            "ein": "27-3456789",
        },
    ),
    (
        "determination_two_pages",
        "two_pages",
        [
            *_IRS,
            "Date: October 21, 2024",
            "Employer Identification Number: 82-4567891",
            "",
            "Willow Ford Fellowship Missions",
            "512 Harbor Road",
            "Columbus, OH 43215",
            "",
            "Dear Applicant:",
            "We're pleased to tell you we determined you're exempt",
            "from federal income tax under Internal Revenue Code",
            "(IRC) Section 501(c)(3). Donors can deduct",
            "(continued on next page)",
            "\f",
            "Willow Ford Fellowship Missions",
            "Page 2",
            "contributions they make to you under IRC Section 170.",
            "You're also qualified to receive tax deductible",
            "bequests, devises, transfers or gifts.",
            "Keep this letter for your permanent records.",
            "Letter 947 (Rev. 2-2020)",
        ],
        {
            "organization": "nonprofit",
            "legal_name": "Willow Ford Fellowship Missions",
            "ein": "82-4567891",
        },
    ),
    (
        "ein_notice_business",
        "plain",
        [
            *_IRS,
            "Date of this notice: 04-15-2025",
            "Employer Identification Number: 88-7654321",
            "Form: SS-4   Type of organization: Limited liability company",
            "",
            "Cole Family Coffee LLC",
            "40 Front Street",
            "Lebanon, TN 37087",
            "",
            "WE ASSIGNED YOU AN EMPLOYER IDENTIFICATION NUMBER",
            "Thank you for applying for an Employer Identification",
            "Number (EIN). We assigned you EIN 88-7654321. You",
            "must file Form 1065, U.S. Return of Partnership Income,",
            "each year.",
            "Notice CP 575",
        ],
        {"organization": "other"},
    ),
]


def build() -> dict[str, bytes]:
    """Every file of the set, by its path under the set's directory."""
    files: dict[str, bytes] = {}
    job = {
        "job": JOB,
        "output_model": "core.tools.letter_read:LetterReply",
        "decimal_fields": [],
        "prompt_source": "core/tools/letter_read.py SYSTEM and USER, as the interview's letter "
        "screen sends them; documents and cases built by scripts/letter_read_set.py",
        "built_by": "scripts/letter_read_set.py",
        "system": SYSTEM,
        "user": USER,
    }
    files["job.json"] = (json.dumps(job, indent=2) + "\n").encode()
    for name, look, lines, expect in LETTERS:
        file, data = document(name, look, lines, width=WIDTH)
        files[f"documents/{file}"] = data
        case = {
            "name": name,
            "note": f"a {look.replace('_', ' ')} IRS letter: whether it is to a nonprofit, and "
            "its name and number, read off the picture",
            "document": file,
            "text": "\n".join(lines),
            "expect": {"doc_type": "irs_letter", **expect},
        }
        files[f"cases/{name}.json"] = (
            json.dumps(case, indent=2, ensure_ascii=False) + "\n"
        ).encode()
    return files


def main(argv: list[str]) -> int:
    return write_or_check(argv, SET, build(), f"{len(LETTERS)} letters")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
