from __future__ import annotations
from aaac.common.classes import AccessClass, downgrade
from aaac.common.config import RunConfig
from aaac.common.events import EventLogger
from aaac.admission.store import QueueStore


async def handle_timeout(tid: str, store: QueueStore, logger: EventLogger, cfg: RunConfig) -> None:
    """C4: Non-Regressive Re-Queue Policy.
    
    Called by the AdmissionController when a ticket's window expires while inflight.
    
    Starvation Note: 
    In AAAC mode, preserving join_seq means a repeatedly-failing client is re-served
    ahead of later arrivals. This is a deliberate choice over reset-on-failure.
    It is bounded because each attempt is strictly cheaper (lighter payload, longer 
    window via the downgrade mechanism), so the expected number of re-serves per 
    ticket is small and decreasing.
    """
    ticket = await store.get_ticket(tid)
    if not ticket:
        return
        
    # Always emit TIMEOUT first
    await logger.log(
        "TIMEOUT",
        ticket_id=tid,
        access_class=int(ticket.access_class),
        true_class=int(ticket.true_class),
        attempt=ticket.attempt
    )
    
    new_attempt = ticket.attempt + 1
    new_class = ticket.access_class
    score = ticket.join_seq
    
    if cfg.mode == "baseline":
        # Baseline mode represents the divergent loop (reset-on-failure).
        # Put the user at the tail of the queue, don't change their class.
        score = await store.next_seq()
    else:
        # AAAC mode: downgrade class, keep score the same (non-regressive)
        if ticket.attempt >= cfg.admission.max_attempts:
            new_class = AccessClass.LOW
            await logger.log(
                "FORCED_FLOOR",
                ticket_id=tid,
                access_class=int(new_class),
                true_class=int(ticket.true_class),
                attempt=new_attempt
            )
        else:
            new_class = downgrade(ticket.access_class)
            
    if new_class != ticket.access_class:
        await logger.log(
            "DOWNGRADE",
            ticket_id=tid,
            access_class=int(new_class),
            true_class=int(ticket.true_class),
            attempt=ticket.attempt
        )
        
    await store.reinsert(tid, score=score, new_class=new_class, attempt=new_attempt)
    
    await logger.log(
        "REQUEUE",
        ticket_id=tid,
        access_class=int(new_class),
        true_class=int(ticket.true_class),
        attempt=new_attempt,
        # Note: position is read outside the reinsert lock. It could be slightly stale
        # if another ticket was admitted in between, but this is acceptable for a log.
        position=await store.position(tid)
    )
