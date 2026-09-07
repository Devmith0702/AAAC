from __future__ import annotations
import asyncio
import time
from typing import Protocol, Any
from aaac.common.classes import AccessClass

class TicketData:
    __slots__ = ("state", "attempt", "access_class", "join_seq", "expires_at", "true_class")
    
    def __init__(
        self,
        state: str,
        attempt: int,
        access_class: AccessClass,
        join_seq: int,
        expires_at: float | None,
        true_class: AccessClass
    ):
        self.state = state
        self.attempt = attempt
        self.access_class = access_class
        self.join_seq = join_seq
        self.expires_at = expires_at
        self.true_class = true_class

class QueueStore(Protocol):
    async def next_seq(self) -> int: ...
    async def create_ticket(self, tid: str, seq: int, true_class: AccessClass, access_class: AccessClass, attempt: int, expires_at: float | None = None) -> None: ...
    async def get_ticket(self, tid: str) -> TicketData | None: ...
    async def admit_n(self, n: int, now: float, windows_s: dict[AccessClass, float]) -> list[tuple[str, float]]: ...
    async def reinsert(self, tid: str, score: int, new_class: AccessClass | None = None, attempt: int | None = None) -> None: ...
    async def update_class(self, tid: str, new_class: AccessClass) -> None: ...
    async def position(self, tid: str) -> int: ...
    async def waiting_count(self) -> int: ...
    async def inflight_count(self) -> int: ...
    async def expire_inflight(self, now: float) -> list[str]: ...
    async def complete(self, tid: str, state: str) -> None: ...
    async def get_counters(self) -> dict[str, dict[str, int]]: ...

ADMIT_N_LUA = """
local run = KEYS[1]
local waiting_key = 'aaac:' .. run .. ':waiting'
local inflight_key = 'aaac:' .. run .. ':inflight'
local n = tonumber(ARGV[1])
local now = tonumber(ARGV[2])
local w_high = tonumber(ARGV[3])
local w_medium = tonumber(ARGV[4])
local w_low = tonumber(ARGV[5])

local windows = {[0] = w_high, [1] = w_medium, [2] = w_low}

local tickets = redis.call('ZPOPMIN', waiting_key, n)
local admitted = {}

for i = 1, #tickets, 2 do
    local tid = tickets[i]
    local ticket_key = 'aaac:' .. run .. ':ticket:' .. tid
    
    local class_str = redis.call('HGET', ticket_key, 'class')
    if class_str then
        local class_val = tonumber(class_str)
        local win = windows[class_val] or w_medium
        local expires_at = now + win
        
        redis.call('HMSET', ticket_key, 'state', 'ADMITTED', 'expires_at', tostring(expires_at))
        redis.call('ZADD', inflight_key, expires_at, tid)
        redis.call('HINCRBY', 'aaac:' .. run .. ':counters', 'waiting:' .. class_val, -1)
        
        table.insert(admitted, tid)
        table.insert(admitted, tostring(expires_at))
    end
end
return admitted
"""

