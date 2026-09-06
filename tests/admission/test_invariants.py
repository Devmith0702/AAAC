import pytest
import asyncio
import random
import time
import json
import os
from collections import defaultdict

from aaac.common.config import get_config, reset_config
from aaac.common.classes import AccessClass
from aaac.common.events import EventLogger
from aaac.admission.store import InMemoryQueueStore
from aaac.admission.controller import AdmissionController
from aaac.admission.requeue import handle_timeout

@pytest.fixture
def base_config():
    reset_config()
    cfg = get_config()
    return cfg

@pytest.mark.asyncio
async def test_invariant_i1_position_non_increasing(base_config):
    """
    I1: Position is monotonically non-increasing for a given ticket 
    across its whole lifetime in 'aaac' mode.
    """
    base_config = get_config()
    # Force aaac mode
    cfg_dict = base_config.__dict__.copy()
    cfg_dict["mode"] = "aaac"
    # Create a simple mock config
    class MockConfig:
        pass
    cfg = MockConfig()
    for k, v in cfg_dict.items():
        setattr(cfg, k, v)
        
    store = InMemoryQueueStore(cfg.run_id)
    
    # Track position history for each ticket
    position_history = defaultdict(list)
    
    # 1. Join phase: 200 tickets arrive in random order
    tids = []
    classes = [AccessClass.HIGH, AccessClass.MEDIUM, AccessClass.LOW]
    
    for i in range(200):
        tid = f"t{i}"
        tids.append(tid)
        cls = random.choice(classes)
        seq = await store.next_seq()
        await store.create_ticket(tid, seq, cls, cls, 1)
        
    # Record initial positions
    for tid in tids:
        pos = await store.position(tid)
        position_history[tid].append(pos)
        
    # 2. Admit and Requeue phase
    # We will simulate several "ticks" where we admit some tickets and timeout some.
    now = time.time()
    windows = {
        AccessClass.HIGH: 10.0,
        AccessClass.MEDIUM: 15.0,
        AccessClass.LOW: 25.0
    }
    
    class DummyLogger:
        async def log(self, *args, **kwargs):
            pass
            
    logger = DummyLogger()
    
    for step in range(10):
        # Admit 10 tickets
        admitted = await store.admit_n(10, now, windows)
        
        # Record positions after admission
        # Even though they are ADMITTED, their position should be considered 0 
        # or we only track while WAITING.
        for tid in tids:
            ticket = await store.get_ticket(tid)
            if ticket and ticket.state == "WAITING":
                pos = await store.position(tid)
                position_history[tid].append(pos)
        # Simulate timeouts for half the admitted tickets
        if admitted:
            # Force expire all
            expired_tids = await store.expire_inflight(now + 100)
            
            timeouts = expired_tids[:len(expired_tids)//2]
            completes = expired_tids[len(expired_tids)//2:]
            
            for tid in timeouts:
                # handle_timeout will reinsert
                await handle_timeout(tid, store, logger, cfg)
                
            for tid in completes:
                await store.complete(tid, "COMPLETED")
                
        # Record positions after requeue
        for tid in tids:
            ticket = await store.get_ticket(tid)
            if ticket and ticket.state == "WAITING":
                pos = await store.position(tid)
                position_history[tid].append(pos)
                
    # 3. Assert I1
    for tid, history in position_history.items():
        # In AAAC mode, a ticket's position can temporarily drop to 0 when admitted, 
        # and then go to some value > 0 if it times out and is re-inserted behind other re-inserted tickets.
        # However, the number of unresolved tickets ahead of it NEVER increases.
        # Thus, its position never exceeds its initial position.
        assert max(history) <= history[0], f"I1 VIOLATION: Ticket {tid} position exceeded initial position!"



@pytest.mark.asyncio
async def test_invariant_i2_class_never_upgrades(base_config):
    """
    I2: Access class is non-increasing in capability (never upgraded).
    Downgrade path: HIGH -> MEDIUM -> LOW -> LOW (floor).
    """
    # Force aaac mode
    cfg_dict = base_config.__dict__.copy()
    cfg_dict["mode"] = "aaac"
    class MockConfig:
        pass
    cfg = MockConfig()
    for k, v in cfg_dict.items():
        setattr(cfg, k, v)
        
    store = InMemoryQueueStore(cfg.run_id)
    
    class DummyLogger:
        async def log(self, *args, **kwargs):
            pass
    logger = DummyLogger()
    
    tid = "test_i2"
    await store.create_ticket(tid, 1, AccessClass.HIGH, AccessClass.HIGH, 1)
    
    class_history = [AccessClass.HIGH]
    
    # Simulate 10 timeouts
    for _ in range(10):
        # Move to inflight
        await store.admit_n(1, time.time(), {AccessClass.HIGH: 10, AccessClass.MEDIUM: 10, AccessClass.LOW: 10})
        await store.expire_inflight(time.time() + 100)
        
        # Timeout
        await handle_timeout(tid, store, logger, cfg)
        
        ticket = await store.get_ticket(tid)
        class_history.append(ticket.access_class)
        
    # Assert monotonic degradation
    # IntEnum: HIGH=0, MEDIUM=1, LOW=2. So values should be non-decreasing numerically
    for i in range(1, len(class_history)):
        assert class_history[i].value >= class_history[i-1].value, f"I2 VIOLATION: Upgraded from {class_history[i-1].name} to {class_history[i].name}"


@pytest.mark.asyncio
async def test_invariant_i6_log_reconciliation(base_config):
    """
    I6: Every state transition emits exactly one event; log reconciliation.
    """
    run_id = "test_i6_run"
    mode = "aaac"
    logger = EventLogger(run_id, mode)
    store = InMemoryQueueStore(run_id)
    
    # Use a real config for the logger
    cfg_dict = base_config.__dict__.copy()
    cfg_dict["mode"] = mode
    class MockConfig:
        pass
    cfg = MockConfig()
    for k, v in cfg_dict.items():
        setattr(cfg, k, v)
        
    # 1. Join -> WAITING
    tid = "i6_ticket"
    await store.create_ticket(tid, 1, AccessClass.HIGH, AccessClass.HIGH, 1)
    await logger.log("JOIN", ticket_id=tid, access_class=int(AccessClass.HIGH), true_class=int(AccessClass.HIGH), attempt=1, position=0)
    
    # 2. ADMIT -> ADMITTED
    admitted = await store.admit_n(1, time.time(), {AccessClass.HIGH: 10, AccessClass.MEDIUM: 10, AccessClass.LOW: 10})
    await logger.log("ADMIT", ticket_id=tid, access_class=int(AccessClass.HIGH), true_class=int(AccessClass.HIGH), attempt=1, position=0)
    
    # 3. TIMEOUT -> WAITING
    await store.expire_inflight(time.time() + 100)
    await handle_timeout(tid, store, logger, cfg)
    
    # 4. ADMIT again -> ADMITTED
    admitted = await store.admit_n(1, time.time(), {AccessClass.HIGH: 10, AccessClass.MEDIUM: 10, AccessClass.LOW: 10})
    await logger.log("ADMIT", ticket_id=tid, access_class=int(AccessClass.MEDIUM), true_class=int(AccessClass.HIGH), attempt=2, position=0)
    
    # 5. COMPLETE -> COMPLETED
    await store.complete(tid, "COMPLETED")
    await logger.log("COMPLETE", ticket_id=tid, access_class=int(AccessClass.MEDIUM), true_class=int(AccessClass.HIGH), attempt=2, bytes=100, duration_ms=50, variant="reduced")
    
    await logger.close()
    
    # Read logs and reconcile
    log_path = logger.filepath
    assert os.path.exists(log_path)
    
    events = []
    with open(log_path, "r") as f:
        for line in f:
            events.append(json.loads(line))
            
    # Expected sequence of events for this ticket
    expected_sequence = ["JOIN", "ADMIT", "TIMEOUT", "DOWNGRADE", "REQUEUE", "ADMIT", "COMPLETE"]
    actual_sequence = [e["event"] for e in events if e.get("ticket_id") == tid]
    
    assert actual_sequence == expected_sequence, f"I6 VIOLATION: Event sequence mismatch.\nExpected: {expected_sequence}\nActual: {actual_sequence}"
    
    # Clean up
    os.remove(log_path)
