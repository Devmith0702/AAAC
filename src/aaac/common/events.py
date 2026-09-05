from __future__ import annotations
import os
import json
import time
import asyncio
from typing import Any

# Allowed event vocabulary
ALLOWED_EVENTS = {
    "JOIN", "ESTIMATE", "ADMIT", "COMPLETE", "TIMEOUT",
    "REQUEUE", "DOWNGRADE", "FORCED_FLOOR", "ABANDON", "ORIGIN_SAMPLE", "CONTROL"
}

class EventLogger:
    def __init__(self, run_id: str, mode: str):
        self.run_id = run_id
        self.mode = mode

        # Resolve the results directory from env var, defaulting to "results/"
        results_dir = os.environ.get("AAAC_RESULTS_DIR", "results")
        os.makedirs(results_dir, exist_ok=True)
        self.filepath = os.path.join(results_dir, f"events-{run_id}.jsonl")
        self._buffer: list[str] = []
        self._lock = asyncio.Lock()
        self._flush_task: asyncio.Task | None = None
        self._closing = False
        
    async def log(self, event: str, ticket_id: str | None = None, **fields: Any) -> None:
        if event not in ALLOWED_EVENTS:
            raise ValueError(f"Unknown event name: {event}")
            
        record = {
            "ts": time.time(),
            "run_id": self.run_id,
            "mode": self.mode,
        }
        if ticket_id is not None:
            record["ticket_id"] = ticket_id
            
        record["event"] = event
        record.update(fields)
        
        line = json.dumps(record, separators=(',', ':')) + "\n"
        
        async with self._lock:
            self._buffer.append(line)
            if len(self._buffer) >= 100:
                await self._flush_unlocked()
            elif self._flush_task is None and not self._closing:
                self._flush_task = asyncio.create_task(self._flush_timer())
                
    async def _flush_timer(self) -> None:
        try:
            await asyncio.sleep(0.5) # 500ms
        except asyncio.CancelledError:
            pass
        finally:
            async with self._lock:
                await self._flush_unlocked()
                self._flush_task = None
                
    async def _flush_unlocked(self) -> None:
        if not self._buffer:
            return
            
        lines = "".join(self._buffer)
        self._buffer.clear()
        
        # Append to file atomically enough (OS file append)
        def write_sync():
            with open(self.filepath, "a", encoding="utf-8") as f:
                f.write(lines)
                f.flush()
                os.fsync(f.fileno())
                
        await asyncio.to_thread(write_sync)
        
    async def close(self) -> None:
        self._closing = True
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        async with self._lock:
            await self._flush_unlocked()
