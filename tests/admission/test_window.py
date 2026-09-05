"""Tests for admission/window.py — C2 (Adaptive Admission Window).

These tests verify the core invariants of the window module:
1. aaac mode produces class-differentiated windows via kappa multipliers
2. baseline mode produces a flat window regardless of class
3. Windows are clamped to w_max_s
4. windows_for_all() returns the correct dict format for store.admit_n()
5. weighted_mean_window() correctly weights by waiting ticket counts
"""
from __future__ import annotations
import pytest
from aaac.common.classes import AccessClass
from aaac.common.config import AdmissionConfig
from aaac.admission.window import window_for, windows_for_all, weighted_mean_window


# ---------------------------------------------------------------------------
# Fixtures — mirror the values from configs/run.yaml
# ---------------------------------------------------------------------------

@pytest.fixture
def admission_cfg() -> AdmissionConfig:
    """Standard admission config matching configs/run.yaml."""
    return AdmissionConfig(
        w_base_s=20.0,
        kappa={"HIGH": 1.0, "MEDIUM": 1.5, "LOW": 2.5},
        w_max_s=60.0,
        alpha_min=5.0,
        alpha_max=400.0,
        alpha_increase=2.0,
        alpha_decrease=0.7,
        control_tick_s=1.0,
        target_origin_p95_ms=400,
        target_origin_err_rate=0.005,
        max_attempts=5,
        poll_interval_ms=2000,
    )


@pytest.fixture
def tight_cfg() -> AdmissionConfig:
    """Config with a low w_max_s so we can test the clamping logic."""
    return AdmissionConfig(
        w_base_s=20.0,
        kappa={"HIGH": 1.0, "MEDIUM": 1.5, "LOW": 2.5},
        w_max_s=30.0,  # LOW would be 20*2.5=50, but gets clamped to 30
        alpha_min=5.0,
        alpha_max=400.0,
        alpha_increase=2.0,
        alpha_decrease=0.7,
        control_tick_s=1.0,
        target_origin_p95_ms=400,
        target_origin_err_rate=0.005,
        max_attempts=5,
        poll_interval_ms=2000,
    )


# ---------------------------------------------------------------------------
# window_for — aaac mode
# ---------------------------------------------------------------------------

class TestWindowForAaac:
    """In aaac mode, windows are class-differentiated via kappa multipliers."""

    def test_high_class_gets_base_window(self, admission_cfg: AdmissionConfig):
        """HIGH kappa=1.0, so W(HIGH) = 20.0 * 1.0 = 20.0"""
        w = window_for(AccessClass.HIGH, admission_cfg, "aaac")
        assert w == pytest.approx(20.0)

    def test_medium_class_gets_scaled_window(self, admission_cfg: AdmissionConfig):
        """MEDIUM kappa=1.5, so W(MEDIUM) = 20.0 * 1.5 = 30.0"""
        w = window_for(AccessClass.MEDIUM, admission_cfg, "aaac")
        assert w == pytest.approx(30.0)

    def test_low_class_gets_longest_window(self, admission_cfg: AdmissionConfig):
        """LOW kappa=2.5, so W(LOW) = 20.0 * 2.5 = 50.0"""
        w = window_for(AccessClass.LOW, admission_cfg, "aaac")
        assert w == pytest.approx(50.0)

    def test_window_ordering_invariant(self, admission_cfg: AdmissionConfig):
        """W(HIGH) <= W(MEDIUM) <= W(LOW) — lower-quality links get more time."""
        w_high = window_for(AccessClass.HIGH, admission_cfg, "aaac")
        w_med = window_for(AccessClass.MEDIUM, admission_cfg, "aaac")
        w_low = window_for(AccessClass.LOW, admission_cfg, "aaac")
        assert w_high <= w_med <= w_low

    def test_window_clamped_to_w_max(self, tight_cfg: AdmissionConfig):
        """When w_base * kappa exceeds w_max_s, clamp to w_max_s."""
        # LOW: 20 * 2.5 = 50, but w_max_s = 30
        w = window_for(AccessClass.LOW, tight_cfg, "aaac")
        assert w == pytest.approx(30.0)

    def test_medium_also_clamped(self, tight_cfg: AdmissionConfig):
        """MEDIUM: 20 * 1.5 = 30 == w_max_s, exactly on the boundary."""
        w = window_for(AccessClass.MEDIUM, tight_cfg, "aaac")
        assert w == pytest.approx(30.0)

    def test_high_not_clamped(self, tight_cfg: AdmissionConfig):
        """HIGH: 20 * 1.0 = 20 < 30, no clamping needed."""
        w = window_for(AccessClass.HIGH, tight_cfg, "aaac")
        assert w == pytest.approx(20.0)


# ---------------------------------------------------------------------------
# window_for — baseline mode
# ---------------------------------------------------------------------------

