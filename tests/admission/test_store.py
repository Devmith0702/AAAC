import pytest
import time
from aaac.common.classes import AccessClass
from aaac.admission.store import InMemoryQueueStore

@pytest.mark.asyncio
async def test_in_memory_store_admit_n():
    store = InMemoryQueueStore("test")
    
    await store.create_ticket("t1", await store.next_seq(), AccessClass.HIGH, AccessClass.HIGH, 1)
    await store.create_ticket("t2", await store.next_seq(), AccessClass.MEDIUM, AccessClass.MEDIUM, 1)
    await store.create_ticket("t3", await store.next_seq(), AccessClass.LOW, AccessClass.LOW, 1)
    
    assert await store.waiting_count() == 3
    assert await store.position("t1") == 0
    assert await store.position("t3") == 2
    
    windows = {AccessClass.HIGH: 10, AccessClass.MEDIUM: 20, AccessClass.LOW: 30}
    now = time.time()
    
    admitted = await store.admit_n(2, now, windows)
    assert len(admitted) == 2
    
    assert await store.waiting_count() == 1
    assert await store.inflight_count() == 2
    
    # Check expires_at
    t1_tid, t1_exp = admitted[0]
    t2_tid, t2_exp = admitted[1]
    
    assert t1_tid == "t1"
    assert t1_exp == now + 10
    
    assert t2_tid == "t2"
    assert t2_exp == now + 20
    
    t1_data = await store.get_ticket("t1")
    assert t1_data.state == "ADMITTED"
    assert t1_data.expires_at == now + 10
    
@pytest.mark.asyncio
async def test_in_memory_store_expire_inflight():
    store = InMemoryQueueStore("test")
    await store.create_ticket("t1", 1, AccessClass.HIGH, AccessClass.HIGH, 1)
    
    windows = {AccessClass.HIGH: 10, AccessClass.MEDIUM: 20, AccessClass.LOW: 30}
    now = time.time()
    await store.admit_n(1, now, windows)
    
    expired = await store.expire_inflight(now + 5)
    assert len(expired) == 0
    
    expired = await store.expire_inflight(now + 15)
    assert len(expired) == 1
    assert expired[0] == "t1"
    assert await store.inflight_count() == 0
