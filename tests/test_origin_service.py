"""The origin service end to end, in-process.

The load-bearing tests here are the two that protect the experiment rather than
the code: that the origin sheds load instead of absorbing it (§4.1 — this is what
makes ``mode: none`` collapse), and that ``/origin/health`` stays responsive while
every service slot is occupied (§3.5 — otherwise M1's controller goes blind).
"""

from __future__ import annotations

import asyncio

import pytest

from aaac.origin.health import HealthTracker
from aaac.origin.records import make_result_record
from aaac.origin.service import OriginState
from aaac.origin.sinks import MemorySink
from tests.conftest import FakeClock, cancel_all, make_config, running_app, wait_until


async def test_result_returns_the_deterministic_record() -> None:
    config = make_config(median_ms=1.0, sigma=0.1)
    async with running_app(config) as (_app, client):
        response = await client.get("/origin/result", params={"index": 900_123})

    assert response.status_code == 200
    assert response.json() == make_result_record(config.seed, 900_123)
    assert response.headers["cache-control"] == "no-store"


async def test_result_rejects_a_negative_index() -> None:
    async with running_app(make_config(median_ms=1.0)) as (_app, client):
        response = await client.get("/origin/result", params={"index": -1})
    assert response.status_code == 422


async def test_health_reports_the_three_contract_keys_and_nothing_else() -> None:
    # M1 consumes this shape. Extra diagnostics belong on ORIGIN_SAMPLE.
    async with running_app(make_config(median_ms=1.0)) as (_app, client):
        response = await client.get("/origin/health")

    assert response.status_code == 200
    assert set(response.json()) == {"in_flight", "p99_ms", "err_rate_1s"}


async def test_origin_sheds_load_once_the_bounded_queue_is_full() -> None:
    # concurrency 1 + queue 2 means the fourth concurrent arrival must be shed
    # immediately. This is the mechanism behind congestion collapse in §4.1.
    config = make_config(median_ms=5000.0, sigma=0.01, concurrency_limit=1, queue_limit=2)
    async with running_app(config) as (app, client):
        state: OriginState = app.state.origin
        occupying = [
            asyncio.create_task(client.get("/origin/result", params={"index": i}))
            for i in range(3)
        ]
        assert await wait_until(lambda: state.waiting == 2 and state.in_flight == 1)

        shed = await client.get("/origin/result", params={"index": 99})
        await cancel_all(occupying)

    assert shed.status_code == 503
    assert shed.json() == {"detail": "origin queue full"}


async def test_queue_bound_is_not_overshot_by_simultaneous_arrivals() -> None:
    config = make_config(median_ms=5000.0, sigma=0.01, concurrency_limit=1, queue_limit=2)
    async with running_app(config) as (app, client):
        state: OriginState = app.state.origin
        tasks = [
            asyncio.create_task(client.get("/origin/result", params={"index": i}))
            for i in range(12)
        ]
        assert await wait_until(lambda: state.in_flight == 1 and state.waiting == 2)
        # 1 in service + 2 queued; the other 9 must already have been shed.
        assert await wait_until(
            lambda: sum(1 for t in tasks if t.done() and t.result().status_code == 503) == 9
        )
        await cancel_all(tasks)


async def test_health_stays_responsive_while_every_slot_is_occupied() -> None:
    # The single most important test in this file. A health check that queues
    # behind real traffic blinds M1's controller exactly when it matters (§3.5).
    config = make_config(median_ms=5000.0, sigma=0.01, concurrency_limit=2, queue_limit=4)
    async with running_app(config) as (app, client):
        state: OriginState = app.state.origin
        occupying = [
            asyncio.create_task(client.get("/origin/result", params={"index": i}))
            for i in range(6)
        ]
        assert await wait_until(lambda: state.in_flight == 2 and state.waiting == 4)

        health = await asyncio.wait_for(client.get("/origin/health"), timeout=1.0)
        await cancel_all(occupying)

    assert health.status_code == 200
    assert health.json()["in_flight"] == 2