class TestWindowForBaseline:
    """In baseline mode, all classes get the same flat window (w_base_s)."""

    def test_high_gets_base(self, admission_cfg: AdmissionConfig):
        w = window_for(AccessClass.HIGH, admission_cfg, "baseline")
        assert w == pytest.approx(20.0)

    def test_medium_gets_base(self, admission_cfg: AdmissionConfig):
        w = window_for(AccessClass.MEDIUM, admission_cfg, "baseline")
        assert w == pytest.approx(20.0)

    def test_low_gets_base(self, admission_cfg: AdmissionConfig):
        w = window_for(AccessClass.LOW, admission_cfg, "baseline")
        assert w == pytest.approx(20.0)

    def test_all_classes_equal(self, admission_cfg: AdmissionConfig):
        """Core invariant: baseline is access-blind, no class differentiation."""
        windows = {cls: window_for(cls, admission_cfg, "baseline") for cls in AccessClass}
        assert len(set(windows.values())) == 1  # all the same value


# ---------------------------------------------------------------------------
# window_for — none mode
# ---------------------------------------------------------------------------

class TestWindowForNone:
    """In none mode, window_for returns w_base_s as a defensive default."""

    def test_none_mode_returns_base(self, admission_cfg: AdmissionConfig):
        w = window_for(AccessClass.HIGH, admission_cfg, "none")
        assert w == pytest.approx(20.0)


# ---------------------------------------------------------------------------
# windows_for_all — dict format for store.admit_n()
# ---------------------------------------------------------------------------

class TestWindowsForAll:
    """windows_for_all() returns a dict mapping every AccessClass to its window."""

    def test_returns_all_three_classes(self, admission_cfg: AdmissionConfig):
        d = windows_for_all(admission_cfg, "aaac")
        assert set(d.keys()) == {AccessClass.HIGH, AccessClass.MEDIUM, AccessClass.LOW}

    def test_values_match_individual_calls(self, admission_cfg: AdmissionConfig):
        d = windows_for_all(admission_cfg, "aaac")
        for cls in AccessClass:
            assert d[cls] == pytest.approx(window_for(cls, admission_cfg, "aaac"))

    def test_baseline_all_equal(self, admission_cfg: AdmissionConfig):
        d = windows_for_all(admission_cfg, "baseline")
        assert all(v == pytest.approx(20.0) for v in d.values())


# ---------------------------------------------------------------------------
# weighted_mean_window — used by controller for C_max
# ---------------------------------------------------------------------------

class TestWeightedMeanWindow:
    """weighted_mean_window() computes load-weighted average across waiting classes."""

    def test_single_class_waiting(self, admission_cfg: AdmissionConfig):
        """If only HIGH tickets are waiting, mean == W(HIGH)."""
        counts = {AccessClass.HIGH: 100, AccessClass.MEDIUM: 0, AccessClass.LOW: 0}
        mean = weighted_mean_window(counts, admission_cfg, "aaac")
        assert mean == pytest.approx(20.0)

    def test_uniform_distribution(self, admission_cfg: AdmissionConfig):
        """Equal tickets in all classes: mean = (20+30+50)/3 = 33.33..."""
        counts = {AccessClass.HIGH: 100, AccessClass.MEDIUM: 100, AccessClass.LOW: 100}
        mean = weighted_mean_window(counts, admission_cfg, "aaac")
        expected = (20.0 + 30.0 + 50.0) / 3
        assert mean == pytest.approx(expected)

    def test_skewed_towards_low(self, admission_cfg: AdmissionConfig):
        """Mostly LOW tickets waiting should pull the mean higher."""
        counts = {AccessClass.HIGH: 10, AccessClass.MEDIUM: 10, AccessClass.LOW: 980}
        mean = weighted_mean_window(counts, admission_cfg, "aaac")
        # Mean should be close to W(LOW)=50 since LOW dominates
        assert mean > 45.0

    def test_empty_waiting_returns_base(self, admission_cfg: AdmissionConfig):
        """No tickets waiting: fallback to w_base_s (safe default for C_max calc)."""
        counts = {AccessClass.HIGH: 0, AccessClass.MEDIUM: 0, AccessClass.LOW: 0}
        mean = weighted_mean_window(counts, admission_cfg, "aaac")
        assert mean == pytest.approx(20.0)

    def test_baseline_mean_is_always_base(self, admission_cfg: AdmissionConfig):
        """In baseline mode, all windows are w_base_s, so mean == w_base_s."""
        counts = {AccessClass.HIGH: 100, AccessClass.MEDIUM: 200, AccessClass.LOW: 300}
        mean = weighted_mean_window(counts, admission_cfg, "baseline")
        assert mean == pytest.approx(20.0)

    def test_real_world_class_mix(self, admission_cfg: AdmissionConfig):
        """Using the class_mix from run.yaml: HIGH=25%, MEDIUM=40%, LOW=35%."""
        # 20000 clients * class_mix
        counts = {AccessClass.HIGH: 5000, AccessClass.MEDIUM: 8000, AccessClass.LOW: 7000}
        mean = weighted_mean_window(counts, admission_cfg, "aaac")
        expected = (5000*20.0 + 8000*30.0 + 7000*50.0) / 20000
        assert mean == pytest.approx(expected)
