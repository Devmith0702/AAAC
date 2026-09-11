"""Event sinks for ``ORIGIN_SAMPLE``.

CLAUDE.md §3.8 gives the event log a single writer, the admission service, but
origin samples come from this process. That is open question §5.2(1). The
resolution taken here is the pluggable-sink one: the origin emits through
:class:`EventSink`, and the default implementation writes a *separate*
``results/origin-{run_id}.jsonl`` that the analysis joins to M1's log on
timestamp. No contract change is required, and swapping to an M1 ingest endpoint
later replaces one class rather than the service.

The known cost, which must be measured and not assumed away: joining on
timestamp across containers carries clock-skew risk.

Writes are handed to a background drain task so no request handler ever blocks on
disk I/O (§3.10 rule 3).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class EventSink(Protocol):
    """Where the origin publishes its ``ORIGIN_SAMPLE`` events."""

    async def start(self) -> None: ...

    async def emit(self, event: Mapping[str, Any]) -> None: ...

    async def aclose(self) -> None: ...


class NullSink:
    """Discards everything. Used when a run is not being recorded."""

    async def start(self) -> None:
        return None

    async def emit(self, event: Mapping[str, Any]) -> None:
        return None

    async def aclose(self) -> None:
        return None


class MemorySink:
    """Collects events in a list. For tests only — never use in a real run."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def start(self) -> None:
        return None

    async def emit(self, event: Mapping[str, Any]) -> None:
        self.events.append(dict(event))

    async def aclose(self) -> None:
        return None


class JsonlSink:
    """Append-only JSONL writer with a background drain task."""

    def __init__(self, path: Path, queue_maxsize: int = 4096) -> None:
        self.path = Path(path)
        self._queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=queue_maxsize)
        self._task: asyncio.Task[None] | None = None
        self._fh: Any = None
        self.dropped = 0

    async def start(self) -> None:
        # `results/` is not tracked by git when empty, so a clean clone will not
        # have it. Create it rather than failing at the first sample.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = await asyncio.to_thread(self.path.open, "a", encoding="utf-8")
        self._task = asyncio.create_task(self._drain(), name="origin-jsonl-sink")

    async def emit(self, event: Mapping[str, Any]) -> None:
        try:
            self._queue.put_nowait(dict(event))
        except asyncio.QueueFull:
            # Losing a sample silently would be a hole in the instrument
            # (§3.8). Count it so the run can report the gap honestly.
            self.dropped += 1

    async def _drain(self) -> None:
        assert self._fh is not None
        while True:
            event = await self._queue.get()
            if event is None:
                return
            line = json.dumps(event, separators=(",", ":")) + "\n"
            await asyncio.to_thread(self._write, line)

    def _write(self, line: str) -> None:
        self._fh.write(line)
        self._fh.flush()

    async def aclose(self) -> None:
        if self._task is not None:
            await self._queue.put(None)
            await self._task
            self._task = None
        if self._fh is not None:
            await asyncio.to_thread(self._fh.close)
            self._fh = None

    async def __aenter__(self) -> JsonlSink:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()
