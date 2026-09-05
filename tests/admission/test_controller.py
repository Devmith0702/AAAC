import pytest
import asyncio
import math
from unittest.mock import AsyncMock, MagicMock, patch

from aaac.common.classes import AccessClass
from aaac.common.config import AdmissionConfig, LoadConfig, RunConfig, EstimatorConfig, DeliveryConfig, OriginConfig
from aaac.common.events import EventLogger
from aaac.admission.store import InMemoryQueueStore
from aaac.admission.controller import AdmissionController
from aaac.admission.window import weighted_mean_window


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
            max_attempts=5,
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
    # Stub the actual file writing to avoid disk I/O in tests
    log._flush_unlocked = AsyncMock()
    log.log = AsyncMock()
    return log

@pytest.fixture
def controller(store, logger, mock_cfg):
    ctrl = AdmissionController(store, logger, mock_cfg)
    return ctrl

@pytest.mark.asyncio
async def test_aimd_scale_up_when_healthy(controller, store):
    """When origin is healthy, alpha should increase additively."""
    # Mock origin healthy
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"p99_ms": 100.0, "err_rate_1s": 0.0}
    controller.http_client.get = AsyncMock(return_value=mock_resp)
    
    start_alpha = controller.alpha
    await controller._tick()
    
    assert controller.alpha == start_alpha + controller.cfg.admission.alpha_increase

@pytest.mark.asyncio
async def test_aimd_scale_down_when_unhealthy(controller, store):
    """When origin is hostile, alpha should decrease multiplicatively."""
    # Mock origin hostile (p99 = 500ms > 400ms target)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"p99_ms": 500.0, "err_rate_1s": 0.0}
    controller.http_client.get = AsyncMock(return_value=mock_resp)
    
    # Artificially boost alpha so we can see it drop
    controller.alpha = 100.0
    await controller._tick()
    
    assert controller.alpha == 100.0 * controller.cfg.admission.alpha_decrease

@pytest.mark.asyncio
async def test_aimd_clamps_to_min_max(controller, store):
    """Alpha must never drop below alpha_min or exceed alpha_max."""
    # Mock origin hostile
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"p99_ms": 500.0, "err_rate_1s": 0.0}
    controller.http_client.get = AsyncMock(return_value=mock_resp)
    
    # Try to drop it below min
    controller.alpha = controller.cfg.admission.alpha_min
    await controller._tick()
    assert controller.alpha == controller.cfg.admission.alpha_min
    
    # Mock origin healthy
    mock_resp.json.return_value = {"p99_ms": 100.0, "err_rate_1s": 0.0}
    controller.alpha = controller.cfg.admission.alpha_max
    await controller._tick()
    assert controller.alpha == controller.cfg.admission.alpha_max

@pytest.mark.asyncio
async def test_mu_hat_ewma_calculation(controller, store):
    """Capacity estimate mu_hat should track completions using EWMA (alpha=0.3)."""
    # Tick 1: 10 completions
    controller.record_completion() # Need 10
    controller.completions_this_tick = 10
    
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"p99_ms": 100.0, "err_rate_1s": 0.0}
    controller.http_client.get = AsyncMock(return_value=mock_resp)
    
    await controller._tick()
    
    rate = 10 / controller.cfg.admission.control_tick_s
    expected_mu_hat = (0.3 * rate) + (0.7 * 0.0)
    assert controller.mu_hat == pytest.approx(expected_mu_hat)
    
    # Tick 2: 5 completions
    controller.completions_this_tick = 5
    await controller._tick()
    
    rate2 = 5 / controller.cfg.admission.control_tick_s
    expected_mu_hat2 = (0.3 * rate2) + (0.7 * expected_mu_hat)
    assert controller.mu_hat == pytest.approx(expected_mu_hat2)

@pytest.mark.asyncio
async def test_invariant_i3_in_flight_le_c_max(controller, store, mock_cfg):
    """I3: in_flight <= C_max at every tick (controller unit test with a hostile origin)."""
    # 1. Fill the queue with many waiting tickets
    for i in range(500):
        await store.create_ticket(f"t{i}", i, AccessClass.HIGH, AccessClass.HIGH, 1, None)
        
    # 2. Mock origin as HOSTILE (always unhealthy)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"p99_ms": 1500.0, "err_rate_1s": 0.5}
    controller.http_client.get = AsyncMock(return_value=mock_resp)
    
    # 3. Simulate multiple ticks
    controller.alpha = 50.0  # Start with high alpha
    
    for tick in range(10):
        # Provide some fake completions so mu_hat is non-zero
        controller.completions_this_tick = 10 
        
        await controller._tick()
        
        # Compute expected C_max exactly as controller does
        total_waiting = await store.waiting_count()
        mix = mock_cfg.load.class_mix
        waiting_counts = {
            AccessClass.HIGH: int(total_waiting * mix.get("HIGH", 0.33)),
            AccessClass.MEDIUM: int(total_waiting * mix.get("MEDIUM", 0.33)),
            AccessClass.LOW: int(total_waiting * mix.get("LOW", 0.34))
        }
        w_mean = weighted_mean_window(waiting_counts, mock_cfg.admission, mock_cfg.mode)
        c_max = math.ceil(controller.mu_hat * w_mean) if controller.mu_hat > 0 else float('inf')
        
        in_flight = await store.inflight_count()
        
        # INVARIANT I3 assertion
        if c_max != float('inf'):
            assert in_flight <= c_max, f"Tick {tick}: in_flight ({in_flight}) exceeded C_max ({c_max})"
