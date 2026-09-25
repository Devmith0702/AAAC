import asyncio
import contextlib
import logging
import math
import time

import httpx

from aaac.admission.requeue import handle_timeout
from aaac.admission.store import QueueStore
from aaac.admission.window import weighted_mean_window, windows_for_all
from aaac.common.classes import AccessClass
from aaac.common.config import RunConfig
from aaac.common.events import EventLogger

log = logging.getLogger(__name__)

#: Horizon, in seconds, over which mu_hat averages the completion rate.
#:
#: MEASURED DEFECT: the estimator was `0.3*rate + 0.7*mu_hat` on a 1 s tick — a
#: ~3 s memory. Service times here run to ~300 s for a LOW client fetching a
#: 411 KB page over 512 kbit/s, so almost every tick observes zero completions
#: and mu_hat decays to zero between them. C_max = ceil(mu_hat * W_mean) then
#: collapses to 0, capacity_limit = max(0, 0 - in_flight) = 0, and the queue
#: admits nobody — which cannot recover, because admitting nobody produces no
#: completions to raise mu_hat again. Observed: C_max <= 1 on 82% of control
#: ticks under baseline and 90% under aaac, with mu_hat median 0.000.
#:
#: The horizon must exceed the service time being estimated. 60 s covers the
#: LOW-class transfer with margin at this scale.
MU_HAT_HORIZON_S = 60.0

#: Liveness floor on the concurrency cap, expressed as a fraction of what the
#: origin can serve concurrently: floor = origin.concurrency_limit // this.
#:
#: A cap of zero is not a control decision, it is a deadlock — so a floor is
#: needed. Tying it to `origin.concurrency_limit` ties it to the thing C_max
#: exists to protect, and makes it scale with the configured origin instead of
#: being a magic number.
#:
#: A FLOOR OF 1 WAS TRIED AND IS WRONG. The reasoning was that one client at a
#: time gets the whole link, which is true of the transfer and false of the
#: queue: 49 LOW clients then have to be served sequentially through a single
#: slot while all of them burn the same abandon timer. Measured under `aaac`:
#: C_max pinned at 1 for all 637 ticks, 15 admits for 49 clients in 10 minutes,
#: 47 abandoned without ever being admitted, 1/49 completions.
#:
#: The binding constraint is the shaped link, not the origin. At the gate's
#: measured 16 KB/s TCP goodput for LOW, even all 49 clients sharing the link
#: fetch `essential` (1,909 B) in ~5.7 s against a 50 s window — while the same
#: 49 sharing it for `full` (411 KB) need ~1,231 s against a 20 s window and all
#: miss. Payload adaptation is what separates those two outcomes, so the cap
#: must not be the thing that prevents clients from reaching it. The origin
#: meanwhile was measured at max 3 in-flight with 0 rejections, so a quarter of
#: its limit is conservative.
MIN_C_MAX_ORIGIN_FRACTION = 4

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
        # True once record_completion() has been called at least once. Retained
        # for observability only: it used to gate a cold-start bypass that held
        # C_max unconstrained until the first completion, which deadlocked when
        # nothing ever completed (see the note beside the c_max computation).
        # Nothing reads it now.
        self._ever_completed: bool = False

        self.is_running = False
        self._task: asyncio.Task | None = None
        
        # Async HTTP client for origin health checks
        self.http_client = httpx.AsyncClient(timeout=1.0)

    def record_completion(self) -> None:
        """Call this from the API when a ticket completes to update capacity estimates."""
        self.completions_this_tick += 1
        self._ever_completed = True

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
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
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

        # 1. Capacity estimate — mu_hat via EWMA over MU_HAT_HORIZON_S.
        #
        # The coefficient is derived from the tick and the horizon rather than
        # hard-coded, so a change to control_tick_s cannot silently shorten the
        # memory. See MU_HAT_HORIZON_S for the defect this replaced.
        comps = self.completions_this_tick
        self.completions_this_tick = 0
        rate = comps / adm_cfg.control_tick_s
        ewma_alpha = min(1.0, adm_cfg.control_tick_s / MU_HAT_HORIZON_S)
        self.mu_hat = (ewma_alpha * rate) + ((1.0 - ewma_alpha) * self.mu_hat)

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

        healthy = (
            p99 < adm_cfg.target_origin_p95_ms
            and err_rate < adm_cfg.target_origin_err_rate
        )

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
        
        # Cold-start bypass: hold C_max unconstrained until the EWMA has seen at
        # least one real completion. Do NOT gate on `mu_hat > 0` — the EWMA decays
        # geometrically but never reaches exactly 0 after any completion, so that
        # check never re-engages and C_max pins at ceil(epsilon * w_mean) = 1.
        # The floor keeps the queue alive: ceil(mu_hat * W_mean) reaches 0
        # whenever the completion estimate does, and a cap of 0 admits nobody
        # for the rest of the run. See MIN_C_MAX_ORIGIN_FRACTION.
        #
        # The cold-start bypass that used to sit here — unlimited capacity until
        # the first completion — was removed. It deadlocked in the opposite
        # direction: if nothing completes, `_ever_completed` never flips, so the
        # cap stays infinite, every waiting client is admitted at once, they
        # contend for one shared link, and none of them finishes inside its
        # window. Measured on the shaped testbed: 49 LOW clients, 1,829 admits
        # and 1,820 timeouts (~37 retry cycles each), zero completions, mu_hat
        # pinned at 0.000 for all 660 ticks. The bypass existed because the old
        # 3-second EWMA made mu_hat decay to nothing between completions; with
        # MU_HAT_HORIZON_S that estimate now recovers on its own, so the cap can
        # simply be honoured from the first tick.
        min_c_max = max(
            1, self.cfg.origin.concurrency_limit // MIN_C_MAX_ORIGIN_FRACTION
        )
        c_max = max(c_max_base, min_c_max)

        # 4. Admit tickets
        alpha_limit = int(self.alpha * adm_cfg.control_tick_s)
        capacity_limit = max(0, c_max - in_flight) if c_max != float('inf') else float('inf')
        
        n = min(alpha_limit, capacity_limit, total_waiting)
        n = int(n)

        if n > 0:
            windows_s = windows_for_all(adm_cfg, self.cfg.mode)
            admitted = await self.store.admit_n(n, now, windows_s)

            for tid, _exp in admitted:
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
        # origin_p99 uses None (→ JSON null) when origin is unreachable; float('inf')
        # is not valid JSON and breaks strict parsers.
        c_max_log = -1 if c_max == float('inf') else c_max
        p99_log = None if math.isinf(p99) else p99
        await self.logger.log(
            "CONTROL",
            ticket_id=None,
            alpha=self.alpha,
            mu_hat=self.mu_hat,
            in_flight=in_flight,
            C_max=c_max_log,
            origin_p99=p99_log
        )
