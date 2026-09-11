"""Rolling health window: quantisation, rotation, and what counts as an error."""

from __future__ import annotations

import pytest

from aaac.origin.health import HealthTracker
from tests.conftest import FakeClock


def make_tracker(clock: FakeClock, window_s: float = 5.0, n_buckets: int = 5) -> HealthTracker:
    return HealthTracker(window_s=window_s, n_buckets=n_buckets, clock=clock)


def test_empty_window_reports_zeros() -> None:
    stats = make_tracker(FakeClock()).window_stats()
    assert (stats.served, stats.errors, stats.p99_ms, stats.err_rate_1s) == (0, 0, 0.0, 0.0)


def test_reported_p99_never_understates_the_true_p99() -> None:
    # The histogram is log-spaced, so p99 is bucket-quantised. The invariant that
    # matters is the direction of the error: reporting a p99 *lower* than reality
    # would flatter the origin exactly when it is struggling.
    clock = FakeClock()
    tracker = make_tracker(clock)
    samples = [5.0 + i * 0.37 for i in range(1000)]
    for value in samples:
        tracker.record_served(value)

    ordered = sorted(samples)
    true_p99 = ordered[int(0.99 * len(ordered)) - 1]
    reported = tracker.window_stats().p99_ms

    assert reported >= true_p99
    # One bucket is ~9.6% wide; allow two to cover exact-edge rounding.
    assert reported <= true_p99 * 1.25


def test_p50_is_also_conservative() -> None:
    clock = FakeClock()
    tracker = make_tracker(clock)
    for value in (10.0 + i * 0.11 for i in range(500)):
        tracker.record_served(value)
    stats = tracker.window_stats()
    assert 10.0 <= stats.p50_ms <= 90.0


def test_observations_leave_the_window_once_it_has_rolled_over() -> None:
    clock = FakeClock()
    tracker = make_tracker(clock, window_s=5.0, n_buckets=5)
    tracker.record_served(120.0)
    assert tracker.window_stats().served == 1

    clock.advance(6.0)
    assert tracker.window_stats().served == 0


def test_observations_survive_inside_the_window() -> None:
    clock = FakeClock()
    tracker = make_tracker(clock, window_s=5.0, n_buckets=5)
    for _ in range(3):
        tracker.record_served(120.0)
        clock.advance(1.0)
    assert tracker.window_stats().served == 3


def test_rejections_count_as_errors_but_stay_out_of_the_latency_histogram() -> None:
    # A 503 completes in microseconds. Folding those into the latency
    # distribution would drag p99 down precisely when the origin is overloaded.
    clock = FakeClock()
    tracker = make_tracker(clock)
    tracker.record_served(500.0)
    for _ in range(99):
        tracker.record_rejected()

    stats = tracker.window_stats()
    assert stats.served == 1
    assert stats.rejected == 99
    assert stats.errors == 99
    assert stats.p99_ms >= 500.0


def test_abandoned_requests_are_not_errors() -> None:
    # A client that disconnects mid-service timed out on its own link. The origin
    # did nothing wrong, and timed-out clients are expected in this experiment.
    clock = FakeClock()
    tracker = make_tracker(clock)
    tracker.record_abandoned()
    stats = tracker.window_stats()
    assert stats.abandoned == 1
    assert stats.errors == 0
    assert stats.err_rate_1s == 0.0


def test_err_rate_1s_reads_only_fully_elapsed_buckets() -> None:
    clock = FakeClock()
    tracker = make_tracker(clock, window_s=5.0, n_buckets=5)

    clock.advance(0.5)
    for _ in range(4):
        tracker.record_rejected()

    # Still inside the bucket being filled: nothing has fully elapsed yet, so
    # the trailing-1 s rate has no complete bucket to read.
    assert tracker.window_stats().err_rate_1s == 0.0

    clock.advance(1.0)
    assert tracker.window_stats().err_rate_1s == pytest.approx(1.0)


def test_err_rate_1s_mixes_served_and_rejected_in_the_denominator() -> None:
    clock = FakeClock()
    tracker = make_tracker(clock, window_s=5.0, n_buckets=5)
    clock.advance(0.1)
    for _ in range(3):
        tracker.record_served(20.0)
    tracker.record_rejected()

    clock.advance(1.0)
    assert tracker.window_stats().err_rate_1s == pytest.approx(0.25)


def test_served_failures_count_as_errors() -> None:
    clock = FakeClock()
    tracker = make_tracker(clock)
    tracker.record_served(20.0, ok=False)
    stats = tracker.window_stats()
    assert stats.served == 1
    assert stats.errors == 1


def test_memory_does_not_grow_with_observation_count() -> None:
    # The O(1) claim in §3.5 is about fixed bucket and histogram sizes.
    clock = FakeClock()
    tracker = make_tracker(clock)
    for i in range(50_000):
        tracker.record_served(1.0 + (i % 900))
    assert len(tracker._buckets) == 5
    assert all(len(b.hist) == len(tracker._buckets[0].hist) for b in tracker._buckets)


@pytest.mark.parametrize(("window_s", "n_buckets"), [(0.0, 5), (5.0, 0), (-1.0, 5)])
def test_invalid_window_configuration_is_rejected(window_s: float, n_buckets: int) -> None:
    with pytest.raises(ValueError):
        HealthTracker(window_s=window_s, n_buckets=n_buckets)
