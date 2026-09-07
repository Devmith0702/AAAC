"""
Tests for src/aaac/admission/api.py

No Redis or network required — all tests wire InMemoryQueueStore directly.
"""
from __future__ import annotations

import pytest
import time
from fastapi.testclient import TestClient

from aaac.admission.api import app
import aaac.admission.api as api
from aaac.admission.store import InMemoryQueueStore
from aaac.common.config import get_config
from aaac.common.events import EventLogger
from aaac.admission.controller import AdmissionController
from aaac.common.classes import AccessClass
from unittest.mock import AsyncMock


# ---------------------------------------------------------------------------
# Fixture: wire in-memory store so tests don't need Redis or lifespan
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
async def setup_api():
    cfg = get_config()
    api.cfg = cfg
    api.store = InMemoryQueueStore(cfg.run_id)
    api.logger = EventLogger(cfg.run_id, cfg.mode)
    api.logger.log = AsyncMock()          # avoid file I/O in tests
    api.controller = AdmissionController(api.store, api.logger, cfg)
    yield
    # no cleanup needed — InMemory store is GC'd


# ---------------------------------------------------------------------------
# Basic route smoke tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_join_endpoint():
    with TestClient(app) as client:
        response = client.post("/queue/join", json={"client_id": "c1", "true_class": 0})
    assert response.status_code == 200
    data = response.json()
    assert "ticket_id" in data
    assert data["join_seq"] == 1
    assert data["position"] == 0
    assert data["eta_s"] == 0.0
    assert "poll_interval_ms" in data


@pytest.mark.asyncio
async def test_estimate_endpoint():
    with TestClient(app) as client:
        join_res = client.post("/queue/join", json={"client_id": "c1", "true_class": 0})
        tid = join_res.json()["ticket_id"]

        est_req = {
            "ticket_id": tid,
            "throughput_kbps": 1000.0,
            "rtt_mean_ms": 50.0,
            "rtt_jitter_ms": 10.0,
            "loss_ratio": 0.0,
            "stability": 1.0,
            "access_class": 1,   # MEDIUM — downgrade from MEDIUM default stays MEDIUM
            "confidence": 0.9,
            "model_version": "v1",
            "fallback": False,
        }
        est_res = client.post("/queue/estimate", json=est_req)
    assert est_res.status_code == 200
    assert est_res.json() == {"accepted": True, "access_class": 1}

    ticket = await api.store.get_ticket(tid)
    assert ticket.access_class == AccessClass.MEDIUM


@pytest.mark.asyncio
async def test_status_endpoint():
    with TestClient(app) as client:
        join_res = client.post("/queue/join", json={"client_id": "c1", "true_class": 0})
        tid = join_res.json()["ticket_id"]
        status_res = client.get(f"/queue/status/{tid}")
    assert status_res.status_code == 200
    data = status_res.json()
    assert data["ticket_id"] == tid
    assert data["state"] == "WAITING"
    assert data["position"] == 0
    assert data["admit_token"] is None


@pytest.mark.asyncio
async def test_complete_endpoint():
    with TestClient(app) as client:
        join_res = client.post("/queue/join", json={"client_id": "c1", "true_class": 0})
        tid = join_res.json()["ticket_id"]
        comp_res = client.post("/queue/complete", json={
            "ticket_id": tid,
            "ok": True,
            "bytes": 1024,
            "duration_ms": 500,
            "variant": "full",
        })
    assert comp_res.status_code == 200
    assert comp_res.json() == {"state": "COMPLETED"}

    ticket = await api.store.get_ticket(tid)
    assert ticket.state == "COMPLETED"


@pytest.mark.asyncio
async def test_snapshot_endpoint():
    with TestClient(app) as client:
        res = client.get("/admin/snapshot")
    assert res.status_code == 200
    data = res.json()
    assert data["run_id"] == api.cfg.run_id
    assert "waiting" in data
    assert "completed" in data
    assert "timed_out" in data


