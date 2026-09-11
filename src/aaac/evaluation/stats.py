"""Statistics for the experiment harness (CLAUDE.md §4.5).

§3.1 fixes the analysis stack at pandas + matplotlib. scipy is not on it, so the
few distributions needed here are implemented directly rather than smuggling in a
dependency. Everything is checked against published critical values in
``tests/test_stats.py``.

A WARNING ABOUT WILCOXON AT FIVE SEEDS
--------------------------------------
§4.5 offers "a paired t-test or Wilcoxon signed-rank" with "minimum 5 seeds".
Those two do not combine. The exact two-sided signed-rank test on n = 5 pairs has
only 2**5 = 32 equally likely sign assignments, so the smallest attainable
p-value is 2/32 = 0.0625. **Wilcoxon cannot reject at alpha = 0.05 with five
seeds, no matter how large the effect.** :func:`wilcoxon_min_achievable_p` makes
that explicit, and the harness refuses to report a Wilcoxon result it could never
have rejected. Either use the paired t-test (which is what §4.5's design points
at anyway) or run at least 6 seeds.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from itertools import product

# --------------------------------------------------------------------------
# distributions
# --------------------------------------------------------------------------

_TINY = 1e-30


def _betacf(a: float, b: float, x: float, itmax: int = 300, eps: float = 3e-16) -> float:
    """Continued fraction for the incomplete beta function (modified Lentz)."""
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < _TINY:
        d = _TINY
    d = 1.0 / d
    h = d
    for m in range(1, itmax + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < _TINY:
            d = _TINY
        c = 1.0 + aa / c
        if abs(c) < _TINY:
            c = _TINY
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < _TINY:
            d = _TINY
        c = 1.0 + aa / c
        if abs(c) < _TINY:
            c = _TINY
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def betainc(a: float, b: float, x: float) -> float:
    """Regularised incomplete beta ``I_x(a, b)``."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    log_beta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(log_beta + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    back = math.exp(log_beta + b * math.log1p(-x) + a * math.log(x))
    return 1.0 - back * _betacf(b, a, 1.0 - x) / b


def t_cdf(t: float, df: float) -> float:
    """P(T <= t) for Student's t with ``df`` degrees of freedom."""
    if df <= 0:
        raise ValueError(f"df must be > 0, got {df}")
    x = df / (df + t * t)
    tail = 0.5 * betainc(df / 2.0, 0.5, x)
    return 1.0 - tail if t > 0 else tail


def t_sf_two_sided(t: float, df: float) -> float:
    """Two-sided p-value for a t statistic."""
    if df <= 0:
        raise ValueError(f"df must be > 0, got {df}")
    if math.isinf(t):
        return 0.0
    return betainc(df / 2.0, 0.5, df / (df + t * t))


def t_ppf(p: float, df: float) -> float:
    """Inverse CDF by bisection. Accurate to ~1e-10, which is ample here."""
    if not 0.0 < p < 1.0:
        raise ValueError(f"p must be in (0, 1), got {p}")
    lo, hi = -1e4, 1e4
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if t_cdf(mid, df) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


# --------------------------------------------------------------------------
# summaries
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Interval:
    """A point estimate with a confidence interval and how it was produced."""

    mean: float
    low: float
    high: float
    n: int
    method: str

    @property
    def excludes_zero(self) -> bool:
        return self.low > 0.0 or self.high < 0.0

    def __str__(self) -> str:
        return f"{self.mean:.4f} [{self.low:.4f}, {self.high:.4f}]"


def mean_ci_t(values: Sequence[float], conf: float = 0.95) -> Interval:
    """Mean with a t-distribution confidence interval (§4.5)."""
    n = len(values)
    if n == 0:
        raise ValueError("mean_ci_t needs at least one value")
    mean = sum(values) / n
    if n == 1:
        return Interval(mean, -math.inf, math.inf, 1, "t (n=1, undefined)")
    variance = sum((v - mean) ** 2 for v in values) / (n - 1)
    se = math.sqrt(variance / n)
    crit = t_ppf(1.0 - (1.0 - conf) / 2.0, n - 1)
    return Interval(mean, mean - crit * se, mean + crit * se, n, f"t, df={n - 1}")


def bootstrap_ci(
    values: Sequence[float],
    statistic: Callable[[Sequence[float]], float] | None = None,
    *,
    conf: float = 0.95,
    n_resamples: int = 10_000,
    seed: int = 0,
) -> Interval:
    """Percentile bootstrap CI. Use when the distribution is visibly skewed (§4.5)."""
    n = len(values)
    if n == 0:
        raise ValueError("bootstrap_ci needs at least one value")
    stat = statistic or (lambda xs: sum(xs) / len(xs))
    rng = random.Random(seed)
    draws = sorted(
        stat([values[rng.randrange(n)] for _ in range(n)]) for _ in range(n_resamples)
    )
    alpha = (1.0 - conf) / 2.0
    low = draws[int(alpha * n_resamples)]
    high = draws[min(n_resamples - 1, int((1.0 - alpha) * n_resamples))]
    return Interval(stat(values), low, high, n, f"bootstrap, {n_resamples} resamples")


