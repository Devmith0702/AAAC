"""Tests for admission/requeue.py — C4 (Non-Regressive Re-Queue).

Tests the retry downgrade logic, forced floor, and baseline divergence.
Covers Invariant I4 and I5.
"""
import pytest
import asyncio
from unittest.mock import AsyncMock

from aaac.common.classes import AccessClass
from aaac.common.config import AdmissionConfig, LoadConfig, RunConfig, EstimatorConfig, DeliveryConfig, OriginConfig
from aaac.common.events import EventLogger
from aaac.admission.store import InMemoryQueueStore
from aaac.admission.requeue import handle_timeout


@pytest.fixture
def mock_cfg():
    return RunConfig(
        run_id="test_run",
        mode="aaac",
        seed=1,
        admission=AdmissionConfig(
            w_base_s=20.0,
            kappa={"HIGH": 1.0, "MEDIUM": 1.5, "LOW": 2.5},
            w_max_s=60.0,
            alpha_min=5.0,
            alpha_max=400.0,
            alpha_increase=2.0,
            alpha_decrease=0.7,
            control_tick_s=1.0,
            target_origin_p95_ms=400.0,
            target_origin_err_rate=0.005,
            max_attempts=3,  # Set low for easier testing of forced floor
            poll_interval_ms=2000,
        ),
        estimator=EstimatorConfig(probe_bytes=1, min_rtt_samples=1, confidence_threshold=0.6, model_path=""),
        delivery=DeliveryConfig(budgets_bytes={}),
        origin=OriginConfig(service_time_ms={}, concurrency_limit=64, queue_limit=256),
        load=LoadConfig(n_clients=10, scale_factor=1, burst_center_s=1, burst_sigma_s=1, tail_decay_s=1, class_mix={"HIGH": 0.33, "MEDIUM": 0.33, "LOW": 0.34}, abandon_after_s=1)
    )

@pytest.fixture
def store():
    return InMemoryQueueStore("test_run")

@pytest.fixture
def logger():
    log = EventLogger("test_run", "aaac")
    log.log = AsyncMock()
    return log

@pytest.mark.asyncio
async def test_requeue_aaac_downgrade(store, logger, mock_cfg):
    """In AAAC mode, a TIMEOUT should downgrade class but preserve score."""
    await store.create_ticket("t1", 100, AccessClass.HIGH, AccessClass.HIGH, 1, None)
    
    # Simulate timeout
    await handle_timeout("t1", store, logger, mock_cfg)
    
    ticket = await store.get_ticket("t1")
    assert ticket.state == "WAITING"
    assert ticket.attempt == 2
    assert ticket.access_class == AccessClass.MEDIUM  # HIGH -> MEDIUM
    
    # The score should be preserved. In InMemoryQueueStore, we can verify via position.
    assert ticket.join_seq == 100
    
    # Verify events
    logger.log.assert_any_call(
        "TIMEOUT", ticket_id="t1", access_class=int(AccessClass.HIGH), true_class=int(AccessClass.HIGH), attempt=1
    )
    logger.log.assert_any_call(
        "DOWNGRADE", ticket_id="t1", access_class=int(AccessClass.MEDIUM), true_class=int(AccessClass.HIGH), attempt=1
    )
    logger.log.assert_any_call(
        "REQUEUE", ticket_id="t1", access_class=int(AccessClass.MEDIUM), true_class=int(AccessClass.HIGH), attempt=2, position=0
    )

@pytest.mark.asyncio
async def test_invariant_i4_forced_floor(store, logger, mock_cfg):
    """I4: Simulate a LOW client that fails until it reaches ESSENTIAL (LOW).
    
    When attempts >= max_attempts, it is forced to LOW and logs FORCED_FLOOR.
    """
    await store.create_ticket("t1", 100, AccessClass.HIGH, AccessClass.LOW, mock_cfg.admission.max_attempts, None)
    
    await handle_timeout("t1", store, logger, mock_cfg)
    
    ticket = await store.get_ticket("t1")
    assert ticket.access_class == AccessClass.LOW
    assert ticket.attempt == mock_cfg.admission.max_attempts + 1
    
    # Verify FORCED_FLOOR was emitted
    logger.log.assert_any_call(
        "FORCED_FLOOR", ticket_id="t1", access_class=int(AccessClass.LOW), true_class=int(AccessClass.HIGH), attempt=mock_cfg.admission.max_attempts + 1
    )

@pytest.mark.asyncio
async def test_invariant_i5_cleaner(store, logger, mock_cfg):
    """Cleaner test for I5 to prove t1 goes to the back of the line."""
    mock_cfg = RunConfig(
        run_id=mock_cfg.run_id,
        mode="baseline",
        seed=mock_cfg.seed,
        admission=mock_cfg.admission,
        estimator=mock_cfg.estimator,
        delivery=mock_cfg.delivery,
        origin=mock_cfg.origin,
        load=mock_cfg.load
    )
    
    # Sequence of events:
    # 1. t1 joins
    seq1 = await store.next_seq()
    await store.create_ticket("t1", seq1, AccessClass.HIGH, AccessClass.HIGH, 1, None)
    
    # 2. t2 joins
    seq2 = await store.next_seq()
    await store.create_ticket("t2", seq2, AccessClass.LOW, AccessClass.LOW, 1, None)
    
    # 3. Admit t1 so it moves from waiting to inflight
    await store.admit_n(1, now=100.0, windows_s={AccessClass.HIGH: 20.0, AccessClass.MEDIUM: 20.0, AccessClass.LOW: 20.0})
    
    # 4. t1 times out and is requeued in baseline mode
    await handle_timeout("t1", store, logger, mock_cfg)
    
    # 5. Check positions. t2 should now be in front of t1!
    pos_t2 = await store.position("t2")
    pos_t1 = await store.position("t1")
    
    assert pos_t2 == 0
    assert pos_t1 == 1
    
    # Also verify no downgrade occurred for t1
    ticket_t1 = await store.get_ticket("t1")
    assert ticket_t1.access_class == AccessClass.HIGH
