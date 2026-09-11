"""The analysis chain against the event shapes M1's admission service actually emits.

The other metrics tests use hand-built events in the shape §3.8 describes. This
file pins the shape M1 *ships*, transcribed field for field from
``origin/thisaru`` at 4a1acc5:

- record envelope ............ common/events.py        EventLogger.log
- JOIN + ADMIT, mode none .... admission/api.py        queue_join (both access_class HIGH)
- JOIN, baseline / aaac ...... admission/api.py        queue_join (MEDIUM placeholder)
- ESTIMATE ................... admission/api.py        queue_estimate (accepted estimates only)
- COMPLETE / ABANDON ......... admission/api.py        queue_complete (no `ok`; ok=false -> ABANDON)
- ADMIT ...................... admission/controller.py tick
- CONTROL .................... admission/controller.py tick (no ticket_id)
- TIMEOUT, DOWNGRADE, REQUEUE  admission/requeue.py    handle_timeout (TIMEOUT carries no bytes)

TODO(merge): once M1's package is in the tree, produce these events with M1's
real EventLogger instead, so a change to their emitted fields fails here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from aaac.evaluation.access_class import AccessClass
from aaac.evaluation.events import EventLog
from aaac.evaluation.metrics import build_traces, compute

HIGH, MEDIUM, LOW = int(AccessClass.HIGH), int(AccessClass.MEDIUM), int(AccessClass.LOW)


def m1(ts: float, event: str, *, run_mode: str, ticket_id: str | None = None,
       **fields: Any) -> dict[str, Any]:
    """One record exactly as EventLogger.log builds it."""
    record: dict[str, Any] = {"ts": ts, "run_id": f"s1-{run_mode}", "mode": run_mode}
    if ticket_id is not None:
        record["ticket_id"] = ticket_id
    record["event"] = event
    record.update(fields)
    return record


def controller_admit(ts: float, tid: str, *, run_mode: str, cls: int, true_class: int,
                     attempt: int) -> dict[str, Any]:
    return m1(ts, "ADMIT", run_mode=run_mode, ticket_id=tid, access_class=cls,
              true_class=true_class, attempt=attempt, bytes=0, duration_ms=None,
              variant=None, position=0)


def origin_sample(ts: float, run_mode: str) -> dict[str, Any]:
    """As written by aaac.origin.service to results/origin-{run_id}.jsonl."""
    return {"ts": ts, "run_id": f"s1-{run_mode}", "mode": run_mode, "event": "ORIGIN_SAMPLE",
            "in_flight": 3, "waiting": 0, "window_s": 5.0, "served": 40, "errors": 0,
            "rejected": 0, "abandoned": 0, "p50_ms": 62.0, "p99_ms": 181.0,
            "err_rate_1s": 0.0}


def write_jsonl(path: Path, events: list[dict[str, Any]]) -> Path:
    path.write_text("".join(json.dumps(e, separators=(",", ":")) + "\n" for e in events),
                    encoding="utf-8")
    return path


def baseline_log(tmp_path: Path) -> EventLog:
    b = "baseline"
    events = [
        # h1 — HIGH, classified, completes first time.
        m1(100.0, "JOIN", run_mode=b, ticket_id="h1", access_class=MEDIUM, true_class=HIGH,
           attempt=1, position=3),
        m1(100.5, "ESTIMATE", run_mode=b, ticket_id="h1", access_class=HIGH, true_class=HIGH,
           attempt=1, throughput_kbps=48_000.0, confidence=0.97),
        controller_admit(102.0, "h1", run_mode=b, cls=HIGH, true_class=HIGH, attempt=1),
        m1(103.0, "COMPLETE", run_mode=b, ticket_id="h1", access_class=HIGH, true_class=HIGH,
           attempt=1, bytes=411_000, duration_ms=620.0, variant="full"),
        # l1 — LOW, classified, times out, is re-queued, then a failed transfer.
        m1(100.1, "JOIN", run_mode=b, ticket_id="l1", access_class=MEDIUM, true_class=LOW,
           attempt=1, position=4),
        m1(100.9, "ESTIMATE", run_mode=b, ticket_id="l1", access_class=LOW, true_class=LOW,
           attempt=1, throughput_kbps=270.0, confidence=0.91),
        controller_admit(102.0, "l1", run_mode=b, cls=LOW, true_class=LOW, attempt=1),
        m1(122.0, "TIMEOUT", run_mode=b, ticket_id="l1", access_class=LOW, true_class=LOW,
           attempt=1),
        m1(122.0, "REQUEUE", run_mode=b, ticket_id="l1", access_class=LOW, true_class=LOW,
           attempt=2, position=40),
        controller_admit(150.0, "l1", run_mode=b, cls=LOW, true_class=LOW, attempt=2),
        m1(170.0, "ABANDON", run_mode=b, ticket_id="l1", access_class=LOW, true_class=LOW,
           attempt=2, bytes=150_000, duration_ms=20_000.0, variant="full"),
        # l2 — LOW, admitted before any estimate was accepted, completes.
        m1(100.2, "JOIN", run_mode=b, ticket_id="l2", access_class=MEDIUM, true_class=LOW,
           attempt=1, position=5),
        controller_admit(102.0, "l2", run_mode=b, cls=MEDIUM, true_class=LOW, attempt=1),
        m1(110.0, "COMPLETE", run_mode=b, ticket_id="l2", access_class=MEDIUM, true_class=LOW,
           attempt=1, bytes=61_000, duration_ms=7_900.0, variant="reduced"),
        # Controller ticks carry no ticket_id.
        m1(101.0, "CONTROL", run_mode=b, alpha=7.0, mu_hat=0.0, in_flight=0, C_max=-1,
           origin_p99=None),
        m1(102.0, "CONTROL", run_mode=b, alpha=9.0, mu_hat=0.3, in_flight=3, C_max=6,
           origin_p99=181.0),
    ]
    events_path = write_jsonl(tmp_path / "events-s1-baseline.jsonl", events)
    origin_path = write_jsonl(tmp_path / "origin-s1-baseline.jsonl",
                              [origin_sample(100.0 + i, b) for i in range(71)])
    return EventLog.read(events_path, origin_path)


def test_m1_log_reads_strictly_and_is_single_mode(tmp_path: Path) -> None:
    log = baseline_log(tmp_path)
    assert log.single_mode() == "baseline"
    assert log.ticket_events_without_id() == 0
    assert log.clock_skew_warning() is None


def test_completion_and_delta_from_m1_shapes(tmp_path: Path) -> None:
    m = compute(baseline_log(tmp_path))
    assert (m.per_class[HIGH].joined, m.per_class[HIGH].completed) == (1, 1)
    assert (m.per_class[LOW].joined, m.per_class[LOW].completed) == (2, 1)
    assert m.delta == pytest.approx(0.5)


def test_estimated_class_comes_from_estimate_not_the_join_placeholder(tmp_path: Path) -> None:
    # M1 logs JOIN with access_class=MEDIUM before any classification. Reading
    # that as an estimate would score every unclassified client as MEDIUM.
    traces, _ = build_traces(baseline_log(tmp_path))
    assert traces["h1"].estimated_class == HIGH
    assert traces["l2"].estimated_class is None


def test_classifier_coverage_gap_is_warned(tmp_path: Path) -> None:
    m = compute(baseline_log(tmp_path))
    assert m.classifier.n == 2
    assert m.classifier.accuracy == pytest.approx(1.0)
    assert any("covers 2 of 3" in w for w in m.provenance.warnings)


def test_abandon_bytes_count_as_burned(tmp_path: Path) -> None:
    low = compute(baseline_log(tmp_path)).per_class[LOW]
    assert low.bytes_success == 61_000
    assert low.bytes_total == 61_000 + 150_000


def test_goodput_is_flagged_as_an_upper_bound_when_timeouts_carry_no_bytes(
    tmp_path: Path,
) -> None:
    m = compute(baseline_log(tmp_path))
    assert any("UPPER BOUND" in w for w in m.provenance.warnings)


def test_completion_inferred_without_ok_field_is_disclosed(tmp_path: Path) -> None:
    m = compute(baseline_log(tmp_path))
    assert any("`ok`" in w and "ABANDON" in w for w in m.provenance.warnings)


def test_attempts_are_counted_over_completers_only(tmp_path: Path) -> None:
    low = compute(baseline_log(tmp_path)).per_class[LOW]
    # l1 was admitted twice but never completed; only l2 (one admit) counts.
    assert low.attempts_max == 1


def test_none_mode_shape(tmp_path: Path) -> None:
    n = "none"
    events = [
        m1(10.0, "JOIN", run_mode=n, ticket_id="a", access_class=HIGH, true_class=HIGH,
           attempt=1, position=0),
        m1(10.0, "ADMIT", run_mode=n, ticket_id="a", access_class=HIGH, true_class=HIGH,
           attempt=1, position=0),
        m1(11.0, "COMPLETE", run_mode=n, ticket_id="a", access_class=HIGH, true_class=HIGH,
           attempt=1, bytes=411_000, duration_ms=900.0, variant="full"),
        m1(10.5, "JOIN", run_mode=n, ticket_id="b", access_class=HIGH, true_class=LOW,
           attempt=1, position=0),
        m1(10.5, "ADMIT", run_mode=n, ticket_id="b", access_class=HIGH, true_class=LOW,
           attempt=1, position=0),
    ]
    m = compute(EventLog.read(write_jsonl(tmp_path / "events-s1-none.jsonl", events)))
    assert m.delta == pytest.approx(1.0)
    # No ESTIMATE anywhere in `none`: no classifier figures, and no coverage warning.
    assert m.classifier.n == 0
    assert not any("covers" in w for w in m.provenance.warnings)


def test_aaac_timeout_downgrade_requeue_shape(tmp_path: Path) -> None:
    a = "aaac"
    events = [
        m1(1.0, "JOIN", run_mode=a, ticket_id="t", access_class=MEDIUM, true_class=HIGH,
           attempt=1, position=1),
        controller_admit(2.0, "t", run_mode=a, cls=HIGH, true_class=HIGH, attempt=1),
        m1(22.0, "TIMEOUT", run_mode=a, ticket_id="t", access_class=HIGH, true_class=HIGH,
           attempt=1),
        m1(22.0, "DOWNGRADE", run_mode=a, ticket_id="t", access_class=MEDIUM, true_class=HIGH,
           attempt=1, forced_floor=False),
        m1(22.0, "REQUEUE", run_mode=a, ticket_id="t", access_class=MEDIUM, true_class=HIGH,
           attempt=2, position=1),
    ]
    traces, _ = build_traces(EventLog.read(write_jsonl(tmp_path / "e.jsonl", events)))
    trace = traces["t"]
    assert (trace.timeouts, trace.downgrades, trace.requeues) == (1, 1, 1)
    assert trace.timeouts_without_bytes == 1