# ---------------------------------------------------------------------------
# D1: true_class round-trip — enum value preserved for all three classes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cls_int,cls_enum", [
    (0, AccessClass.HIGH),
    (1, AccessClass.MEDIUM),
    (2, AccessClass.LOW),
])
@pytest.mark.asyncio
async def test_join_true_class_roundtrip(cls_int, cls_enum):
    """D1: true_class sent as int must be stored as the exact AccessClass value."""
    with TestClient(app) as client:
        res = client.post("/queue/join", json={"client_id": "c1", "true_class": cls_int})
    assert res.status_code == 200, res.text
    tid = res.json()["ticket_id"]

    ticket = await api.store.get_ticket(tid)
    assert ticket.true_class == cls_enum, (
        f"true_class round-trip failed: sent {cls_int}, got {ticket.true_class!r}"
    )


@pytest.mark.asyncio
async def test_join_invalid_true_class_rejected():
    """D1: A string like 'HIGH' must be rejected with 422, not silently coerced."""
    with TestClient(app) as client:
        res = client.post("/queue/join", json={"client_id": "c1", "true_class": "HIGH"})
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_join_out_of_range_true_class_rejected():
    """D1: An int outside 0/1/2 must be rejected with 422."""
    with TestClient(app) as client:
        res = client.post("/queue/join", json={"client_id": "c1", "true_class": 99})
    assert res.status_code == 422


# ---------------------------------------------------------------------------
# D2: /queue/estimate must reject upgrades and attempt > 1
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_estimate_rejected_on_requeued_ticket():
    """D2: estimate on attempt > 1 must return accepted=false, closing the I2 upgrade hole."""
    with TestClient(app) as client:
        join_res = client.post("/queue/join", json={"client_id": "c1", "true_class": 0})
        tid = join_res.json()["ticket_id"]

        # Manually bump attempt to simulate a re-queue (while same store is live)
        api.store._tickets[tid]["attempt"] = 2

        est_res = client.post("/queue/estimate", json={
            "ticket_id": tid,
            "throughput_kbps": 5000.0,
            "rtt_mean_ms": 15.0,
            "rtt_jitter_ms": 2.0,
            "loss_ratio": 0.0,
            "stability": 1.0,
            "access_class": 0,   # HIGH — would be an upgrade from MEDIUM
            "confidence": 0.95,
            "model_version": "v1",
            "fallback": False,
        })
        assert est_res.status_code == 200
        data = est_res.json()
        assert data["accepted"] is False, (
            "estimate on attempt=2 must be rejected (D2 — closes I2 upgrade hole)"
        )

        # Confirm class was NOT changed
        ticket = await api.store.get_ticket(tid)
        assert ticket.access_class == AccessClass.MEDIUM


@pytest.mark.asyncio
async def test_estimate_accepted_when_upgrading_from_medium_default():
    """
    Guard 2 was removed: a HIGH estimate on attempt=1 must now be ACCEPTED.

    MEDIUM at join is an absence-of-information placeholder, not a prior
    measurement. The establishing estimate can legitimately place the ticket
    at HIGH. Rejecting it made HIGH classification impossible, which broke Δ
    (the completion-rate gap between HIGH and LOW that the project reports).

    Only attempt > 1 estimates are blocked (guard 1, I2 enforcement).
    """
    with TestClient(app) as client:
        join_res = client.post("/queue/join", json={"client_id": "c1", "true_class": 0})
        tid = join_res.json()["ticket_id"]

        # Ticket starts at MEDIUM default; HIGH estimate on attempt=1 must succeed.
        est_res = client.post("/queue/estimate", json={
            "ticket_id": tid,
            "throughput_kbps": 5000.0,
            "rtt_mean_ms": 10.0,
            "rtt_jitter_ms": 1.0,
            "loss_ratio": 0.0,
            "stability": 1.0,
            "access_class": 0,   # HIGH — from MEDIUM default, attempt=1
            "confidence": 0.99,
            "model_version": "v1",
            "fallback": False,
        })
        assert est_res.json()["accepted"] is True, (
            "HIGH estimate on attempt=1 must be accepted — MEDIUM is a placeholder, "
            "not a prior measurement"
        )

        ticket = await api.store.get_ticket(tid)
        assert ticket.access_class == AccessClass.HIGH



