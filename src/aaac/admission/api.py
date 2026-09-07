"""
Admission Control API — M1

Design decisions implemented (see README / task spec for context):
  D1  true_class on /queue/join is typed as AccessClass (IntEnum), not raw int.
      Pydantic coerces ints; strings are rejected 422. Rationale: M3 scores
      classifier accuracy directly against this field — a silently coerced wrong
      label would produce a confidently wrong accuracy number.

  D2  /queue/estimate rejects when ticket.attempt > 1, returning {accepted:False}.
      NOTE: the earlier WAITING-state guard does NOT close the I2 upgrade hole;
      a re-queued ticket is WAITING again, so a fresh HIGH estimate on attempt 2
      would upgrade HIGH->MEDIUM back to HIGH. The attempt guard closes it.

  D3  none mode is implemented server-side as an immediately-ADMITTED ticket with
      a large window token. Clients join and poll exactly as in every other mode
      and simply never wait. This preserves client mode-blindness (required by M2).
"""
import asyncio
import logging
import math
import os
import time
import json
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from aaac.common.config import get_config
from aaac.common.classes import AccessClass
from aaac.common.schemas import LinkEstimate, TicketStatus
from aaac.common.events import EventLogger
from aaac.common.tokens import issue_token
from aaac.admission.store import InMemoryQueueStore, RedisQueueStore, QueueStore
from aaac.admission.controller import AdmissionController
from aaac.admission.window import window_for




log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------

class JoinRequest(BaseModel):
    client_id: str
    # D1: typed as AccessClass so Pydantic rejects strings and invalid ints at
    # parse time. Clients must send 0/1/2 — matching how LinkEstimate.access_class
    # is serialised.
    true_class: AccessClass

class CompleteRequest(BaseModel):
    ticket_id: str
    ok: bool
    bytes: int
    duration_ms: float
    variant: str

# ---------------------------------------------------------------------------
# Module-level state (populated in lifespan, replaced in tests)
# ---------------------------------------------------------------------------
cfg = get_config()

# NOTE on ticket_id generation: we use uuid.uuid4() (cryptographically random),
# NOT a seeded RNG. §3.10 Rule 5 requires reproducible control decisions
# (AIMD α, class assignment); it does not require reproducible identifiers.
# A seeded module-level RNG replays the same ID sequence on every restart,
# causing ticket_id collisions that corrupt M3's event log and classifier scoring.
store: QueueStore = None          # type: ignore[assignment]
logger: EventLogger = None        # type: ignore[assignment]
controller: AdmissionController = None  # type: ignore[assignment]

# Window for none-mode immediate admission (large enough that the client will
# always complete before it expires regardless of payload size).
_NONE_MODE_WINDOW_S = 3600.0

# ---------------------------------------------------------------------------
# Lifespan: use Redis only when AAAC_REDIS_URL is set, otherwise InMemory.
# This lets M2's integration harness run without Redis.
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global store, logger, controller

    redis_url = os.environ.get("AAAC_REDIS_URL", "")
    if redis_url:
        import redis.asyncio as redis
        redis_client = redis.from_url(redis_url)
        store = RedisQueueStore(redis_client, cfg.run_id)
        log.info("Using RedisQueueStore at %s", redis_url)
    else:
        store = InMemoryQueueStore(cfg.run_id)
        log.info("Using InMemoryQueueStore (set AAAC_REDIS_URL for Redis)")

    logger = EventLogger(cfg.run_id, cfg.mode)
    # origin_url is read from env so the harness can point to its own origin stub
    origin_url = os.environ.get("AAAC_ORIGIN_URL", "http://origin:8002")
    controller = AdmissionController(store, logger, cfg, origin_url=origin_url)
    await controller.start()

    yield

    await controller.stop()
    await logger.close()
    if redis_url:
        # noinspection PyUnboundLocalVariable
        await redis_client.aclose()

# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

app = FastAPI(title="AAAC Admission Core", version="1.0.0", lifespan=lifespan)

# ---------------------------------------------------------------------------
# POST /queue/join
# ---------------------------------------------------------------------------

