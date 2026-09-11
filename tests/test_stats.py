"""Statistics, checked against published values rather than against themselves."""

from __future__ import annotations

import math

import pytest

from aaac.evaluation.stats import (
    bootstrap_ci,
    jains_index,
    mean_ci_t,
    paired_t_test,
    t_cdf,
    t_ppf,
    t_sf_two_sided,
    wilcoxon_min_achievable_p,
    wilcoxon_signed_rank,
)


@pytest.mark.parametrize(
    ("df", "expected"),
    [(1, 12.7062), (2, 4.3027), (4, 2.7764), (9, 2.2622), (29, 2.0452), (100, 1.9840)],
)
def test_t_critical_values_match_published_tables(df: int, expected: float) -> None:
    assert t_ppf(0.975, df) == pytest.approx(expected, abs=1e-3)


def test_t_cdf_is_symmetric_about_zero() -> None:
    for df in (1, 4, 30):
        assert t_cdf(0.0, df) == pytest.approx(0.5)
        assert t_cdf(-1.5, df) == pytest.approx(1.0 - t_cdf(1.5, df))


def test_two_sided_p_matches_the_critical_value() -> None:
    assert t_sf_two_sided(2.7764, 4) == pytest.approx(0.05, abs=1e-4)


def test_t_ppf_rejects_out_of_range() -> None:
    with pytest.raises(ValueError):
        t_ppf(0.0, 4)


def test_mean_ci_brackets_the_mean() -> None:
    interval = mean_ci_t([0.10, 0.11, 0.12, 0.13, 0.14])
    assert interval.low < interval.mean < interval.high
    assert interval.mean == pytest.approx(0.12)
    assert interval.n == 5


def test_single_observation_gives_an_undefined_interval() -> None:
    interval = mean_ci_t([0.5])
    assert math.isinf(interval.high) and math.isinf(interval.low)


def test_excludes_zero_detects_both_directions() -> None:
    assert mean_ci_t([0.10, 0.11, 0.12]).excludes_zero
    assert mean_ci_t([-0.10, -0.11, -0.12]).excludes_zero
    assert not mean_ci_t([-0.10, 0.0, 0.10]).excludes_zero


def test_bootstrap_is_deterministic_given_a_seed() -> None:
    values = [0.1, 0.3, 0.2, 0.9, 0.15]
    a = bootstrap_ci(values, n_resamples=500, seed=7)
    b = bootstrap_ci(values, n_resamples=500, seed=7)
    assert (a.low, a.high) == (b.low, b.high)


def test_bootstrap_brackets_the_mean() -> None:
    interval = bootstrap_ci([0.1, 0.12, 0.11, 0.13, 0.09], n_resamples=2000, seed=1)
    assert interval.low <= interval.mean <= interval.high


@pytest.mark.parametrize(
    ("values", "expected"),
    [([1, 1, 1], 1.0), ([1, 0, 0], 1 / 3), ([2, 2, 2, 2], 1.0), ([0, 0, 0], 1.0)],
)
def test_jains_index(values: list[float], expected: float) -> None:
    assert jains_index(values) == pytest.approx(expected)


def test_jains_index_is_lower_for_a_wider_spread() -> None:
    assert jains_index([0.95, 0.70, 0.30]) < jains_index([0.95, 0.90, 0.85])


def test_paired_t_detects_a_consistent_difference() -> None:
    baseline = [0.30, 0.32, 0.28, 0.31, 0.29]
    aaac = [0.10, 0.12, 0.09, 0.11, 0.10]
    result = paired_t_test(baseline, aaac)
    assert result.statistic > 0
    assert result.p_value < 0.01
    assert result.df == 4


def test_paired_t_finds_nothing_when_there_is_nothing() -> None:
    a = [0.30, 0.32, 0.28, 0.31, 0.29]
    b = [0.31, 0.29, 0.30, 0.30, 0.30]
    assert paired_t_test(a, b).p_value > 0.2


def test_paired_t_rejects_unequal_lengths() -> None:
    with pytest.raises(ValueError):
        paired_t_test([1.0, 2.0], [1.0])


def test_paired_t_handles_identical_series() -> None:
    result = paired_t_test([0.1, 0.2, 0.3], [0.1, 0.2, 0.3])
    assert result.p_value == 1.0


def test_wilcoxon_cannot_reject_at_five_pairs() -> None:
    # The point of the warning in stats.py: five seeds makes this test powerless
    # no matter how large the effect.
    assert wilcoxon_min_achievable_p(5) == pytest.approx(0.0625)
    huge = wilcoxon_signed_rank([1.0, 1.0, 1.0, 1.0, 1.0], [0.0, 0.0, 0.0, 0.0, 0.0])
    assert huge.p_value >= 0.0625
    assert "cannot reject" in huge.note


def test_wilcoxon_can_reject_with_more_pairs() -> None:
    a = [1.0] * 8
    b = [0.0] * 8
    assert wilcoxon_signed_rank(a, b).p_value < 0.05


def test_wilcoxon_drops_zero_differences_and_says_so() -> None:
    result = wilcoxon_signed_rank([1.0, 2.0, 3.0], [1.0, 1.0, 1.0])
    assert result.n == 2
    assert "zero difference" in result.note


def test_wilcoxon_with_all_zero_differences() -> None:
    result = wilcoxon_signed_rank([1.0, 2.0], [1.0, 2.0])
    assert result.p_value == 1.0
