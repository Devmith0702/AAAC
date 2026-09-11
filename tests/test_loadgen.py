"""Load generation against M2's SDK surface — without the SDK, the network, or Redis.

The fake outcome below mirrors ``aaac.client.sdk.ClientOutcome`` (origin/sachintha):
a dataclass whose ``outcome`` is a string enum. ``run_client`` never raises except
CancelledError, so a failure arrives as a label, not an exception.
"""

from __future__ import annotations

import asyncio
import sys
import types
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import pytest
import yaml

from aaac.evaluation.access_class import AccessClass
from aaac.evaluation.loadgen import (
    EXCEPTION,
    UNRECOGNISED,
    ClientRecord,
    LoadReport,
    SdkSettings,
    SdkUnavailableError,
    classify,
    load_sdk_runner,
    run_population,
    sdk_settings_from_config,
)
from aaac.evaluation.population import Client, generate
from aaac.origin.config import LoadConfig


class FakeOutcome(StrEnum):
    COMPLETED = "COMPLETED"
    TIMED_OUT = "TIMED_OUT"
    ORIGIN_UNAVAILABLE = "ORIGIN_UNAVAILABLE"
    ADMISSION_UNAVAILABLE = "ADMISSION_UNAVAILABLE"


@dataclass
class FakeClientOutcome:
    client_id: str
    outcome: FakeOutcome
    error: str = ""


SETTINGS = SdkSettings(
    admission_base="http://admission:8000",
    delivery_base="http://delivery:8001",
    probe_bytes=65536,
    min_rtt_samples=5,
    confidence_threshold=0.6,
    model_path="models/link_classifier.joblib",
    abandon_after_s=900.0,
)


def make_client(i: int, true_class: int = int(AccessClass.LOW)) -> Client:
    return Client(client_index=i, client_id=f"c-1-{i:06d}", index_no=900_000 + i,
                  true_class=true_class, arrival_s=0.0, seed=1 + i)


def scripted(outcomes: dict[int, FakeOutcome]) -> Any:
    async def runner(client: Client) -> FakeClientOutcome:
        return FakeClientOutcome(client.client_id, outcomes[client.client_index])
    return runner


async def test_only_completed_counts_as_ok() -> None:
    runner = scripted({0: FakeOutcome.COMPLETED, 1: FakeOutcome.TIMED_OUT,
                       2: FakeOutcome.ORIGIN_UNAVAILABLE})
    report = await run_population([make_client(i) for i in range(3)], runner,
                                   concurrency_cap=3)
    assert report.completed == 1
    assert report.by_outcome == {"COMPLETED": 1, "TIMED_OUT": 1, "ORIGIN_UNAVAILABLE": 1}
    # Clients that fail to finish, and an origin shedding load, are what the
    # experiment observes — not a compromised run.
    assert report.infrastructure_warning() is None


async def test_admission_unavailable_is_flagged_as_compromising_the_run() -> None:
    runner = scripted({0: FakeOutcome.COMPLETED, 1: FakeOutcome.ADMISSION_UNAVAILABLE})
    report = await run_population([make_client(i) for i in range(2)], runner,
                                  concurrency_cap=2)
    warning = report.infrastructure_warning()
    assert warning is not None and "ADMISSION_UNAVAILABLE" in warning


async def test_a_raising_runner_is_recorded_and_does_not_stop_the_others() -> None:
    async def runner(client: Client) -> FakeClientOutcome:
        if client.client_index == 1:
            raise RuntimeError("sdk broke its never-raise contract")
        return FakeClientOutcome(client.client_id, FakeOutcome.COMPLETED)

    report = await run_population([make_client(i) for i in range(3)], runner,
                                  concurrency_cap=3)
    assert report.by_outcome == {"COMPLETED": 2, EXCEPTION: 1}
    assert report.records[1].error is not None and "RuntimeError" in report.records[1].error
    assert report.infrastructure_warning() is not None


def test_an_unreadable_result_is_unrecognised_not_ok() -> None:
    label, error = classify(object())
    assert label == UNRECOGNISED
    assert error is not None


async def test_concurrency_cap_is_respected() -> None:
    active = 0
    peak = 0

    async def runner(client: Client) -> FakeClientOutcome:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return FakeClientOutcome(client.client_id, FakeOutcome.COMPLETED)

    await run_population([make_client(i) for i in range(8)], runner, concurrency_cap=2)
    assert peak == 2