@app.post("/queue/join")
async def queue_join(req: JoinRequest):
    # Finding 2 fix: use uuid.uuid4() so IDs are unique across restarts.
    # A seeded RNG at module scope replays the same sequence every restart,
    # causing collisions that corrupt the event log. See implementation_plan.md.
    tid = uuid.uuid4().hex
    seq = await store.next_seq()

    # D3: none mode — immediately admit with a large window so the client
    # never waits, while still following the identical join→status→complete
    # protocol that M2 uses in baseline and aaac modes.
    if cfg.mode == "none":
        # none mode reproduces congestion collapse: the portal as it exists today,
        # everyone getting the full 411 KB page, origin unconstrained. Using HIGH
        # (full variant) is deliberate — MEDIUM would serve reduced at ~5 KB, which
        # is payload adaptation (C3), exactly the mechanism the control condition
        # exists to lack. §0.3's MEDIUM default is a safety rule for when AAAC
        # can't classify a client; none isn't a classification failure, it's the
        # deliberate absence of the mechanism, so the default doesn't apply.
        window_s = _NONE_MODE_WINDOW_S
        expires_at = time.time() + window_s
        # Create ticket already in ADMITTED state
        await store.create_ticket(
            tid=tid,
            seq=seq,
            true_class=req.true_class,
            access_class=AccessClass.HIGH,   # none mode: everyone gets full variant
            attempt=1,
            expires_at=expires_at,
        )
        # Mark it admitted immediately by calling admit_n(1) so store state is
        # consistent (ADMITTED, inflight counters correct).
        # We pass a large window dict so it won't time out.
        windows = {cls: _NONE_MODE_WINDOW_S for cls in AccessClass}
        await store.admit_n(1, time.time(), windows)
        admit_token = issue_token(tid, AccessClass.HIGH, 1, _NONE_MODE_WINDOW_S)
        await logger.log(
            "JOIN",
            ticket_id=tid,
            access_class=int(AccessClass.HIGH),
            true_class=int(req.true_class),
            attempt=1,
            position=0,
        )
        await logger.log(
            "ADMIT",
            ticket_id=tid,
            access_class=int(AccessClass.HIGH),
            true_class=int(req.true_class),
            attempt=1,
            position=0,
        )
        return {
            "ticket_id": tid,
            "join_seq": seq,
            "position": 0,
            "eta_s": 0.0,
            "poll_interval_ms": cfg.admission.poll_interval_ms,
        }

    # --- baseline / aaac modes ---
    # Default to MEDIUM until the estimator sends a LinkEstimate.
    access_class = AccessClass.MEDIUM

    await store.create_ticket(
        tid=tid,
        seq=seq,
        true_class=req.true_class,
        access_class=access_class,
        attempt=1,
    )

    position = await store.position(tid)

    await logger.log(
        "JOIN",
        ticket_id=tid,
        access_class=int(access_class),
        true_class=int(req.true_class),
        attempt=1,
        position=position,
    )

    # ETA: position / alpha (alpha is tokens/s; guard against near-zero)
    alpha = max(controller.alpha, 1.0)
    eta_s = position / alpha

    return {
        "ticket_id": tid,
        "join_seq": seq,
        "position": position,
        "eta_s": eta_s,
        "poll_interval_ms": cfg.admission.poll_interval_ms,
    }

# ---------------------------------------------------------------------------
# POST /queue/estimate
# ---------------------------------------------------------------------------

