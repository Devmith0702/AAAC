import asyncio
import math
import time
import httpx
import logging
from aaac.common.classes import AccessClass
from aaac.common.config import RunConfig
from aaac.common.events import EventLogger
from aaac.admission.store import QueueStore
from aaac.admission.window import windows_for_all, weighted_mean_window
from aaac.admission.requeue import handle_timeout

log = logging.getLogger(__name__)

class AdmissionController:
    """C5: Capacity-Tracking Admission Rate Controller."""

    def __init__(
        self,
        store: QueueStore,
        logger: EventLogger,
        cfg: RunConfig,
        origin_url: str = "http://origin:8002",
        requeue_handler=handle_timeout
    ):
        self.store = store
        self.logger = logger
        self.cfg = cfg
        self.origin_url = origin_url
        self.requeue_handler = requeue_handler

        self.completions_this_tick = 0
        self.alpha = float(cfg.admission.alpha_min)
        self.mu_hat = 0.0

        self.is_running = False
        self._task: asyncio.Task | None = None
        
        # Async HTTP client for origin health checks
        self.http_client = httpx.AsyncClient(timeout=1.0)

    def record_completion(self) -> None:
        """Call this from the API when a ticket completes to update capacity estimates."""
        self.completions_this_tick += 1

    async def start(self) -> None:
        """Start the background controller loop."""
        if self.is_running:
            return
        self.is_running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Stop the background controller loop."""
        self.is_running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self.http_client.aclose()

    async def _loop(self) -> None:
        tick_s = self.cfg.admission.control_tick_s
        while self.is_running:
            start_time = time.time()
            try:
                await self._tick()
            except Exception:
                # Log error but don't crash the controller loop
                log.exception("Error in controller tick")

            elapsed = time.time() - start_time
            sleep_time = max(0.0, tick_s - elapsed)
            try:
                await asyncio.sleep(sleep_time)
            except asyncio.CancelledError:
                break

    async def _tick(self) -> None:
        now = time.time()
        adm_cfg = self.cfg.admission

        # 1. Capacity estimate — mu_hat via EWMA (alpha=0.3)
        comps = self.completions_this_tick
        self.completions_this_tick = 0
        rate = comps / adm_cfg.control_tick_s
        self.mu_hat = (0.3 * rate) + (0.7 * self.mu_hat)

        # 2. Rate control (AIMD on origin health)
        try:
            resp = await self.http_client.get(f"{self.origin_url}/origin/health")
            if resp.status_code == 200:
                health = resp.json()
                p99 = health.get("p99_ms", 0.0)
                err_rate = health.get("err_rate_1s", 0.0)
            else:
                p99 = float('inf')
                err_rate = 1.0
        except Exception:
            p99 = float('inf')
            err_rate = 1.0

        healthy = (p99 < adm_cfg.target_origin_p95_ms) and (err_rate < adm_cfg.target_origin_err_rate)

        if healthy:
            self.alpha += adm_cfg.alpha_increase
        else:
            self.alpha *= adm_cfg.alpha_decrease

        self.alpha = max(adm_cfg.alpha_min, min(self.alpha, adm_cfg.alpha_max))

        # 3. Concurrency cap (C_max)
        total_waiting = await self.store.waiting_count()
        in_flight = await self.store.inflight_count()

        # Approximate waiting counts based on configured class mix.
        # This avoids expensive O(N) Redis scans while providing a stable W_mean.
        mix = self.cfg.load.class_mix
        waiting_counts = {
            AccessClass.HIGH: int(total_waiting * mix.get("HIGH", 0.33)),
            AccessClass.MEDIUM: int(total_waiting * mix.get("MEDIUM", 0.33)),
            AccessClass.LOW: int(total_waiting * mix.get("LOW", 0.34))
        }

        w_mean = weighted_mean_window(waiting_counts, adm_cfg, self.cfg.mode)
        c_max_base = math.ceil(self.mu_hat * w_mean)
        
        # Cold start: if mu_hat is 0, we bypass C_max to allow initial tickets to flow.
        c_max = c_max_base if self.mu_hat > 0 else float('inf')

        # 4. Admit tickets
        alpha_limit = int(self.alpha * adm_cfg.control_tick_s)
        capacity_limit = max(0, c_max - in_flight) if c_max != float('inf') else float('inf')
        
        n = min(alpha_limit, capacity_limit, total_waiting)
        n = int(n)

        if n > 0:
            windows_s = windows_for_all(adm_cfg, self.cfg.mode)
            admitted = await self.store.admit_n(n, now, windows_s)

            for tid, exp in admitted:
                ticket = await self.store.get_ticket(tid)
                if not ticket:
                    continue

                await self.logger.log(
                    "ADMIT",
                    ticket_id=tid,
                    access_class=int(ticket.access_class),
                    true_class=int(ticket.true_class),
                    attempt=ticket.attempt,
                    bytes=0,
                    duration_ms=None,
                    variant=None,
                    position=0
                )

        # 5. Sweep expired inflight
        expired_tids = await self.store.expire_inflight(now)
        for tid in expired_tids:
            # Re-queue logic (C4) will happen here
            await self.requeue_handler(tid, self.store, self.logger, self.cfg)

        # 6. Emit CONTROL event
        c_max_log = -1 if c_max == float('inf') else c_max
        await self.logger.log(
            "CONTROL",
            ticket_id=None,
            alpha=self.alpha,
            mu_hat=self.mu_hat,
            in_flight=in_flight,
            C_max=c_max_log,
            origin_p99=p99
        )
