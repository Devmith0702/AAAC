from __future__ import annotations
from enum import IntEnum
from typing import Literal

class AccessClass(IntEnum):
    HIGH = 0
    MEDIUM = 1
    LOW = 2

def downgrade(c: AccessClass) -> AccessClass:
    """Downgrades HIGH to MEDIUM, MEDIUM to LOW, LOW stays LOW."""
    if c == AccessClass.HIGH:
        return AccessClass.MEDIUM
    return AccessClass.LOW

def variant_for(c: AccessClass) -> Literal["full", "reduced", "essential"]:
    """Maps AccessClass to its corresponding payload variant."""
    if c == AccessClass.HIGH:
        return "full"
    elif c == AccessClass.MEDIUM:
        return "reduced"
    return "essential"