async def test_records_are_ordered_by_client_index() -> None:
    runner = scripted(dict.fromkeys(range(5), FakeOutcome.COMPLETED))
    clients = [make_client(i) for i in reversed(range(5))]
    report = await run_population(clients, runner, concurrency_cap=5)
    assert [r.client_index for r in report.records] == list(range(5))


def test_sdk_absent_raises_rather_than_substituting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "aaac.client.sdk", None)
    with pytest.raises(SdkUnavailableError, match="run_client"):
        load_sdk_runner(SETTINGS)


async def test_runner_passes_identity_through_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def run_client(**kwargs: Any) -> FakeClientOutcome:
        seen.update(kwargs)
        return FakeClientOutcome(kwargs["client_id"], FakeOutcome.COMPLETED)

    sdk = types.ModuleType("aaac.client.sdk")
    sdk.run_client = run_client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "aaac.client", types.ModuleType("aaac.client"))
    monkeypatch.setitem(sys.modules, "aaac.client.sdk", sdk)

    client = make_client(3, true_class=int(AccessClass.LOW))
    result = await load_sdk_runner(SETTINGS)(client)

    assert classify(result)[0] == "COMPLETED"
    assert seen["client_id"] == client.client_id
    assert int(seen["true_class"]) == int(AccessClass.LOW)
    assert seen["index_no"] == "900003"
    assert seen["seed"] == client.seed
    assert seen["abandon_after_s"] == 900.0
    assert (seen["admission_base"], seen["delivery_base"]) == (
        SETTINGS.admission_base, SETTINGS.delivery_base,
    )
    # Left at the SDK's default: jitter would change the experiment.
    assert "poll_jitter_frac" not in seen


LOAD = LoadConfig(n_clients=10, scale_factor=10, burst_center_s=30.0, burst_sigma_s=15.0,
                  tail_decay_s=600.0, class_mix={"HIGH": 0.25, "MEDIUM": 0.40, "LOW": 0.35},
                  abandon_after_s=450.0, burst_fraction=0.8)


def test_settings_come_from_the_estimator_section_and_the_population(tmp_path: Path) -> None:
    path = tmp_path / "run.yaml"
    path.write_text(yaml.safe_dump({"estimator": {
        "probe_bytes": 65536, "min_rtt_samples": 5, "confidence_threshold": 0.6,
        "model_path": "models/link_classifier.joblib",
    }}), encoding="utf-8")
    population = generate(1, LOAD)
    settings = sdk_settings_from_config(path, population, admission_base="a",
                                        delivery_base="d")
    assert settings.probe_bytes == 65536
    assert settings.abandon_after_s == population.abandon_after_s == 450.0


async def test_a_bad_cap_is_refused() -> None:
    with pytest.raises(ValueError):
        await run_population([], scripted({}), concurrency_cap=0)


def test_launch_lag_is_reported_rather_than_absorbed() -> None:
    # §5.2 q4: if the machine cannot keep up, that ceiling must show up in the
    # report, not be quietly smoothed away.
    late = LoadReport(records=[], n_clients=10, concurrency_cap=1, wall_clock_s=100.0,
                      population_hash="x", max_launch_lag_s=30.0, p95_launch_lag_s=12.0)
    warning = late.lag_warning()
    assert warning is not None and "did not keep up" in warning


def test_no_lag_warning_when_the_machine_kept_up() -> None:
    fine = LoadReport(records=[], n_clients=10, concurrency_cap=100, wall_clock_s=10.0,
                      population_hash="x", max_launch_lag_s=0.2, p95_launch_lag_s=0.05)
    assert fine.lag_warning() is None


def test_record_launch_lag_and_ok() -> None:
    record = ClientRecord(0, "c", 2, scheduled_s=10.0, started_s=12.5, finished_s=20.0,
                          outcome="COMPLETED")
    assert record.launch_lag_s == pytest.approx(2.5)
    assert record.ok
    assert not ClientRecord(0, "c", 2, 0.0, 0.0, 1.0, outcome="EXPIRED").ok


def test_a_missing_estimator_section_is_refused_not_defaulted(tmp_path: Path) -> None:
    path = tmp_path / "run.yaml"
    path.write_text(yaml.safe_dump({"seed": 1}), encoding="utf-8")
    with pytest.raises(ValueError, match="estimator"):
        sdk_settings_from_config(path, generate(1, LOAD), admission_base="a",
                                 delivery_base="d")
