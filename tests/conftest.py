"""Shared test helpers.

CLAUDE.md §3.10 rule 4: unit tests must not require Redis or the network. Every
test here drives the ASGI app in-process through ``httpx.ASGITransport`` and
injects a config object and a :class:`MemorySink`, so nothing touches a socket or
the filesystem.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI

from aaac.origin.config import OriginConfig, RunConfig, ServiceTimeConfig
from aaac.origin.service import create_app
from aaac.origin.sinks import EventSink, MemorySink


def make_config(
    *,
    seed: int = 1,
    run_id: str = "test",
    mode: str = "none",
    median_ms: float = 60.0,
    sigma: float = 0.5,
    concurrency_limit: int = 64,
    queue_limit: int = 256,
    health_window_s: float = 5.0,
    health_buckets: int = 5,
    sample_interval_s: float = 1.0,
    results_dir: str = "results",
) -> RunConfig:
    return RunConfig(
        seed=seed,
        run_id=run_id,
        mode=mode,
        results_dir=Path(results_dir),
        origin=OriginConfig(
            service_time=ServiceTimeConfig(dist="lognormal", median_ms=median_ms, sigma=sigma),
            concurrency_limit=concurrency_limit,
            queue_limit=queue_limit,
            health_window_s=health_window_s,
            health_buckets=health_buckets,
            sample_interval_s=sample_interval_s,
        ),
    )


@asynccontextmanager
async def running_app(
    config: RunConfig,
    sink: EventSink | None = None,
    *,
    start_sampler: bool = False,
) -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient]]:
    """Run the origin app in-process with its lifespan active."""
    event_sink = sink if sink is not None else MemorySink()
    app = create_app(config, event_sink, start_sampler=start_sampler)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://origin") as client:
            yield app, client


async def wait_until(predicate: Callable[[], bool], timeout: float = 3.0) -> bool:
    """Poll ``predicate`` until true. Avoids timing-sleep flakiness in concurrency tests."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return predicate()


async def cancel_all(tasks: list[asyncio.Task[object]]) -> None:
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


class FakeClock:
    """Manually advanced monotonic clock for deterministic window tests."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds
