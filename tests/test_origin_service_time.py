"""Service-time distribution: determinism and shape."""

from __future__ import annotations

import statistics

import pytest

from aaac.origin.config import ServiceTimeConfig
from aaac.origin.service_time import (
    service_time_ms,
    theoretical_mean_ms,
    theoretical_quantile_ms,
    uniform01,
)

CFG = ServiceTimeConfig(dist="lognormal", median_ms=60.0, sigma=0.5)


def test_same_seed_and_index_replays_exactly() -> None:
    # CLAUDE.md §3.10 rule 5: deterministic given seed, independent of the
    # arrival order the concurrent server happens to produce.
    assert service_time_ms(CFG, 1, 42) == service_time_ms(CFG, 1, 42)


def test_different_seed_gives_different_draw() -> None:
    assert service_time_ms(CFG, 1, 42) != service_time_ms(CFG, 2, 42)


def test_different_index_gives_different_draw() -> None:
    assert service_time_ms(CFG, 1, 42) != service_time_ms(CFG, 1, 43)


def test_uniform01_is_strictly_inside_the_unit_interval() -> None:
    values = [uniform01("svc", 1, i) for i in range(5000)]
    assert all(0.0 < v < 1.0 for v in values)


def test_empirical_median_matches_configuration() -> None:
    draws = [service_time_ms(CFG, 1, i) for i in range(20_000)]
    assert statistics.median(draws) == pytest.approx(CFG.median_ms, rel=0.03)


@pytest.mark.parametrize("q", [0.10, 0.25, 0.75, 0.90, 0.99])
def test_empirical_quantiles_match_the_lognormal(q: float) -> None:
    draws = sorted(service_time_ms(CFG, 1, i) for i in range(20_000))
    empirical = draws[int(q * len(draws))]
    assert empirical == pytest.approx(theoretical_quantile_ms(CFG, q), rel=0.06)


def test_mean_exceeds_median_because_the_tail_is_right_skewed() -> None:
    # This is why §4.1 prefers a lognormal to a fixed delay: the right tail is
    # what produces realistic queueing.
    assert theoretical_mean_ms(CFG) > CFG.median_ms


def test_tail_is_not_truncated() -> None:
    draws = [service_time_ms(CFG, 1, i) for i in range(20_000)]
    assert max(draws) > 4 * CFG.median_ms


def test_theoretical_quantile_rejects_out_of_range() -> None:
    with pytest.raises(ValueError):
        theoretical_quantile_ms(CFG, 1.0)