@app.post("/queue/estimate")
async def queue_estimate(est: LinkEstimate):
    ticket = await store.get_ticket(est.ticket_id)
    if not ticket:
        return {"accepted": False, "access_class": None}

    if ticket.state != "WAITING":
        return {"accepted": False, "access_class": None}

    # D2 GUARD — only one check here; read before changing.
    #
    # What guard 1 (attempt > 1) closes:
    #   A client that timed out is re-queued with a downgraded class (e.g. HIGH→MEDIUM).
    #   That re-queued ticket is WAITING again, so the WAITING-state check above does
    #   NOT block a fresh estimate. Without this guard, the client could re-submit a
    #   HIGH estimate on attempt 2 and silently undo the downgrade it earned. That is
    #   the I2 violation this guard exists for.
    #
    # Why there is NO guard 2 (rejecting int(est.class) < int(ticket.class)):
    #   MEDIUM at join is the §0.3 default — an *absence of classification*, not a
    #   prior measurement. int(HIGH=0) < int(MEDIUM=1) would make every HIGH estimate
    #   on attempt 1 look like an upgrade and reject it. No client could ever be
    #   classified HIGH. I2 is "access class never upgrades after a downgrade"; the
    #   first estimate is not a downgrade reversal, it is the establishing measurement.
    #   Monotonicity runs from the first established class onward, not from the join
    #   placeholder. Guard 2 was added and then removed after it broke Δ with no error.
    #   Do not re-add it.
    if ticket.attempt > 1:
        return {"accepted": False, "access_class": int(ticket.access_class)}


    await logger.log(
        "ESTIMATE",
        ticket_id=est.ticket_id,
        access_class=int(est.access_class),
        true_class=int(ticket.true_class),
        attempt=ticket.attempt,
        throughput_kbps=est.throughput_kbps,
        confidence=est.confidence,
    )

    await store.update_class(est.ticket_id, est.access_class)

    return {"accepted": True, "access_class": int(est.access_class)}

# ---------------------------------------------------------------------------
# GET /queue/status/{ticket_id}
# ---------------------------------------------------------------------------
# This endpoint will be polled by up to 20,000 clients concurrently.
# Keep it cheap: one store lookup, one conditional position read, no writes.

@app.get("/queue/status/{ticket_id}", response_model=TicketStatus)
async def queue_status(ticket_id: str):
    ticket = await store.get_ticket(ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    position = await store.position(ticket_id) if ticket.state == "WAITING" else 0

    admit_token = None
    window_s = None
    if ticket.state == "ADMITTED" and ticket.expires_at:
        now = time.time()
        ttl = ticket.expires_at - now
        if ttl > 0:
            admit_token = issue_token(ticket_id, ticket.access_class, ticket.attempt, ttl)
            window_s = window_for(ticket.access_class, cfg.admission, cfg.mode)

    return TicketStatus(
        ticket_id=ticket_id,
        state=ticket.state,
        position=position,
        attempt=ticket.attempt,
        access_class=ticket.access_class,
        window_s=window_s,
        admit_token=admit_token,
        expires_at=ticket.expires_at,
    )

# ---------------------------------------------------------------------------
# POST /queue/complete
# ---------------------------------------------------------------------------

@app.post("/queue/complete")
async def queue_complete(req: CompleteRequest):
    ticket = await store.get_ticket(req.ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    state = "COMPLETED" if req.ok else "ABANDONED"
    await store.complete(req.ticket_id, state)

    if req.ok:
        controller.record_completion()

    await logger.log(
        "COMPLETE" if req.ok else "ABANDON",
        ticket_id=req.ticket_id,
        access_class=int(ticket.access_class),
        true_class=int(ticket.true_class),
        attempt=ticket.attempt,
        bytes=req.bytes,
        duration_ms=req.duration_ms,
        variant=req.variant,
    )

    return {"state": state}

# ---------------------------------------------------------------------------
# GET /admin/snapshot
# ---------------------------------------------------------------------------

@app.get("/admin/snapshot")
async def admin_snapshot():
    counters = await store.get_counters()
    return {
        "run_id": cfg.run_id,
        "mode": cfg.mode,
        "t": time.time(),
        "alpha": controller.alpha,
        "mu_hat": controller.mu_hat,
        "in_flight": await store.inflight_count(),
        "waiting": counters["waiting"],
        "completed": counters["completed"],
        "timed_out": counters["timed_out"],
    }

# ---------------------------------------------------------------------------
# GET /admin/stream  (SSE, 1 Hz)
# ---------------------------------------------------------------------------

@app.get("/admin/stream")
async def admin_stream(request: Request):
    async def event_generator():
        while True:
            if await request.is_disconnected():
                break
            try:
                data = await admin_snapshot()
                yield f"data: {json.dumps(data)}\n\n"
            except Exception:
                log.exception("SSE snapshot error")
            await asyncio.sleep(1.0)

    return StreamingResponse(event_generator(), media_type="text/event-stream")
