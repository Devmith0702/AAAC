"""The analysis chain against the event shapes M1's admission service emits.

The other metrics tests use hand-built events in the shape §3.8 describes. This
file pins the shape M1 *ships*: every record here is written by M1's real
``common.events.EventLogger``, so the envelope (field set, field order, JSON
encoding) and the closed vocabulary come from their code. If M1 changes what
``log()`` writes, these tests fail — which is the point of the file.

The field values still come from reading M1's call sites:

- record envelope ............ common/events.py        EventLogger.log
- JOIN + ADMIT, mode none .... admission/api.py        queue_join (both access_class HIGH)
- JOIN, baseline / aaac ...... admission/api.py        queue_join (MEDIUM placeholder)
- ESTIMATE ................... admission/api.py        queue_estimate (accepted estimates only)
- COMPLETE / ABANDON ......... admission/api.py        queue_complete (no `ok`; ok=false -> ABANDON)
- ADMIT ...................... admission/controller.py tick
- CONTROL .................... admission/controller.py tick (no ticket_id)
- TIMEOUT, DOWNGRADE, REQUEUE  admission/requeue.py    handle_timeout (TIMEOUT carries no bytes)

``ts`` is passed explicitly. ``EventLogger.log`` stamps ``time.time()`` and then
applies ``**fields``, so a supplied ``ts`` wins — which keeps the timing
assertions deterministic while leaving every other part of the envelope to M1.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from aaac.common.classes import AccessClass
from aaac.common.events import EventLogger
from aaac.evaluation.events import EventLog
from aaac.evaluation.metrics import build_traces, compute

HIGH, MEDIUM, LOW = int(AccessClass.HIGH), int(AccessClass.MEDIUM), int(AccessClass.LOW)

#: (ts, event, ticket_id, fields) — fed to M1's logger verbatim.
Record = tuple[float, str, "str | None", dict[str, Any]]


async def write_with_m1_logger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_id: str,
    mode: str,
    records: list[Record],
) -> Path:
    """Write records through M1's EventLogger and return its event log path."""
    monkeypatch.setenv("AAAC_RESULTS_DIR", str(tmp_path))
    logger = EventLogger(run_id, mode)
    for ts, event, ticket_id, fields in records:
        await logger.log(event, ticket_id=ticket_id, ts=ts, **fields)
    await logger.close()
    return Path(logger.filepath)


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


BASELINE_RECORDS: list[Record] = [
    # h1 — HIGH, classified, completes first time.
    (100.0, "JOIN", "h1", {"access_class": MEDIUM, "true_class": HIGH, "attempt": 1,
                           "position": 3}),
    (100.5, "ESTIMATE", "h1", {"access_class": HIGH, "true_class": HIGH, "attempt": 1,
                               "throughput_kbps": 48_000.0, "confidence": 0.97}),
    (102.0, "ADMIT", "h1", {"access_class": HIGH, "true_class": HIGH, "attempt": 1,
                            "bytes": 0, "duration_ms": None, "variant": None, "position": 0}),
    (103.0, "COMPLETE", "h1", {"access_class": HIGH, "true_class": HIGH, "attempt": 1,
                               "bytes": 411_000, "duration_ms": 620.0, "variant": "full"}),
    # l1 — LOW, classified, times out, is re-queued, then a failed transfer.
    (100.1, "JOIN", "l1", {"access_class": MEDIUM, "true_class": LOW, "attempt": 1,
                           "position": 4}),
    (100.9, "ESTIMATE", "l1", {"access_class": LOW, "true_class": LOW, "attempt": 1,
                               "throughput_kbps": 270.0, "confidence": 0.91}),
    (102.0, "ADMIT", "l1", {"access_class": LOW, "true_class": LOW, "attempt": 1,
                            "bytes": 0, "duration_ms": None, "variant": None, "position": 0}),
    (122.0, "TIMEOUT", "l1", {"access_class": LOW, "true_class": LOW, "attempt": 1}),
    (122.0, "REQUEUE", "l1", {"access_class": LOW, "true_class": LOW, "attempt": 2,
                              "position": 40}),
    (150.0, "ADMIT", "l1", {"access_class": LOW, "true_class": LOW, "attempt": 2,
                            "bytes": 0, "duration_ms": None, "variant": None, "position": 0}),
    (170.0, "ABANDON", "l1", {"access_class": LOW, "true_class": LOW, "attempt": 2,
                              "bytes": 150_000, "duration_ms": 20_000.0, "variant": "full"}),
    # l2 — LOW, admitted before any estimate was accepted, completes.
    (100.2, "JOIN", "l2", {"access_class": MEDIUM, "true_class": LOW, "attempt": 1,
                           "position": 5}),
    (102.0, "ADMIT", "l2", {"access_class": MEDIUM, "true_class": LOW, "attempt": 1,
                            "bytes": 0, "duration_ms": None, "variant": None, "position": 0}),
    (110.0, "COMPLETE", "l2", {"access_class": MEDIUM, "true_class": LOW, "attempt": 1,
                               "bytes": 61_000, "duration_ms": 7_900.0, "variant": "reduced"}),
    # Controller ticks carry no ticket_id.
    (101.0, "CONTROL", None, {"alpha": 7.0, "mu_hat": 0.0, "in_flight": 0, "C_max": -1,
                              "origin_p99": None}),
    (102.0, "CONTROL", None, {"alpha": 9.0, "mu_hat": 0.3, "in_flight": 3, "C_max": 6,
                              "origin_p99": 181.0}),
]