async def test_health_reports_a_nonzero_error_rate_after_shedding() -> None:
    config = make_config(
        median_ms=5000.0,
        sigma=0.01,
        concurrency_limit=1,
        queue_limit=1,
        health_window_s=5.0,
        health_buckets=5,
    )
    # err_rate_1s reads fully elapsed buckets only. A real sleep(1.1) skipped the
    # bucket holding the rejections whenever they landed late in a wall-clock
    # second, so the tracker runs on a manual clock and "one bucket later" is exact.
    clock = FakeClock(now=100.0)
    async with running_app(config) as (app, client):
        state: OriginState = app.state.origin
        state.tracker = HealthTracker(window_s=5.0, n_buckets=5, clock=clock)
        occupying = [
            asyncio.create_task(client.get("/origin/result", params={"index": i}))
            for i in range(2)
        ]
        assert await wait_until(lambda: state.in_flight == 1 and state.waiting == 1)
        for i in range(20):
            await client.get("/origin/result", params={"index": 500 + i})

        during = await client.get("/origin/health")
        clock.advance(1.0)
        after = await client.get("/origin/health")
        await cancel_all(occupying)

    # The bucket still filling is excluded, so the rate lags by one bucket...
    assert during.json()["err_rate_1s"] == 0.0
    # ...and reports the shedding once that bucket has fully elapsed.
    assert after.json()["err_rate_1s"] > 0.0


async def test_service_slots_are_released_after_completion() -> None:
    config = make_config(median_ms=2.0, sigma=0.01, concurrency_limit=2, queue_limit=8)
    async with running_app(config) as (app, client):
        state: OriginState = app.state.origin
        responses = await asyncio.gather(
            *(client.get("/origin/result", params={"index": i}) for i in range(10))
        )
        assert all(r.status_code == 200 for r in responses)
        assert state.in_flight == 0
        assert state.waiting == 0


async def test_abandoned_request_frees_its_slot_and_is_not_an_error() -> None:
    config = make_config(median_ms=5000.0, sigma=0.01, concurrency_limit=1, queue_limit=4)
    async with running_app(config) as (app, client):
        state: OriginState = app.state.origin
        task = asyncio.create_task(client.get("/origin/result", params={"index": 7}))
        assert await wait_until(lambda: state.in_flight == 1)
        await cancel_all([task])
        assert await wait_until(lambda: state.in_flight == 0)

        stats = state.tracker.window_stats()
        assert stats.abandoned == 1
        assert stats.errors == 0


async def test_sampler_emits_origin_sample_events() -> None:
    sink = MemorySink()
    config = make_config(median_ms=1.0, sample_interval_s=0.05)
    async with running_app(config, sink, start_sampler=True) as (_app, client):
        await client.get("/origin/result", params={"index": 1})
        await asyncio.sleep(0.25)

    assert len(sink.events) >= 3
    event = sink.events[0]
    assert event["event"] == "ORIGIN_SAMPLE"
    assert event["run_id"] == "test"
    assert event["mode"] == "none"
    assert set(event) >= {
        "ts", "run_id", "mode", "event", "in_flight", "waiting",
        "window_s", "served", "errors", "rejected", "abandoned",
        "p50_ms", "p99_ms", "err_rate_1s",
    }


async def test_no_samples_are_emitted_when_the_sampler_is_off() -> None:
    sink = MemorySink()
    async with running_app(make_config(median_ms=1.0), sink, start_sampler=False) as (_a, client):
        await client.get("/origin/result", params={"index": 1})
        await asyncio.sleep(0.05)
    assert sink.events == []


@pytest.mark.parametrize("concurrency", [1, 4, 16])
async def test_in_flight_never_exceeds_the_concurrency_limit(concurrency: int) -> None:
    config = make_config(
        median_ms=20.0, sigma=0.3, concurrency_limit=concurrency, queue_limit=256
    )
    peak = 0
    async with running_app(config) as (app, client):
        state: OriginState = app.state.origin

        async def watch() -> None:
            nonlocal peak
            while True:
                peak = max(peak, state.in_flight)
                await asyncio.sleep(0.001)

        watcher = asyncio.create_task(watch())
        await asyncio.gather(
            *(client.get("/origin/result", params={"index": i}) for i in range(60))
        )
        await cancel_all([watcher])

    assert 0 < peak <= concurrency
