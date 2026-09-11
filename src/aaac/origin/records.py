"""Synthetic exam-result records returned by the mock origin.

CLAUDE.md §4.1: "a small JSON record (index number, name, subject/grade list)".

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

_SUBJECTS = (
    "Sinhala", "English", "Mathematics", "Science", "History",
    "Religion", "Health & PE", "Art", "ICT",
)

# 16-entry lookup so a single byte selects a grade with a plausible skew.
_GRADE_TABLE = "AAABBBBCCCCCSSWW"

_N_BYTES = 2 + len(_SUBJECTS)


def _stream(seed: int, index: int) -> bytes:
    key = f"rec:{seed}:{index}".encode()
    return hashlib.blake2b(key, digest_size=_N_BYTES).digest()


def make_result_record(seed: int, index: int) -> dict[str, Any]:
    """Deterministic result record for ``index`` under ``seed``."""
    raw = _stream(seed, index)
    first = _FIRST_NAMES[raw[0] % len(_FIRST_NAMES)]
    last = _LAST_NAMES[raw[1] % len(_LAST_NAMES)]
    results = [
        {"subject": subject, "grade": _GRADE_TABLE[raw[2 + i] % len(_GRADE_TABLE)]}
        for i, subject in enumerate(_SUBJECTS)
    ]
    return {"index_no": index, "name": f"{first} {last}", "results": results}
