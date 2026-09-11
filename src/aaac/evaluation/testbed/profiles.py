"""Link profiles for the three access classes.

CLAUDE.md §4.2: "these parameterise everything and go verbatim in the report".
This module is the single source of truth. ``docker-compose.yml`` mirrors these
values into environment variables, and ``tests/test_testbed_profiles.py`` asserts
the two agree — a profile that drifts between the code and the compose file would
mean the report describes a testbed that was never run.

| Class | Rate | Delay ± jitter | Loss | Represents |
|---|---|---|---|---|
| HIGH | 50 Mbit | 15 ms ± 3 ms | 0.01% | urban fibre / good LTE |
| MEDIUM | 5 Mbit | 60 ms ± 20 ms | 0.5% | typical mobile broadband |
| LOW | 512 kbit | 250 ms ± 120 ms | 3% | congested rural cell |

**Where this deviates from the snippet in §4.2, and why.** The brief's example
uses ``burst 32kbit`` for every class. A token-bucket filter cannot reach its
configured rate if the burst is smaller than roughly ``rate / HZ``; at 50 Mbit
with HZ=1000 that floor is ~50 kbit, so a literal ``32kbit`` would cap the HIGH
class near 30 Mbit and the §4.2 verification gate would fail for a reason that
has nothing to do with the experiment. Burst is therefore scaled per class and
recorded here so the report states what was actually applied.
"""

from __future__ import annotations

from dataclasses import dataclass

from aaac.evaluation.access_class import AccessClass


@dataclass(frozen=True)
class LinkProfile:
    """One netem/tbf configuration, and the target the verifier checks against."""

    name: str
    access_class: AccessClass
    rate: str
    """tbf rate, in `tc` syntax (e.g. "512kbit")."""
    rate_kbps: float
    """The same rate in kbit/s, for the verifier's tolerance check."""
    delay_ms: float
    jitter_ms: float
    loss_pct: float
    burst: str
    represents: str

    @property
    def delay(self) -> str:
        return f"{self.delay_ms:g}ms"

    @property
    def jitter(self) -> str:
        return f"{self.jitter_ms:g}ms"

    @property
    def loss(self) -> str:
        return f"{self.loss_pct:g}%"

    def as_env(self) -> dict[str, str]:
        """Environment for ``netem.sh``. Mirrored in ``docker-compose.yml``."""
        return {
            "AAAC_NETEM_PROFILE": self.name,
            "AAAC_NETEM_RATE": self.rate,
            "AAAC_NETEM_DELAY": self.delay,
            "AAAC_NETEM_JITTER": self.jitter,
            "AAAC_NETEM_LOSS": self.loss,
            "AAAC_NETEM_BURST": self.burst,
        }


PROFILES: dict[AccessClass, LinkProfile] = {
    AccessClass.HIGH: LinkProfile(
        name="HIGH",
        access_class=AccessClass.HIGH,
        rate="50mbit",
        rate_kbps=50_000.0,
        delay_ms=15.0,
        jitter_ms=3.0,
        loss_pct=0.01,
        burst="256kbit",
        represents="urban fibre / good LTE",
    ),
    AccessClass.MEDIUM: LinkProfile(
        name="MEDIUM",
        access_class=AccessClass.MEDIUM,
        rate="5mbit",
        rate_kbps=5_000.0,
        delay_ms=60.0,
        jitter_ms=20.0,
        loss_pct=0.5,
        burst="64kbit",
        represents="typical mobile broadband",
    ),
    AccessClass.LOW: LinkProfile(
        name="LOW",
        access_class=AccessClass.LOW,
        rate="512kbit",
        rate_kbps=512.0,
        delay_ms=250.0,
        jitter_ms=120.0,
        loss_pct=3.0,
        burst="32kbit",
        represents="congested rural cell",
    ),
}

#: Tolerance for the §4.2 verification gate. Not a knob — widening it to make a
#: run pass is exactly the move §1.5 forbids.
VERIFY_TOLERANCE = 0.10


def profile_for(name_or_class: str | AccessClass) -> LinkProfile:
    """Look up a profile by ``AccessClass`` or by name ("HIGH", "low", ...)."""
    if isinstance(name_or_class, AccessClass):
        return PROFILES[name_or_class]
    try:
        return PROFILES[AccessClass[str(name_or_class).upper()]]
    except KeyError:
        raise KeyError(f"unknown link profile {name_or_class!r}") from None
