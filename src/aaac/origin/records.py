"""Synthetic exam-result records returned by the mock origin.

CLAUDE.md §4.1 asks for "a small JSON record (index number, name, subject/grade
list)". This emits that, plus the rest of the fields M2's delivery templates
render. That is not decoration: `delivery/variants.py` renders under Jinja's
``StrictUndefined``, so a record missing a field the template names is a 500 from
the delivery service and a client that transfers nothing. The narrower record
passed every unit test on both sides and still broke the first end-to-end run,
because only a real run puts M3's record into M2's template.

The field set and the ``subjects`` key therefore mirror
``delivery/variants.py:sample_record()``, which is M2's own statement of what a
result record looks like.

The *content* is cosmetic — nothing in the evaluation depends on the names or the
grade distribution. What matters is that a record is deterministic given
``(seed, index)`` so runs replay exactly (§3.10 rule 5), and that its size is
stable so byte accounting in §4.4 is not noisy.

Note the payload weight of the real 450 KB page is added by M2's delivery
service, not here.
"""

from __future__ import annotations

import hashlib
from typing import Any

_EXAM = "General Certificate of Education (Advanced Level)"
_YEAR = 2026
_ISSUED = "2026-09-06"

_FIRST_NAMES = (
    "Amara", "Bhagya", "Chamith", "Dilini", "Eranga", "Fathima", "Gayan", "Hasini",
    "Ishara", "Janith", "Kavindu", "Lakshmi", "Malith", "Nadeesha", "Oshadi", "Pasindu",
    "Ravindu", "Sanduni", "Tharindu", "Umesha", "Vihanga", "Wasana", "Yasiru", "Zahra",
    "Anuki", "Binara", "Chathura", "Damith", "Enoka", "Fernando", "Gimhani", "Harsha",
)

_LAST_NAMES = (
    "Perera", "Fernando", "Silva", "Jayawardena", "Bandara", "Rathnayake", "Gunasekara",
    "Wickramasinghe", "Dissanayake", "Herath", "Senanayake", "Ekanayake", "Weerasinghe",
    "Amarasekara", "Ranasinghe", "Karunaratne", "Abeysekara", "Wijesinghe", "Mendis",
    "Samarasinghe", "Kumara", "Rajapaksha", "Liyanage", "Peiris", "Gamage", "Nawaratne",
    "Thilakaratne", "Munasinghe", "Alwis", "Jayasuriya", "Hettiarachchi", "Wanigasooriya",
)

#: (subject, medium) pairs. Nine subjects, matching the size of a real A/L sheet
#: and M2's sample record — the variant budgets in §3.9 were measured against a
#: table this long, so a shorter one would flatter `full`.
_SUBJECTS = (
    ("Combined Mathematics", "English"),
    ("Physics", "English"),
    ("Chemistry", "English"),
    ("General English", "English"),
    ("General Information Technology", "English"),
    ("Common General Test", "Sinhala"),
    ("Information & Communication Technology", "English"),
    ("Economics", "Sinhala"),
    ("Biology", "English"),
)

_DISTRICTS = (
    ("Kandy", "Central College, Kandy"),
    ("Colombo", "Royal College, Colombo"),
    ("Galle", "Richmond College, Galle"),
    ("Jaffna", "Jaffna Central College"),
    ("Kurunegala", "Maliyadeva College, Kurunegala"),
    ("Matara", "Rahula College, Matara"),
    ("Badulla", "Uva College, Badulla"),
    ("Ratnapura", "Sivali Central College, Ratnapura"),
)

# 16-entry lookup so a single byte selects a grade with a plausible skew.
_GRADE_TABLE = "AAABBBBCCCCCSSWW"

#: A 'W' is a fail. Used only to derive the outcome line the templates print.
_FAIL_GRADES = "W"

_N_BYTES = 20


def _stream(seed: int, index: int) -> bytes:
    key = f"rec:{seed}:{index}".encode()
    return hashlib.blake2b(key, digest_size=_N_BYTES).digest()


def make_result_record(seed: int, index: int) -> dict[str, Any]:
    """Deterministic result record for ``index`` under ``seed``."""
    raw = _stream(seed, index)
    first = _FIRST_NAMES[raw[0] % len(_FIRST_NAMES)]
    last = _LAST_NAMES[raw[1] % len(_LAST_NAMES)]
    subjects = [
        {
            "subject": subject,
            "medium": medium,
            "grade": _GRADE_TABLE[raw[2 + i] % len(_GRADE_TABLE)],
        }
        for i, (subject, medium) in enumerate(_SUBJECTS)
    ]
    district, centre = _DISTRICTS[raw[11] % len(_DISTRICTS)]

    # Fixed-width by construction, so the record's size barely moves with the
    # index: z_score is always 6 characters, and the ranks are zero-padded.
    z_score = (raw[12] * 256 + raw[13]) / 65535 * 3.0
    district_rank = raw[14] * 256 + raw[15]
    island_rank = raw[16] * 65536 + raw[17] * 256 + raw[18]

    passed = all(s["grade"] not in _FAIL_GRADES for s in subjects)

    return {
        "exam": _EXAM,
        "year": _YEAR,
        "index_no": index,
        "name": f"{first} {last}",
        "centre": centre,
        "district": district,
        "subjects": subjects,
        "z_score": f"{z_score:.4f}",
        "district_rank": f"{district_rank % 1000:03d}",
        "island_rank": f"{island_rank % 100000:05d}",
        "outcome": (
            "Qualified for university admission"
            if passed
            else "Not qualified for university admission"
        ),
        "passed": passed,
        "issued": _ISSUED,
        "reference": f"AL{_YEAR}-{index}-R1",
    }