async def baseline_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> EventLog:
    events_path = await write_with_m1_logger(
        tmp_path, monkeypatch, "s1-baseline", "baseline", BASELINE_RECORDS
    )
    origin_path = write_jsonl(tmp_path / "origin-s1-baseline.jsonl",
                              [origin_sample(100.0 + i, "baseline") for i in range(71)])
    return EventLog.read(events_path, origin_path)


async def test_m1_logger_refuses_an_event_outside_the_closed_vocabulary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # §3.8's vocabulary is closed on M1's side as well as in M3's reader, so a
    # new event type cannot reach the log without both of us noticing.
    monkeypatch.setenv("AAAC_RESULTS_DIR", str(tmp_path))
    logger = EventLogger("s1-baseline", "baseline")
    with pytest.raises(ValueError, match="Unknown event name"):
        await logger.log("PROMOTE", ticket_id="h1")
    await logger.close()


async def test_m1_log_reads_strictly_and_is_single_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = await baseline_log(tmp_path, monkeypatch)
    assert log.single_mode() == "baseline"
    assert log.ticket_events_without_id() == 0
    assert log.clock_skew_warning() is None


async def test_completion_and_delta_from_m1_shapes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    m = compute(await baseline_log(tmp_path, monkeypatch))
    assert (m.per_class[HIGH].joined, m.per_class[HIGH].completed) == (1, 1)
    assert (m.per_class[LOW].joined, m.per_class[LOW].completed) == (2, 1)
    assert m.delta == pytest.approx(0.5)


async def test_estimated_class_comes_from_estimate_not_the_join_placeholder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # M1 logs JOIN with access_class=MEDIUM before any classification. Reading
    # that as an estimate would score every unclassified client as MEDIUM.
    traces, _ = build_traces(await baseline_log(tmp_path, monkeypatch))
    assert traces["h1"].estimated_class == HIGH
    assert traces["l2"].estimated_class is None


async def test_classifier_coverage_gap_is_warned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    m = compute(await baseline_log(tmp_path, monkeypatch))
    assert m.classifier.n == 2
    assert m.classifier.accuracy == pytest.approx(1.0)
    assert any("covers 2 of 3" in w for w in m.provenance.warnings)


async def test_abandon_bytes_count_as_burned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    low = compute(await baseline_log(tmp_path, monkeypatch)).per_class[LOW]
    assert low.bytes_success == 61_000
    assert low.bytes_total == 61_000 + 150_000


async def test_goodput_is_flagged_as_an_upper_bound_when_timeouts_carry_no_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    m = compute(await baseline_log(tmp_path, monkeypatch))
    assert any("UPPER BOUND" in w for w in m.provenance.warnings)


async def test_completion_inferred_without_ok_field_is_disclosed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    m = compute(await baseline_log(tmp_path, monkeypatch))
    assert any("`ok`" in w and "ABANDON" in w for w in m.provenance.warnings)


async def test_attempts_are_counted_over_completers_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    low = compute(await baseline_log(tmp_path, monkeypatch)).per_class[LOW]
    # l1 was admitted twice but never completed; only l2 (one admit) counts.
    assert low.attempts_max == 1


async def test_none_mode_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    records: list[Record] = [
        (10.0, "JOIN", "a", {"access_class": HIGH, "true_class": HIGH, "attempt": 1,
                             "position": 0}),
        (10.0, "ADMIT", "a", {"access_class": HIGH, "true_class": HIGH, "attempt": 1,
                              "position": 0}),
        (11.0, "COMPLETE", "a", {"access_class": HIGH, "true_class": HIGH, "attempt": 1,
                                 "bytes": 411_000, "duration_ms": 900.0, "variant": "full"}),
        (10.5, "JOIN", "b", {"access_class": HIGH, "true_class": LOW, "attempt": 1,
                             "position": 0}),
        (10.5, "ADMIT", "b", {"access_class": HIGH, "true_class": LOW, "attempt": 1,
                              "position": 0}),
    ]
    events_path = await write_with_m1_logger(tmp_path, monkeypatch, "s1-none", "none", records)
    m = compute(EventLog.read(events_path))
    assert m.delta == pytest.approx(1.0)
    # No ESTIMATE anywhere in `none`: no classifier figures, and no coverage warning.
    assert m.classifier.n == 0
    assert not any("covers" in w for w in m.provenance.warnings)


async def test_aaac_timeout_downgrade_requeue_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    records: list[Record] = [
        (1.0, "JOIN", "t", {"access_class": MEDIUM, "true_class": HIGH, "attempt": 1,
                            "position": 1}),
        (2.0, "ADMIT", "t", {"access_class": HIGH, "true_class": HIGH, "attempt": 1,
                             "bytes": 0, "duration_ms": None, "variant": None, "position": 0}),
        (22.0, "TIMEOUT", "t", {"access_class": HIGH, "true_class": HIGH, "attempt": 1}),
        (22.0, "DOWNGRADE", "t", {"access_class": MEDIUM, "true_class": HIGH, "attempt": 1,
                                  "forced_floor": False}),
        (22.0, "REQUEUE", "t", {"access_class": MEDIUM, "true_class": HIGH, "attempt": 2,
                                "position": 1}),
    ]
    events_path = await write_with_m1_logger(tmp_path, monkeypatch, "s1-aaac", "aaac", records)
    traces, _ = build_traces(EventLog.read(events_path))
    trace = traces["t"]
    assert (trace.timeouts, trace.downgrades, trace.requeues) == (1, 1, 1)
    assert trace.timeouts_without_bytes == 1
