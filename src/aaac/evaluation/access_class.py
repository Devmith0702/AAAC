# ---------------------------------------------------------------------------
# STUB — DELETE THIS MODULE WHEN M1 SHIPS src/aaac/common/classes.py
#
# CLAUDE.md §1.4 permits a clearly-marked local stub inside my own package so
# that M3 is not blocked on M1. The enum below is transcribed *verbatim* from
# the immutable contract in CLAUDE.md §3.3 and must not be edited here: if the
# real one ever differs from this, the contract has been broken and that is a
# CONTRACT CHANGE conversation, not an edit to this file.
#
# When common/classes.py lands, delete this file and use:
#     from aaac.common.classes import AccessClass
# ---------------------------------------------------------------------------
"""Access classes (local stub of the shared contract)."""

from __future__ import annotations

from enum import IntEnum


class AccessClass(IntEnum):  # ordering matters: downgrade = HIGH -> MEDIUM -> LOW
    HIGH = 0
    MEDIUM = 1
    LOW = 2


#: The default whenever classification is unavailable or low-confidence (§3.3).
DEFAULT_ACCESS_CLASS = AccessClass.MEDIUM
