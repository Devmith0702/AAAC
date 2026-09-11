"""Mock origin service — the overloadable stand-in for the results portal.

CLAUDE.md §3.5 and §4.1.

| Method | Path | Returns |
|---|---|---|
| GET | ``/origin/result?index=<index_no>`` | JSON record; 503 past the queue limit |
| GET | ``/origin/health`` | ``{in_flight, p99_ms, err_rate_1s}`` |

The bounded queue in front of the concurrency semaphore is the mechanism that
makes ``mode: none`` collapse (§4.1). If it does not collapse, the problem
statement is undemonstrated — so the shedding path is load-bearing, not defensive
plumbing.

``/origin/health`` deliberately touches nothing but the health tracker. It never
acquires the semaphore and never awaits I/O, because a health check that queues
behind real traffic blinds M1's controller exactly when it matters (§3.5).

Note on §3.10 rule 3 ("no sleeping in request handlers"): the rule forbids
*blocking* the event loop. Simulating service time is this service's entire
purpose, so it awaits ``asyncio.sleep``, which yields rather than blocks.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from aaac.origin.config import RunConfig, load_run_config
from aaac.origin.health import HealthTracker
from aaac.origin.records import make_result_record
from aaac.origin.service_time import service_time_ms
from aaac.origin.sinks import EventSink, JsonlSink

_NO_STORE = {"Cache-Control": "no-store"}


@dataclass
class OriginState:
    """Everything the request handlers touch. Single-threaded, asyncio-owned."""

    config: RunConfig
    sink: EventSink
    tracker: HealthTracker
    sem: asyncio.Semaphore
    monotonic: Callable[[], float] = time.monotonic
    wall: Callable[[], float] = time.time
    waiting: int = 0
    in_flight: int = 0
    sampler: asyncio.Task[None] | None = field(default=None, repr=False)


@asynccontextmanager
async def _serving_slot(state: OriginState) -> AsyncIterator[None]:
    """Occupy one of ``concurrency_limit`` service slots, queueing if necessary."""
    state.waiting += 1
    try:
        await state.sem.acquire()
    finally:
        state.waiting -= 1
    state.in_flight += 1
    try:
        yield
    finally:
        state.in_flight -= 1
        state.sem.release()


async def _sampler_loop(state: OriginState) -> None:
    """Emit ``ORIGIN_SAMPLE`` once per ``sample_interval_s`` (§4.1)."""
    interval = state.config.origin.sample_interval_s
    next_at = state.monotonic()
    while True:
        next_at += interval
        await asyncio.sleep(max(0.0, next_at - state.monotonic()))
        stats = state.tracker.window_stats()
        await state.sink.emit(
            {
                "ts": state.wall(),
                "run_id": state.config.run_id,
                "mode": state.config.mode,
                "event": "ORIGIN_SAMPLE",
                "in_flight": state.in_flight,
                "waiting": state.waiting,
                "window_s": stats.window_s,
                "served": stats.served,
                "errors": stats.errors,
                "rejected": stats.rejected,
                "abandoned": stats.abandoned,
                "p50_ms": round(stats.p50_ms, 3),
                "p99_ms": round(stats.p99_ms, 3),
                "err_rate_1s": round(stats.err_rate_1s, 6),
            }
        )


def create_app(
    config: RunConfig | None = None,
    sink: EventSink | None = None,
    *,
    start_sampler: bool = True,
) -> FastAPI:
    """Build the origin ASGI app.

    ``config`` and ``sink`` are injectable so tests need neither a config file
    nor a writable results directory (§3.10 rule 4).
    """
    cfg = config if config is not None else load_run_config()
    event_sink = sink if sink is not None else JsonlSink(cfg.origin_events_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        state: OriginState = app.state.origin
        await state.sink.start()
        if start_sampler:
            state.sampler = asyncio.create_task(_sampler_loop(state), name="origin-sampler")
        try:
            yield
        finally:
            if state.sampler is not None:
                state.sampler.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await state.sampler
                state.sampler = None
            await state.sink.aclose()

    app = FastAPI(title="AAAC mock origin", version="0.1.0", lifespan=lifespan)
    app.state.origin = OriginState(
        config=cfg,
        sink=event_sink,
        tracker=HealthTracker(
            window_s=cfg.origin.health_window_s,
            n_buckets=cfg.origin.health_buckets,
        ),
        sem=asyncio.Semaphore(cfg.origin.concurrency_limit),
    )

    @app.get("/origin/result")
    async def origin_result(index: int = Query(..., ge=0)) -> JSONResponse:
        state: OriginState = app.state.origin

        # Check-then-increment with no await in between, so this is atomic under
        # asyncio and the queue bound cannot be overshot by concurrent arrivals.
        if state.waiting >= state.config.origin.queue_limit:
            state.tracker.record_rejected()
            return JSONResponse(
                {"detail": "origin queue full"}, status_code=503, headers=_NO_STORE
            )

        async with _serving_slot(state):
            started = state.monotonic()
            try:
                delay_ms = service_time_ms(
                    state.config.origin.service_time, state.config.seed, index
                )
                await asyncio.sleep(delay_ms / 1000.0)
            except asyncio.CancelledError:
                # The client went away mid-service. Not an origin error — timed
                # out clients are an expected feature of this experiment — so it
                # is counted separately and kept out of the latency histogram.
                state.tracker.record_abandoned()
                raise
            record = make_result_record(state.config.seed, index)
            state.tracker.record_served((state.monotonic() - started) * 1000.0, ok=True)

        return JSONResponse(record, headers=_NO_STORE)

    @app.get("/origin/health")
    async def origin_health() -> JSONResponse:
        # Never acquires the semaphore, never awaits I/O (§3.5). The response
        # body is exactly the three contract keys — extra diagnostics go to
        # ORIGIN_SAMPLE, not here, so M1's consumer sees a stable shape.
        state: OriginState = app.state.origin
        stats = state.tracker.window_stats()
        payload: dict[str, Any] = {
            "in_flight": state.in_flight,
            "p99_ms": round(stats.p99_ms, 3),
            "err_rate_1s": round(stats.err_rate_1s, 6),
        }
        return JSONResponse(payload, headers=_NO_STORE)

    return app