class RedisQueueStore:
    def __init__(self, redis_client, run_id: str):
        self.r = redis_client
        self.run_id = run_id
        # register_script is synchronous in redis-py — safe to call in __init__
        self._admit_script = self.r.register_script(ADMIT_N_LUA)

    async def next_seq(self) -> int:
        return await self.r.incr(f"aaac:{self.run_id}:seq")
        
    async def create_ticket(
        self, tid: str, seq: int, true_class: AccessClass, access_class: AccessClass, attempt: int, expires_at: float | None = None
    ) -> None:
        key = f"aaac:{self.run_id}:ticket:{tid}"
        mapping = {
            "state": "WAITING",
            "attempt": str(attempt),
            "class": str(int(access_class)),
            "true_class": str(int(true_class)),
            "join_seq": str(seq),
            "expires_at": str(expires_at) if expires_at else ""
        }
        pipe = self.r.pipeline()
        pipe.hset(key, mapping=mapping)
        pipe.zadd(f"aaac:{self.run_id}:waiting", {tid: seq})
        pipe.hincrby(f"aaac:{self.run_id}:counters", f"waiting:{int(access_class)}", 1)
        await pipe.execute()
        
    async def get_ticket(self, tid: str) -> TicketData | None:
        data = await self.r.hgetall(f"aaac:{self.run_id}:ticket:{tid}")
        if not data:
            return None
        return TicketData(
            state=data[b"state"].decode(),
            attempt=int(data[b"attempt"]),
            access_class=AccessClass(int(data[b"class"])),
            join_seq=int(data[b"join_seq"]),
            expires_at=float(data[b"expires_at"]) if data.get(b"expires_at") else None,
            true_class=AccessClass(int(data[b"true_class"]))
        )
        
    async def admit_n(self, n: int, now: float, windows_s: dict[AccessClass, float]) -> list[tuple[str, float]]:
        if n <= 0:
            return []
        keys = [self.run_id]
        args = [n, now, windows_s[AccessClass.HIGH], windows_s[AccessClass.MEDIUM], windows_s[AccessClass.LOW]]
        result = await self._admit_script(keys=keys, args=args)
        
        admitted = []
        for i in range(0, len(result), 2):
            admitted.append((result[i].decode(), float(result[i+1].decode())))
        return admitted
        
    async def reinsert(self, tid: str, score: int, new_class: AccessClass | None = None, attempt: int | None = None) -> None:
        key = f"aaac:{self.run_id}:ticket:{tid}"
        pipe = self.r.pipeline()
        
        # Get old class for timeout counter
        old_class_bytes = await self.r.hget(key, "class")
        old_class = int(old_class_bytes) if old_class_bytes else 0
        
        if new_class is not None:
            pipe.hset(key, "class", str(int(new_class)))
        if attempt is not None:
            pipe.hset(key, "attempt", str(attempt))
            
        pipe.hset(key, "state", "WAITING")
        pipe.zrem(f"aaac:{self.run_id}:inflight", tid)
        pipe.zadd(f"aaac:{self.run_id}:waiting", {tid: score})
        
        # Reinsert implies timeout in this system
        pipe.hincrby(f"aaac:{self.run_id}:counters", f"timed_out:{old_class}", 1)
        
        # Increment waiting for the new class
        target_class = int(new_class) if new_class is not None else old_class
        pipe.hincrby(f"aaac:{self.run_id}:counters", f"waiting:{target_class}", 1)
        
        await pipe.execute()
        
    async def update_class(self, tid: str, new_class: AccessClass) -> None:
        key = f"aaac:{self.run_id}:ticket:{tid}"
        pipe = self.r.pipeline()
        old_class_bytes = await self.r.hget(key, "class")
        
        if old_class_bytes:
            old_class = int(old_class_bytes)
            new_cls = int(new_class)
            if old_class != new_cls:
                pipe.hset(key, "class", str(new_cls))
                pipe.hincrby(f"aaac:{self.run_id}:counters", f"waiting:{old_class}", -1)
                pipe.hincrby(f"aaac:{self.run_id}:counters", f"waiting:{new_cls}", 1)
                await pipe.execute()
        
    async def position(self, tid: str) -> int:
        rank = await self.r.zrank(f"aaac:{self.run_id}:waiting", tid)
        return rank if rank is not None else 0
        
    async def waiting_count(self) -> int:
        return await self.r.zcard(f"aaac:{self.run_id}:waiting")
        
    async def inflight_count(self) -> int:
        return await self.r.zcard(f"aaac:{self.run_id}:inflight")
        
    async def expire_inflight(self, now: float) -> list[str]:
        # Atomic pop from inflight ZSET <= now
        script = """
        local inflight_key = KEYS[1]
        local now = tonumber(ARGV[1])
        local expired = redis.call('ZRANGEBYSCORE', inflight_key, '-inf', now)
        if #expired > 0 then
            redis.call('ZREMRANGEBYSCORE', inflight_key, '-inf', now)
        end
        return expired
        """
        keys = [f"aaac:{self.run_id}:inflight"]
        args = [now]
        result = await self.r.eval(script, len(keys), *keys, *args)
        return [t.decode() for t in result]
        
    async def complete(self, tid: str, state: str) -> None:
        key = f"aaac:{self.run_id}:ticket:{tid}"
        cls_bytes = await self.r.hget(key, "class")
        cls_val = int(cls_bytes) if cls_bytes else 0
        
        pipe = self.r.pipeline()
        pipe.hset(key, "state", state)
        pipe.zrem(f"aaac:{self.run_id}:inflight", tid)
        if state == "COMPLETED":
            pipe.hincrby(f"aaac:{self.run_id}:counters", f"completed:{cls_val}", 1)
        await pipe.execute()
        
    async def get_counters(self) -> dict[str, dict[str, int]]:
        raw = await self.r.hgetall(f"aaac:{self.run_id}:counters")
        res = {"waiting": {"0":0, "1":0, "2":0}, "completed": {"0":0, "1":0, "2":0}, "timed_out": {"0":0, "1":0, "2":0}}
        for k_b, v_b in raw.items():
            k = k_b.decode()
            v = int(v_b)
            if ":" in k:
                cat, cls = k.split(":")
                if cat in res:
                    res[cat][cls] = v
        return res

