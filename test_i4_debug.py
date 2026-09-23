import asyncio
from unittest.mock import AsyncMock

from aaac.common.classes import AccessClass
from aaac.common.config import AdmissionConfig, LoadConfig, RunConfig, EstimatorConfig, DeliveryConfig, OriginConfig
from aaac.common.events import EventLogger
from aaac.admission.store import InMemoryQueueStore
from aaac.admission.requeue import handle_timeout

async def main():
    mock_cfg = RunConfig(
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

    store = InMemoryQueueStore("test_run")
    logger = EventLogger("test_run", "aaac")
    logger.log = AsyncMock()

    await store.create_ticket("t1", 100, AccessClass.HIGH, AccessClass.LOW, mock_cfg.admission.max_attempts, None)
    
    await handle_timeout("t1", store, logger, mock_cfg)

    print("Calls made to logger.log:")
    for call in logger.log.call_args_list:
        print(call)

asyncio.run(main())
