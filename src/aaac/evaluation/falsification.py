"""The pre-registered decision rule, in code (CLAUDE.md §4.7).

The rule itself is written out in ``README.md`` and was fixed before any full run.
This module is only its mechanical form. Two properties matter more than the
arithmetic:

* **It can return NOT SUPPORTED, and that path is tested.** §4.7 asks for the
  branch to be exercised against fabricated null data so that reporting a
  negative result is known to work before the real run rather than discovered
  afterwards. ``tests/test_falsification.py`` does exactly that.
* **Every condition reports its own verdict.** A single overall boolean would let
  a near-miss on origin stability disappear behind a large effect on Delta.

The margin in condition 3 is a pre-registered constant. Changing it in the same
edit as anything that improves a reported number is the move §1.5 forbids; it is
here as a named default so a change to it is visible in a diff.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from aaac.evaluation.stats import Interval, PairedTest, mean_ci_t, paired_t_test

#: Pre-registered. HIGH-class p95 time-to-completion under aaac may be at most
#: this much worse than baseline, in relative terms (§4.7, README).
HIGH_P95_MARGIN = 0.20

#: §4.5: "Minimum 5 seeds".
MIN_SEEDS = 5


@dataclass(frozen=True)
class Condition:
    """One clause of the decision rule, with the evidence behind it."""

    name: str
    passed: bool
    detail: str

    def __str__(self) -> str:
        return f"[{'PASS' if self.passed else 'FAIL'}] {self.name}: {self.detail}"


@dataclass
class Verdict:
    supported: bool
    conditions: list[Condition]
    delta_baseline: Interval | None = None
    delta_aaac: Interval | None = None
    paired_difference: Interval | None = None
    paired_test: PairedTest | None = None
    aggregate_completion_baseline: float | None = None
    aggregate_completion_aaac: float | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def headline(self) -> str:
        return "HYPOTHESIS SUPPORTED" if self.supported else "HYPOTHESIS NOT SUPPORTED"

    def failed_conditions(self) -> list[Condition]:
        return [c for c in self.conditions if not c.passed]


def evaluate(
    *,
    delta_baseline: Sequence[float],
    delta_aaac: Sequence[float],
    origin_err_baseline: Sequence[float],
    origin_err_aaac: Sequence[float],
    high_p95_baseline: Sequence[float],
    high_p95_aaac: Sequence[float],
    aggregate_completion_baseline: Sequence[float] = (),
    aggregate_completion_aaac: Sequence[float] = (),
    high_p95_margin: float = HIGH_P95_MARGIN,
    min_seeds: int = MIN_SEEDS,
) -> Verdict:
    """Apply the pre-registered rule. Every sequence is per-seed and paired."""
    n = len(delta_baseline)
    lengths = {
        len(delta_aaac), len(origin_err_baseline), len(origin_err_aaac),
        len(high_p95_baseline), len(high_p95_aaac), n,
    }
    if len(lengths) != 1:
        raise ValueError("all per-seed series must be the same length; runs are paired by seed")
    if n < 2:
        raise ValueError("need at least 2 seeds to form a paired interval")

    notes: list[str] = []
    if n < min_seeds:
        notes.append(
            f"Only {n} seeds. §4.5 requires a minimum of {min_seeds}; this verdict is "
            "under-powered and must not be presented as the headline result."
        )

    ci_baseline = mean_ci_t(delta_baseline)
    ci_aaac = mean_ci_t(delta_aaac)

    # Paired per-seed difference: positive means AAAC narrowed the gap.
    differences = [b - a for b, a in zip(delta_baseline, delta_aaac, strict=True)]
    paired_ci = mean_ci_t(differences)
    test = paired_t_test(delta_baseline, delta_aaac)

    conditions: list[Condition] = []

    narrowed = ci_aaac.mean < ci_baseline.mean
    excludes_zero = paired_ci.excludes_zero
    conditions.append(
        Condition(
            name="Delta reduced, paired 95% CI excludes zero",
            passed=narrowed and excludes_zero,
            detail=(
                f"mean Delta baseline {ci_baseline.mean:.4f}, aaac {ci_aaac.mean:.4f}; "
                f"paired difference {paired_ci} "
                f"({'excludes' if excludes_zero else 'includes'} zero); "
                f"paired t = {test.statistic:.3f}, p = {test.p_value:.4f}"
            ),
        )
    )

    mean_err_baseline = sum(origin_err_baseline) / n
    mean_err_aaac = sum(origin_err_aaac) / n
    conditions.append(
        Condition(
            name="Origin 5xx rate not worse under aaac",
            passed=mean_err_aaac <= mean_err_baseline,
            detail=(
                f"mean err rate baseline {mean_err_baseline:.6f}, "
                f"aaac {mean_err_aaac:.6f}"
            ),
        )
    )

    mean_p95_baseline = sum(high_p95_baseline) / n
    mean_p95_aaac = sum(high_p95_aaac) / n
    if mean_p95_baseline > 0:
        regression = (mean_p95_aaac - mean_p95_baseline) / mean_p95_baseline
    else:
        regression = 0.0 if mean_p95_aaac == 0 else float("inf")
    conditions.append(
        Condition(
            name=f"HIGH-class p95 TTC within {high_p95_margin:.0%} of baseline",
            passed=regression <= high_p95_margin,
            detail=(
                f"baseline {mean_p95_baseline:.2f}s, aaac {mean_p95_aaac:.2f}s "
                f"({regression:+.1%})"
            ),
        )
    )

    agg_baseline = agg_aaac = None
    if aggregate_completion_baseline and aggregate_completion_aaac:
        agg_baseline = sum(aggregate_completion_baseline) / len(aggregate_completion_baseline)
        agg_aaac = sum(aggregate_completion_aaac) / len(aggregate_completion_aaac)
        if agg_aaac < agg_baseline:
            # §4.7 flags this explicitly: it is a real finding, and it belongs in
            # the body of the report rather than a footnote.
            notes.append(
                f"Aggregate completion FELL under aaac ({agg_baseline:.4f} -> {agg_aaac:.4f}) "
                "while Delta was evaluated. Longer LOW windows consume admission "
                "throughput; if Delta narrowed here, it narrowed at least partly by "
                "making other classes worse. Report this in the body, not a footnote."
            )

    return Verdict(
        supported=all(c.passed for c in conditions),
        conditions=conditions,
        delta_baseline=ci_baseline,
        delta_aaac=ci_aaac,
        paired_difference=paired_ci,
        paired_test=test,
        aggregate_completion_baseline=agg_baseline,
        aggregate_completion_aaac=agg_aaac,
        notes=notes,
    )


def render(verdict: Verdict) -> str:
    lines = ["=" * 72, verdict.headline, "=" * 72, ""]
    for condition in verdict.conditions:
        lines.append(str(condition))
    if not verdict.supported:
        failed = ", ".join(c.name for c in verdict.failed_conditions())
        lines += ["", f"Failed condition(s): {failed}"]
    for note in verdict.notes:
        lines += ["", f"NOTE: {note}"]
    return "\n".join(lines)
