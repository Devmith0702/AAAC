import asyncio
import time
import uuid
import json
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from aaac.common.config import get_config
from aaac.common.classes import AccessClass
from aaac.common.schemas import LinkEstimate, TicketStatus
from aaac.common.events import EventLogger
from aaac.common.tokens import issue_token
from aaac.admission.store import RedisQueueStore
from aaac.admission.controller import AdmissionController
from aaac.admission.window import window_for

import redis.asyncio as redis

# Pydantic schemas for requests
class JoinRequest(BaseModel):
    client_id: str
    true_class: int

class CompleteRequest(BaseModel):
    ticket_id: str
    ok: bool
    bytes: int
    duration_ms: float
    variant: str

from contextlib import asynccontextmanager

# These will be initialized in the lifespan context
redis_client: redis.Redis = None
store: RedisQueueStore = None
logger: EventLogger = None
controller: AdmissionController = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_client, store, logger, controller
    redis_client = redis.Redis(host="redis", port=6379, db=0)
    store = RedisQueueStore(redis_client, cfg.run_id)
    logger = EventLogger(cfg.run_id, cfg.mode)
    controller = AdmissionController(store, logger, cfg, origin_url="http://origin:8002")
    await controller.start()
    
    yield
    
    await controller.stop()
    await logger.close()
    await redis_client.aclose()

# Application state
app = FastAPI(title="AAAC Admission Core", lifespan=lifespan)
cfg = get_config()

import random as _random
_rng = _random.Random(cfg.seed)

@app.post("/queue/join")
async def queue_join(req: JoinRequest):
    tid = f"{_rng.getrandbits(128):032x}"
    seq = await store.next_seq()
    
    true_class = AccessClass(req.true_class)
    # Default to MEDIUM unless estimated otherwise
    access_class = AccessClass.MEDIUM
    
    await store.create_ticket(
        tid=tid, 
        seq=seq, 
        true_class=true_class, 
        access_class=access_class, 
        attempt=1
    )
    
    position = await store.position(tid)
    
    await logger.log(
        "JOIN",
        ticket_id=tid,
        access_class=int(access_class),
        true_class=int(true_class),
        attempt=1,
        position=position
    )
    
    # Calculate ETA based on current alpha. If alpha is 0 (or very low), prevent division by zero
    alpha = max(controller.alpha, 1.0)
    eta_s = position / alpha
    
    return {
        "ticket_id": tid,
        "join_seq": seq,
        "position": position,
        "eta_s": eta_s,
        "poll_interval_ms": cfg.admission.poll_interval_ms
    }

@app.post("/queue/estimate")
async def queue_estimate(est: LinkEstimate):
    ticket = await store.get_ticket(est.ticket_id)
    if not ticket:
        return {"accepted": False, "access_class": None}
        
    if ticket.state != "WAITING":
        # Only accept estimates for waiting tickets
        return {"accepted": False, "access_class": None}
        
    await logger.log(
        "ESTIMATE",
        ticket_id=est.ticket_id,
        access_class=int(est.access_class),
        true_class=int(ticket.true_class),
        attempt=ticket.attempt,
        throughput_kbps=est.throughput_kbps,
        confidence=est.confidence
    )
    
    # Update the ticket's access class without losing position or triggering timeout logic
    await store.update_class(est.ticket_id, est.access_class)
    
    return {"accepted": True, "access_class": est.access_class.value}

@app.get("/queue/status/{ticket_id}", response_model=TicketStatus)
async def queue_status(ticket_id: str):
    ticket = await store.get_ticket(ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
        
    position = await store.position(ticket_id) if ticket.state == "WAITING" else 0
    
    # If admitted, issue a fresh token for M2 (expires at ticket.expires_at)
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
        expires_at=ticket.expires_at
    )

@app.post("/queue/complete")
async def queue_complete(req: CompleteRequest):
    ticket = await store.get_ticket(req.ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
        
    state = "COMPLETED" if req.ok else "ABANDONED"
    await store.complete(req.ticket_id, state)
    
    # Update controller capacity estimates
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
        variant=req.variant
    )
    
    return {"state": state}

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
        "timed_out": counters["timed_out"]
    }

@app.get("/admin/stream")
async def admin_stream(request: Request):
    async def event_generator():
        while True:
            # Check if client disconnected
            if await request.is_disconnected():
                break
                
            try:
                # Reuse the snapshot logic
                data = await admin_snapshot()
                yield f"data: {json.dumps(data)}\n\n"
            except Exception as e:
                # Log but don't kill the stream
                import logging
                logging.getLogger(__name__).error(f"SSE error: {e}")
                
            await asyncio.sleep(1.0)
            
    return StreamingResponse(event_generator(), media_type="text/event-stream")
