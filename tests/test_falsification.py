"""The pre-registered decision rule (CLAUDE.md §4.7).

    Build that branch and test it against fabricated null data. The proposal
    commits to reporting a negative result; make it mechanically impossible to
    quietly avoid one.

That is what this file is for. The NOT SUPPORTED path is exercised on data where
AAAC does nothing, on data where it helps but not reliably, and on data where it
narrows Δ by making everyone worse.
"""

from __future__ import annotations

import pytest

from aaac.evaluation.falsification import HIGH_P95_MARGIN, evaluate, render

FIVE = 5
GOOD_DELTA_BASELINE = [0.30, 0.32, 0.28, 0.31, 0.29]
GOOD_DELTA_AAAC = [0.10, 0.12, 0.09, 0.11, 0.10]
NO_ERRORS = [0.0] * FIVE
SAME_P95 = [40.0] * FIVE


def verdict_for(**overrides: object):
    kwargs = {
        "delta_baseline": GOOD_DELTA_BASELINE,
        "delta_aaac": GOOD_DELTA_AAAC,
        "origin_err_baseline": NO_ERRORS,
        "origin_err_aaac": NO_ERRORS,
        "high_p95_baseline": SAME_P95,
        "high_p95_aaac": SAME_P95,
    }
    kwargs.update(overrides)
    return evaluate(**kwargs)  # type: ignore[arg-type]


def test_a_real_effect_is_supported() -> None:
    assert verdict_for().supported


# -- the branch that matters ----------------------------------------------


def test_null_data_is_not_supported() -> None:
    # AAAC changes nothing. This must report a negative result.
    null = [0.30, 0.32, 0.28, 0.31, 0.29]
    verdict = verdict_for(delta_aaac=list(null))
    assert not verdict.supported
    assert verdict.headline == "HYPOTHESIS NOT SUPPORTED"


def test_an_effect_too_noisy_to_call_is_not_supported() -> None:
    # Delta is lower on average but the paired CI spans zero.
    noisy = [0.05, 0.55, 0.02, 0.60, 0.10]
    verdict = verdict_for(delta_aaac=noisy)
    assert not verdict.supported
    assert verdict.paired_difference is not None
    assert not verdict.paired_difference.excludes_zero


def test_aaac_making_the_gap_worse_is_not_supported() -> None:
    verdict = verdict_for(delta_aaac=[0.50, 0.52, 0.48, 0.51, 0.49])
    assert not verdict.supported


def test_sacrificing_origin_stability_is_not_supported() -> None:
    verdict = verdict_for(origin_err_aaac=[0.05] * FIVE)
    assert not verdict.supported
    failed = [c.name for c in verdict.failed_conditions()]
    assert any("5xx" in name for name in failed)


def test_degrading_high_class_completion_time_is_not_supported() -> None:
    verdict = verdict_for(high_p95_aaac=[40.0 * (1.0 + HIGH_P95_MARGIN + 0.05)] * FIVE)
    assert not verdict.supported
    assert any("HIGH-class" in c.name for c in verdict.failed_conditions())


def test_a_regression_just_inside_the_margin_still_passes() -> None:
    verdict = verdict_for(high_p95_aaac=[40.0 * (1.0 + HIGH_P95_MARGIN - 0.01)] * FIVE)
    assert verdict.supported


def test_every_condition_reports_its_own_verdict() -> None:
    # A single overall boolean would let a near-miss disappear behind a large
    # effect elsewhere.
    verdict = verdict_for(origin_err_aaac=[0.05] * FIVE)
    assert len(verdict.conditions) == 3
    assert sum(1 for c in verdict.conditions if c.passed) == 2


# -- the trade-off §4.7 flags directly ------------------------------------


def test_a_narrower_gap_bought_by_lower_overall_completion_is_called_out() -> None:
    # "If aggregate completion falls while Δ narrows, that is a real finding and
    # it belongs in the body of the report, not a footnote."
    verdict = verdict_for(
        aggregate_completion_baseline=[0.90] * FIVE,
        aggregate_completion_aaac=[0.70] * FIVE,
    )
    assert verdict.supported  # the three conditions still hold
    assert any("Aggregate completion FELL" in note for note in verdict.notes)
    assert any("body, not a footnote" in note for note in verdict.notes)


def test_no_note_when_aggregate_completion_holds_up() -> None:
    verdict = verdict_for(
        aggregate_completion_baseline=[0.90] * FIVE,
        aggregate_completion_aaac=[0.93] * FIVE,
    )
    assert not any("FELL" in note for note in verdict.notes)


# -- guards ---------------------------------------------------------------


def test_too_few_seeds_is_flagged_as_under_powered() -> None:
    verdict = evaluate(
        delta_baseline=[0.3, 0.3, 0.3],
        delta_aaac=[0.1, 0.1, 0.1],
        origin_err_baseline=[0.0] * 3,
        origin_err_aaac=[0.0] * 3,
        high_p95_baseline=[40.0] * 3,
        high_p95_aaac=[40.0] * 3,
    )
    assert any("under-powered" in note for note in verdict.notes)


def test_unpaired_series_are_refused() -> None:
    with pytest.raises(ValueError, match="paired"):
        verdict_for(delta_aaac=[0.1, 0.2])


def test_a_single_seed_is_refused() -> None:
    with pytest.raises(ValueError, match="at least 2"):
        evaluate(
            delta_baseline=[0.3], delta_aaac=[0.1],
            origin_err_baseline=[0.0], origin_err_aaac=[0.0],
            high_p95_baseline=[40.0], high_p95_aaac=[40.0],
        )


def test_render_names_the_failing_conditions() -> None:
    output = render(verdict_for(origin_err_aaac=[0.05] * FIVE))
    assert "HYPOTHESIS NOT SUPPORTED" in output
    assert "Failed condition(s):" in output
    assert "[FAIL]" in output and "[PASS]" in output
