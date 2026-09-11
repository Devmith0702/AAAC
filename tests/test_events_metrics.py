"""Event log reading and the §4.4 metrics.

Fixtures are written by hand so each test states exactly which events produce
which number — the point of §7.2 provenance.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from aaac.evaluation.access_class import AccessClass
from aaac.evaluation.events import EventLog, LogIntegrityError
from aaac.evaluation.metrics import build_traces, compute, quantile

HIGH, MEDIUM, LOW = int(AccessClass.HIGH), int(AccessClass.MEDIUM), int(AccessClass.LOW)


def ev(ts: float, ticket: str, name: str, **extra: Any) -> dict[str, Any]:
    return {"ts": ts, "run_id": "r1", "mode": "baseline", "ticket_id": ticket,
            "event": name, **extra}


def write_log(tmp_path: Path, events: list[dict[str, Any]], name: str = "events.jsonl") -> Path:
    path = tmp_path / name
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    return path


def completing_ticket(ticket: str, true_class: int, *, t0: float, attempts: int = 1,
                      payload: int = 450_000) -> list[dict[str, Any]]:
    events = [ev(t0, ticket, "JOIN", true_class=true_class)]
    events.append(ev(t0 + 0.2, ticket, "ESTIMATE", true_class=true_class,
                     access_class=true_class))
    t = t0 + 1.0
    for attempt in range(1, attempts + 1):
        events.append(ev(t, ticket, "ADMIT", true_class=true_class, attempt=attempt))
        t += 5.0
        if attempt < attempts:
            events.append(ev(t, ticket, "TIMEOUT", true_class=true_class, attempt=attempt,
                             ok=False, bytes=payload // 2))
            events.append(ev(t + 0.1, ticket, "REQUEUE", true_class=true_class,
                             attempt=attempt))
            t += 1.0
    events.append(ev(t, ticket, "COMPLETE", true_class=true_class, attempt=attempts,
                     ok=True, bytes=payload))
    return events


def failing_ticket(ticket: str, true_class: int, *, t0: float,
                   attempts: int = 3) -> list[dict[str, Any]]:
    events = [ev(t0, ticket, "JOIN", true_class=true_class)]
    t = t0 + 1.0
    for attempt in range(1, attempts + 1):
        events.append(ev(t, ticket, "ADMIT", true_class=true_class, attempt=attempt))
        events.append(ev(t + 5.0, ticket, "TIMEOUT", true_class=true_class, attempt=attempt,
                         ok=False, bytes=100_000))
        t += 7.0
    events.append(ev(t, ticket, "ABANDON", true_class=true_class))
    return events


# -- reading ---------------------------------------------------------------


def test_reads_and_time_orders(tmp_path: Path) -> None:
    path = write_log(tmp_path, [ev(2.0, "t1", "JOIN"), ev(1.0, "t2", "JOIN")])
    log = EventLog.read(path)
    assert [e["ticket_id"] for e in log.events] == ["t2", "t1"]


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        EventLog.read(tmp_path / "nope.jsonl")


def test_truncated_json_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text('{"ts":1,"event":"JOIN","ticket_id":"t1"}\n{"ts":2,"eve\n')
    with pytest.raises(LogIntegrityError, match="not valid JSON"):
        EventLog.read(path)


def test_event_outside_the_closed_vocabulary_is_refused(tmp_path: Path) -> None:
    # §3.8's vocabulary is closed. Silently skipping an unknown type would be a
    # hole in the instrument.
    path = write_log(tmp_path, [ev(1.0, "t1", "SOMETHING_NEW")])
    with pytest.raises(LogIntegrityError, match="closed vocabulary"):
        EventLog.read(path)


def test_non_strict_mode_counts_what_it_skipped(tmp_path: Path) -> None:
    path = write_log(tmp_path, [ev(1.0, "t1", "JOIN"), ev(2.0, "t2", "MYSTERY")])
    log = EventLog.read(path, strict=False)
    assert log.unknown_event_types == {"MYSTERY": 1}


def test_mixed_modes_in_one_log_are_refused(tmp_path: Path) -> None:
    events = [ev(1.0, "t1", "JOIN"), {**ev(2.0, "t2", "JOIN"), "mode": "aaac"}]
    with pytest.raises(LogIntegrityError, match="one mode"):
        EventLog.read(write_log(tmp_path, events)).single_mode()


def test_clock_skew_between_the_two_logs_is_flagged(tmp_path: Path) -> None:
    events = [ev(1000.0, "t1", "JOIN"), ev(1010.0, "t1", "COMPLETE", ok=True)]
    events += [
        {"ts": 5000.0 + i, "run_id": "r1", "mode": "baseline", "event": "ORIGIN_SAMPLE",
         "in_flight": 1, "p99_ms": 60.0, "err_rate_1s": 0.0}
        for i in range(5)
    ]
    log = EventLog.read(write_log(tmp_path, events))
    warning = log.clock_skew_warning()
    assert warning is not None and "do not overlap" in warning


# -- traces ----------------------------------------------------------------


def test_trace_counts_attempts_and_bytes(tmp_path: Path) -> None:
    events = completing_ticket("t1", LOW, t0=100.0, attempts=3)
    traces, _ = build_traces(EventLog.read(write_log(tmp_path, events)))
    trace = traces["t1"]
    assert trace.admits == 3
    assert trace.timeouts == 2
    assert trace.completed_ok
    assert trace.bytes_success == 450_000
    # Two burned attempts at 225_000 each, plus the successful 450_000.
    assert trace.bytes_total == 450_000 + 2 * 225_000


def test_time_to_completion_is_last_complete_minus_join(tmp_path: Path) -> None:
    events = completing_ticket("t1", HIGH, t0=100.0, attempts=1)
    traces, _ = build_traces(EventLog.read(write_log(tmp_path, events)))
    assert traces["t1"].time_to_completion_s == pytest.approx(6.0)


# -- metrics ---------------------------------------------------------------


def build_run(tmp_path: Path) -> Path:
    events: list[dict[str, Any]] = []
    for i in range(10):
        events += completing_ticket(f"h{i}", HIGH, t0=100.0 + i)
    for i in range(8):
        events += completing_ticket(f"m{i}", MEDIUM, t0=100.0 + i, attempts=2)
    for i in range(2):
        events += failing_ticket(f"mf{i}", MEDIUM, t0=100.0 + i)
    for i in range(5):
        events += completing_ticket(f"l{i}", LOW, t0=100.0 + i, attempts=4)
    for i in range(5):
        events += failing_ticket(f"lf{i}", LOW, t0=100.0 + i)
    events += [
        {"ts": 100.0 + i, "run_id": "r1", "mode": "baseline", "event": "ORIGIN_SAMPLE",
         "in_flight": 10, "p99_ms": 150.0, "err_rate_1s": 0.02, "served": 50,
         "errors": 1, "rejected": 1}
        for i in range(40)
    ]
    return write_log(tmp_path, events)


def test_completion_rates_are_per_true_class(tmp_path: Path) -> None:
    m = compute(EventLog.read(build_run(tmp_path)))
    assert m.per_class[HIGH].completion_rate == pytest.approx(1.0)
    assert m.per_class[MEDIUM].completion_rate == pytest.approx(0.8)
    assert m.per_class[LOW].completion_rate == pytest.approx(0.5)


def test_delta_is_high_minus_low(tmp_path: Path) -> None:
    m = compute(EventLog.read(build_run(tmp_path)))
    assert m.delta == pytest.approx(0.5)


def test_grouping_uses_true_class_not_the_estimate(tmp_path: Path) -> None:
    # A LOW client the classifier called HIGH must still count as LOW, or the
    # metric measures the classifier instead of the system.
    events = completing_ticket("t1", LOW, t0=100.0)
    for e in events:
        if e["event"] == "ESTIMATE":
            e["access_class"] = HIGH
    m = compute(EventLog.read(write_log(tmp_path, events)))
    assert m.per_class[LOW].joined == 1
    assert m.per_class[HIGH].joined == 0


def test_non_completers_are_censored_and_counted(tmp_path: Path) -> None:
    m = compute(EventLog.read(build_run(tmp_path)))
    low = m.per_class[LOW]
    assert low.ttc_completers == 5
    assert low.ttc_censored == 5
    assert m.provenance.non_completers_censored == 7


def test_goodput_includes_bytes_burned_on_failed_attempts(tmp_path: Path) -> None:
    events = completing_ticket("t1", LOW, t0=100.0, attempts=3)
    m = compute(EventLog.read(write_log(tmp_path, events)))
    low = m.per_class[LOW]
    assert low.bytes_total > low.bytes_success
    assert low.goodput == pytest.approx(450_000 / 900_000)


def test_attempt_ccdf_is_monotone_and_starts_at_one(tmp_path: Path) -> None:
    m = compute(EventLog.read(build_run(tmp_path)))
    ccdf = m.per_class[LOW].attempts_ccdf
    assert ccdf[0] == (0, 1.0)
    values = [p for _, p in ccdf]
    assert values == sorted(values, reverse=True)


def test_jains_index_is_computed_over_the_three_class_rates(tmp_path: Path) -> None:
    m = compute(EventLog.read(build_run(tmp_path)))
    assert 0.0 < m.jain < 1.0


def test_origin_stability_averages_disjoint_one_second_buckets(tmp_path: Path) -> None:
    m = compute(EventLog.read(build_run(tmp_path)))
    assert m.origin.samples == 40
    assert m.origin.err_rate_mean == pytest.approx(0.02)
    assert m.origin.p99_max_ms == pytest.approx(150.0)


def test_classifier_confusion_and_optimistic_error(tmp_path: Path) -> None:
    events: list[dict[str, Any]] = []
    # 8 LOW clients correctly classified, 2 called HIGH (optimistic - the error
    # that sends a slow client a payload it cannot fetch).
    for i in range(10):
        block = completing_ticket(f"l{i}", LOW, t0=100.0 + i)
        for e in block:
            if e["event"] == "ESTIMATE" and i < 2:
                e["access_class"] = HIGH
        events += block
    m = compute(EventLog.read(write_log(tmp_path, events)))
    assert m.classifier.n == 10
    assert m.classifier.accuracy == pytest.approx(0.8)
    assert m.classifier.optimistic_error_rate == pytest.approx(0.2)
    assert m.classifier.pessimistic_error_rate == pytest.approx(0.0)
    assert m.classifier.confusion[LOW][HIGH] == 2


def test_tickets_without_true_class_are_excluded_loudly(tmp_path: Path) -> None:
    # This is the §3.8 contract gap: without true_class, the per-class breakdown
    # that §4.4 exists for is incomplete.
    events = [ev(1.0, "t1", "JOIN"), ev(2.0, "t1", "COMPLETE", ok=True, bytes=1000)]
    m = compute(EventLog.read(write_log(tmp_path, events)))
    assert m.provenance.tickets_included == 0
    assert any("true_class" in w for w in m.provenance.warnings)


def test_tickets_without_join_are_excluded_with_a_reason(tmp_path: Path) -> None:
    events = [ev(2.0, "t1", "COMPLETE", true_class=HIGH, ok=True, bytes=10)]
    m = compute(EventLog.read(write_log(tmp_path, events)))
    assert any("no JOIN" in reason for reason in m.provenance.exclusions)


def test_complete_without_ok_field_is_flagged(tmp_path: Path) -> None:
    events = [ev(1.0, "t1", "JOIN", true_class=HIGH),
              ev(2.0, "t1", "COMPLETE", true_class=HIGH, bytes=1000)]
    m = compute(EventLog.read(write_log(tmp_path, events)))
    assert any("`ok`" in w for w in m.provenance.warnings)


def test_provenance_describes_itself(tmp_path: Path) -> None:
    m = compute(EventLog.read(build_run(tmp_path)))
    described = m.provenance.describe()
    assert "disaggregated by : true_class" in described
    assert "tickets included" in described
    assert "non-completers censored" in described


def test_delta_is_undefined_rather_than_zero_when_a_class_is_absent(tmp_path: Path) -> None:
    events = completing_ticket("t1", MEDIUM, t0=100.0)
    m = compute(EventLog.read(write_log(tmp_path, events)))
    assert m.delta is None
    assert any("undefined" in w for w in m.provenance.warnings)


@pytest.mark.parametrize(
    ("values", "q", "expected"),
    [([1, 2, 3, 4], 0.5, 2.5), ([5], 0.9, 5.0), ([1, 2, 3, 4, 5], 0.0, 1.0)],
)
def test_quantile(values: list[float], q: float, expected: float) -> None:
    assert quantile(values, q) == pytest.approx(expected)


def test_quantile_of_nothing_is_none_not_zero() -> None:
    # 0.0 would read as "fast"; None reads as "no data", which is the truth.
    assert quantile([], 0.5) is None