@pytest.mark.asyncio
async def test_estimate_class_monotone_across_timeout_sequence():
    """
    D2 property test: across a sequence of estimates interleaved with simulated
    timeouts (attempt increments), access_class must be monotonically non-increasing
    (numerically non-decreasing, since HIGH=0 < MEDIUM=1 < LOW=2).

    Uses a single-use TestClient (not the context-manager form) so that async
    store awaits can interleave with HTTP calls without mixing sync/async contexts.
    """
    from aaac.admission.requeue import handle_timeout
    from aaac.common.config import get_config
    import time as _time

    cfg = get_config()
    store = api.store
    logger_mock = api.logger

    # Use a bare client (no lifespan start/stop — store already wired by fixture)
    http = TestClient(app, raise_server_exceptions=True)

    # Join — default access_class=MEDIUM
    res = http.post("/queue/join", json={"client_id": "c1", "true_class": 0})
    assert res.status_code == 200
    tid = res.json()["ticket_id"]

    class_history = []
    t0 = await store.get_ticket(tid)
    assert t0 is not None
    class_history.append(int(t0.access_class))

    # Simulate 5 rounds: try upgrade estimate, then force timeout + requeue
    for _ in range(5):
        ticket = await store.get_ticket(tid)

        if ticket.attempt == 1:
            # Attempt an upgrade from MEDIUM → HIGH. Must be rejected by D2 + upgrade guard.
            http.post("/queue/estimate", json={
                "ticket_id": tid,
                "throughput_kbps": 5000.0,
                "rtt_mean_ms": 10.0,
                "rtt_jitter_ms": 1.0,
                "loss_ratio": 0.0,
                "stability": 1.0,
                "access_class": 0,   # HIGH — upgrade attempt from MEDIUM
                "confidence": 0.99,
                "model_version": "v1",
                "fallback": False,
            })

        # Admit ticket so expire_inflight can find it in the inflight set
        windows = {cls: 0.001 for cls in AccessClass}  # tiny window
        await store.admit_n(1, _time.time(), windows)
        await store.expire_inflight(_time.time() + 10)

        # Timeout and re-queue (downgrades class)
        await handle_timeout(tid, store, logger_mock, cfg)

        ticket = await store.get_ticket(tid)
        class_history.append(int(ticket.access_class))

    # Monotonically non-decreasing: HIGH=0 is best, LOW=2 is worst
    for i in range(1, len(class_history)):
        assert class_history[i] >= class_history[i - 1], (
            f"I2 violation: access_class went {class_history[i-1]} → {class_history[i]} "
            f"(upgrade) at step {i}. Full history: {class_history}"
        )



# ---------------------------------------------------------------------------
# D3: none mode — ticket is ADMITTED immediately, client never waits
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_none_mode_join_returns_admitted_immediately():
    """D3: in none mode, join must produce an immediately ADMITTED ticket."""
    original_mode = api.cfg.mode
    try:
        # Temporarily switch to none mode
        object.__setattr__(api.cfg, "mode", "none")

        with TestClient(app) as client:
            res = client.post("/queue/join", json={"client_id": "c1", "true_class": 0})
        assert res.status_code == 200
        data = res.json()
        assert data["position"] == 0
        assert data["eta_s"] == 0.0

        tid = data["ticket_id"]
        ticket = await api.store.get_ticket(tid)
        assert ticket.state == "ADMITTED", (
            "D3: none mode must immediately ADMIT the ticket, client must never wait"
        )
    finally:
        object.__setattr__(api.cfg, "mode", original_mode)


@pytest.mark.asyncio
async def test_none_mode_status_returns_token():
    """D3: in none mode, /queue/status must return a valid admit_token immediately."""
    original_mode = api.cfg.mode
    try:
        object.__setattr__(api.cfg, "mode", "none")

        with TestClient(app) as client:
            join_res = client.post("/queue/join", json={"client_id": "c1", "true_class": 1})
            tid = join_res.json()["ticket_id"]
            status_res = client.get(f"/queue/status/{tid}")

        assert status_res.status_code == 200
        data = status_res.json()
        assert data["state"] == "ADMITTED"
        assert data["admit_token"] is not None, "none mode must provide token immediately"
        assert data["position"] == 0
    finally:
        object.__setattr__(api.cfg, "mode", original_mode)
