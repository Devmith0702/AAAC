"""Simulated origin service time.

CLAUDE.md §4.1: a lognormal with median 60 ms and sigma 0.5. A fixed delay would
understate queueing effects and flatter the system, so the right tail is real and
is deliberately not truncated.

**Determinism (CLAUDE.md §3.10 rule 5).** Drawing from one shared RNG in arrival
order is *not* reproducible in a concurrent server, because arrival order varies
between runs. Each service time is therefore derived from ``hash(seed, index)``,
which replays exactly regardless of scheduling. The cost, which belongs in the
report: a retry for the same index number draws the *same* service time, so
per-record service time is correlated across attempts rather than independent.
"""

from __future__ import annotations

import hashlib
import math
from statistics import NormalDist

from aaac.origin.config import ServiceTimeConfig

_U64 = 1 << 64
_NORMAL = NormalDist()


def uniform01(*parts: object) -> float:
    """A deterministic uniform draw on the open interval (0, 1) from ``parts``.

    Stable across processes and Python versions, unlike :func:`hash`.
    """
    key = ":".join(str(p) for p in parts).encode("utf-8")
    digest = hashlib.blake2b(key, digest_size=8).digest()
    return (int.from_bytes(digest, "big") + 0.5) / _U64


def service_time_ms(cfg: ServiceTimeConfig, seed: int, index: int) -> float:
    """Service time in milliseconds for ``index`` under ``seed``.

    Lognormal with ``median = exp(mu)`` and shape ``sigma``.
    """
    z = _NORMAL.inv_cdf(uniform01("svc", seed, index))
    return cfg.median_ms * math.exp(cfg.sigma * z)


def theoretical_mean_ms(cfg: ServiceTimeConfig) -> float:
    """``exp(mu + sigma^2 / 2)`` — the mean, which exceeds the median."""
    return cfg.median_ms * math.exp(cfg.sigma**2 / 2.0)


def theoretical_quantile_ms(cfg: ServiceTimeConfig, q: float) -> float:
    """The ``q``-quantile of the configured distribution, for tests and the report."""
    if not 0.0 < q < 1.0:
        raise ValueError(f"q must be in (0, 1), got {q}")
    return cfg.median_ms * math.exp(cfg.sigma * _NORMAL.inv_cdf(q))