def jains_index(values: Sequence[float]) -> float:
    """``J = (sum x)^2 / (n * sum x^2)`` — 1.0 is perfectly fair (§4.4)."""
    n = len(values)
    if n == 0:
        raise ValueError("jains_index needs at least one value")
    total = sum(values)
    sq = sum(v * v for v in values)
    if sq == 0:
        # Every class completed nothing. Perfectly equal, and perfectly useless —
        # the caller must report the completion rates alongside this number.
        return 1.0
    return (total * total) / (n * sq)


# --------------------------------------------------------------------------
# paired tests
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PairedTest:
    name: str
    statistic: float
    p_value: float
    n: int
    df: float | None
    note: str = ""


def paired_differences(a: Sequence[float], b: Sequence[float]) -> list[float]:
    if len(a) != len(b):
        raise ValueError(f"paired data must be the same length, got {len(a)} and {len(b)}")
    if not a:
        raise ValueError("paired data must be non-empty")
    return [x - y for x, y in zip(a, b, strict=True)]


def paired_t_test(a: Sequence[float], b: Sequence[float]) -> PairedTest:
    """Paired t-test on ``a - b``.

    §4.5: modes share a seed, so this is the correct comparison and it is far
    more powerful than treating the two sets of runs as independent.
    """
    diffs = paired_differences(a, b)
    n = len(diffs)
    if n < 2:
        raise ValueError("paired_t_test needs at least 2 pairs")
    mean = sum(diffs) / n
    variance = sum((d - mean) ** 2 for d in diffs) / (n - 1)
    if variance == 0.0:
        note = "zero variance in the differences; p is 0 or 1 by construction"
        return PairedTest("paired t", math.inf if mean else 0.0,
                          0.0 if mean else 1.0, n, n - 1, note)
    t = mean / math.sqrt(variance / n)
    return PairedTest("paired t", t, t_sf_two_sided(t, n - 1), n, n - 1)


def wilcoxon_min_achievable_p(n: int) -> float:
    """Smallest two-sided p the exact signed-rank test can produce with ``n`` pairs."""
    if n < 1:
        raise ValueError(f"n must be >= 1, got {n}")
    return min(1.0, 2.0 / (2.0**n))


def wilcoxon_signed_rank(a: Sequence[float], b: Sequence[float]) -> PairedTest:
    """Exact two-sided Wilcoxon signed-rank test on ``a - b``.

    Zero differences are dropped, which is the standard treatment and is recorded
    in the returned note rather than passed over. Exact enumeration is used up to
    20 pairs; beyond that the normal approximation takes over.
    """
    diffs = [d for d in paired_differences(a, b) if d != 0.0]
    dropped = len(a) - len(diffs)
    n = len(diffs)
    if n == 0:
        return PairedTest("wilcoxon", 0.0, 1.0, 0, None, "every difference was exactly zero")

    magnitudes = sorted((abs(d), i) for i, d in enumerate(diffs))
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and magnitudes[j + 1][0] == magnitudes[i][0]:
            j += 1
        average = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[magnitudes[k][1]] = average
        i = j + 1

    w_plus = sum(r for d, r in zip(diffs, ranks, strict=True) if d > 0)
    total = sum(ranks)
    observed = min(w_plus, total - w_plus)

    note = f"{dropped} zero difference(s) dropped" if dropped else ""
    floor = wilcoxon_min_achievable_p(n)
    if floor > 0.05:
        extra = (
            f"n={n}: the smallest attainable two-sided p is {floor:.4f}, so this test "
            "cannot reject at 0.05 regardless of effect size"
        )
        note = f"{note}; {extra}" if note else extra

    if n <= 20:
        atleast = 0
        for signs in product((0, 1), repeat=n):
            plus = sum(r for s, r in zip(signs, ranks, strict=True) if s)
            if min(plus, total - plus) <= observed + 1e-12:
                atleast += 1
        p = atleast / (2**n)
    else:  # pragma: no cover - the experiment runs far fewer seeds than this
        mean_w = total / 2.0
        sd_w = math.sqrt(sum(r * r for r in ranks) / 4.0)
        z = (observed - mean_w) / sd_w if sd_w else 0.0
        p = 2.0 * (1.0 - 0.5 * (1.0 + math.erf(abs(z) / math.sqrt(2.0))))

    return PairedTest("wilcoxon", observed, min(1.0, p), n, None, note)
