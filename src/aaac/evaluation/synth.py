# ---------------------------------------------------------------------------
# FABRICATED DATA. NOT A MEASUREMENT.
#
# Every event this module produces is invented. It exists for two reasons, both
# named in CLAUDE.md:
#
#   1. §4.7 - "Build that branch and test it against fabricated null data."
#      The falsification rule must be exercised against a result where AAAC does
#      nothing, so that reporting HYPOTHESIS NOT SUPPORTED is known to work
#      before the real run, not discovered afterwards.
#   2. Metrics and plots need a log to be tested against while M1's admission
#      service does not yet exist.
#
# Logs written by this module are named `synthetic-*.jsonl` and report.py prints
# a prominent banner if it is ever pointed at one. No number produced from this
# module may appear in the write-up.
# ---------------------------------------------------------------------------
"""Synthetic event logs for testing the analysis chain."""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aaac.evaluation.access_class import AccessClass
from aaac.evaluation.population import Population

SYNTHETIC_PREFIX = "synthetic-"

#: Payload sizes per variant, roughly matching the 450 KB page of §2.
VARIANT_BYTES = {"full": 450_000, "reduced": 150_000, "essential": 40_000}


@dataclass
class Scenario:
    """Knobs describing an invented world. None of this is measured."""

    name: str
    completion_prob: dict[int, float]
    """Per-attempt completion probability, keyed by true_class."""
    max_attempts: int = 6
    variant_for: dict[int, str] = field(
        default_factory=lambda: {
            int(AccessClass.HIGH): "full",
            int(AccessClass.MEDIUM): "full",
            int(AccessClass.LOW): "full",
        }
    )
    classifier_accuracy: float = 0.97
    window_s: float = 20.0
    origin_err_rate: float = 0.0
    origin_p99_ms: float = 180.0
    emit_estimate: bool = True


def null_scenario(name: str = "null") -> Scenario:
    """A world where the access class determines everything and AAAC changes nothing."""
    return Scenario(
        name=name,
        completion_prob={
            int(AccessClass.HIGH): 0.95,
            int(AccessClass.MEDIUM): 0.70,
            int(AccessClass.LOW): 0.30,
        },
    )


def helpful_scenario(name: str = "helpful") -> Scenario:
    """A world where LOW clients get a smaller payload and mostly succeed."""
    return Scenario(
        name=name,
        completion_prob={
            int(AccessClass.HIGH): 0.95,
            int(AccessClass.MEDIUM): 0.88,
            int(AccessClass.LOW): 0.80,
        },
        variant_for={
            int(AccessClass.HIGH): "full",
            int(AccessClass.MEDIUM): "reduced",
            int(AccessClass.LOW): "essential",
        },
    )


def _estimated_class(rng: random.Random, true_class: int, accuracy: float) -> int:
    if rng.random() < accuracy:
        return true_class
    others = [int(c) for c in AccessClass if int(c) != true_class]
    return rng.choice(others)


def synthesize(
    population: Population,
    mode: str,
    scenario: Scenario,
    *,
    run_id: str,
    seed: int = 0,
    t0: float = 1_756_900_000.0,
) -> list[dict[str, Any]]:
    """Build a full event log for one run. Deterministic given ``seed``."""
    rng = random.Random(seed)
    events: list[dict[str, Any]] = []

    def emit(ts: float, ticket_id: str, name: str, **extra: Any) -> None:
        events.append(
            {
                "ts": round(ts, 6),
                "run_id": run_id,
                "mode": mode,
                "ticket_id": ticket_id,
                "event": name,
                **extra,
            }
        )

    last_ts = t0
    for client in population.clients:
        ticket_id = f"t-{client.client_index:06d}"
        true_class = client.true_class
        joined_at = t0 + client.arrival_s
        emit(joined_at, ticket_id, "JOIN", true_class=true_class, attempt=0, position=0)

        estimated = true_class
        if scenario.emit_estimate and mode != "none":
            estimated = _estimated_class(rng, true_class, scenario.classifier_accuracy)
            emit(
                joined_at + 0.4,
                ticket_id,
                "ESTIMATE",
                true_class=true_class,
                access_class=estimated,
                attempt=0,
            )

        variant = scenario.variant_for[true_class] if mode == "aaac" else "full"
        payload = VARIANT_BYTES[variant]
        prob = scenario.completion_prob[true_class]

        ts = joined_at + (0.0 if mode == "none" else rng.uniform(1.0, 25.0))
        completed = False
        for attempt in range(1, scenario.max_attempts + 1):
            emit(
                ts, ticket_id, "ADMIT",
                true_class=true_class, access_class=estimated, attempt=attempt,
                variant=variant, bytes=0, position=0,
            )
            duration = rng.uniform(1.0, scenario.window_s)
            ts += duration
            if rng.random() < prob:
                emit(
                    ts, ticket_id, "COMPLETE",
                    true_class=true_class, access_class=estimated, attempt=attempt,
                    ok=True, bytes=payload, duration_ms=round(duration * 1000, 3),
                    variant=variant,
                )
                completed = True
                break
            # Burned bytes on a failed attempt: the retry loop's real cost (§4.4).
            burned = int(payload * rng.uniform(0.2, 0.9))
            emit(
                ts, ticket_id, "TIMEOUT",
                true_class=true_class, access_class=estimated, attempt=attempt,
                ok=False, bytes=burned, duration_ms=round(duration * 1000, 3),
                variant=variant,
            )
            if attempt < scenario.max_attempts:
                ts += rng.uniform(1.0, 10.0)
                emit(
                    ts, ticket_id, "REQUEUE",
                    true_class=true_class, access_class=estimated, attempt=attempt,
                    position=0 if mode == "aaac" else rng.randint(100, 5000),
                )
        if not completed:
            emit(
                ts + 1.0, ticket_id, "ABANDON",
                true_class=true_class, attempt=scenario.max_attempts,
            )
        last_ts = max(last_ts, ts + 2.0)

    # ORIGIN_SAMPLE at 1 Hz across the whole run, matching the shape emitted by
    # the real origin service.
    t = t0
    while t <= last_ts:
        events.append(
            {
                "ts": round(t, 6),
                "run_id": run_id,
                "mode": mode,
                "event": "ORIGIN_SAMPLE",
                "in_flight": rng.randint(0, 64),
                "waiting": rng.randint(0, 32),
                "window_s": 5.0,
                "served": rng.randint(20, 300),
                "errors": 0,
                "rejected": 0,
                "abandoned": 0,
                "p50_ms": 62.0,
                "p99_ms": scenario.origin_p99_ms,
                "err_rate_1s": scenario.origin_err_rate,
            }
        )
        t += 1.0

    events.sort(key=lambda e: (e["ts"], e["event"]))
    return events


def write(events: list[dict[str, Any]], path: Path) -> Path:
    path = Path(path)
    if not path.name.startswith(SYNTHETIC_PREFIX):
        raise ValueError(
            f"refusing to write fabricated events to {path.name!r}: synthetic logs must be "
            f"named {SYNTHETIC_PREFIX}*.jsonl so they can never be mistaken for a measurement"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for event in events:
            fh.write(json.dumps(event, separators=(",", ":")) + "\n")
    return path