class InMemoryQueueStore:
    def __init__(self, run_id: str):
        self.run_id = run_id
        self._seq = 0
        self._tickets: dict[str, dict] = {}
        self._waiting: list[tuple[int, str]] = []  # simple sorted list
        self._inflight: list[tuple[float, str]] = []
        self._counters = {
            "waiting": {"0":0, "1":0, "2":0},
            "completed": {"0":0, "1":0, "2":0},
            "timed_out": {"0":0, "1":0, "2":0}
        }
        self._lock = asyncio.Lock()
        
    async def next_seq(self) -> int:
        async with self._lock:
            self._seq += 1
            return self._seq
            
    async def create_ticket(self, tid: str, seq: int, true_class: AccessClass, access_class: AccessClass, attempt: int, expires_at: float | None = None) -> None:
        async with self._lock:
            self._tickets[tid] = {
                "state": "WAITING",
                "attempt": attempt,
                "class": access_class,
                "true_class": true_class,
                "join_seq": seq,
                "expires_at": expires_at
            }
            self._waiting.append((seq, tid))
            self._waiting.sort(key=lambda x: x[0])
            self._counters["waiting"][str(int(access_class))] += 1
            
    async def get_ticket(self, tid: str) -> TicketData | None:
        async with self._lock:
            t = self._tickets.get(tid)
            if not t: return None
            return TicketData(
                state=t["state"],
                attempt=t["attempt"],
                access_class=t["class"],
                join_seq=t["join_seq"],
                expires_at=t["expires_at"],
                true_class=t["true_class"]
            )
            
    async def admit_n(self, n: int, now: float, windows_s: dict[AccessClass, float]) -> list[tuple[str, float]]:
        async with self._lock:
            admitted = []
            to_pop = min(n, len(self._waiting))
            for _ in range(to_pop):
                _, tid = self._waiting.pop(0)
                t = self._tickets[tid]
                t["state"] = "ADMITTED"
                exp = now + windows_s[t["class"]]
                t["expires_at"] = exp
                self._inflight.append((exp, tid))
                admitted.append((tid, exp))
                self._counters["waiting"][str(int(t["class"]))] -= 1
            self._inflight.sort(key=lambda x: x[0])
            return admitted
            
    async def reinsert(self, tid: str, score: int, new_class: AccessClass | None = None, attempt: int | None = None) -> None:
        async with self._lock:
            t = self._tickets[tid]
            old_class = str(int(t["class"]))
            if new_class is not None:
                t["class"] = new_class
            if attempt is not None:
                t["attempt"] = attempt
            t["state"] = "WAITING"
            self._inflight = [(e, t_id) for e, t_id in self._inflight if t_id != tid]
            self._waiting.append((score, tid))
            self._waiting.sort(key=lambda x: x[0])
            self._counters["timed_out"][old_class] += 1
            self._counters["waiting"][str(int(t["class"]))] += 1
            
    async def update_class(self, tid: str, new_class: AccessClass) -> None:
        async with self._lock:
            t = self._tickets.get(tid)
            if t:
                old_cls = str(int(t["class"]))
                new_cls = str(int(new_class))
                if old_cls != new_cls:
                    t["class"] = new_class
                    self._counters["waiting"][old_cls] -= 1
                    self._counters["waiting"][new_cls] += 1
            
    async def position(self, tid: str) -> int:
        async with self._lock:
            for i, (_, t) in enumerate(self._waiting):
                if t == tid:
                    return i
            return 0
            
    async def waiting_count(self) -> int:
        async with self._lock:
            return len(self._waiting)
            
    async def inflight_count(self) -> int:
        async with self._lock:
            return len(self._inflight)
            
    async def expire_inflight(self, now: float) -> list[str]:
        async with self._lock:
            expired = []
            kept = []
            for exp, tid in self._inflight:
                if exp <= now:
                    expired.append(tid)
                else:
                    kept.append((exp, tid))
            self._inflight = kept
            return expired
            
    async def complete(self, tid: str, state: str) -> None:
        async with self._lock:
            if tid in self._tickets:
                self._tickets[tid]["state"] = state
                if state == "COMPLETED":
                    cls_val = str(int(self._tickets[tid]["class"]))
                    self._counters["completed"][cls_val] += 1
            self._inflight = [(e, t) for e, t in self._inflight if t != tid]
            
    async def get_counters(self) -> dict[str, dict[str, int]]:
        async with self._lock:
            import copy
            return copy.deepcopy(self._counters)
