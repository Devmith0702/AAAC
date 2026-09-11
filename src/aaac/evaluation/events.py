"""Reading the event log (CLAUDE.md §3.8).

    Every metric I report is computed from this file and nothing else.

So this module is the only way numbers enter the analysis. It never reads live
process state, never scrapes ``/admin/snapshot``, and never imports anything from
M1's or M2's packages — the log is the interface, and that is what makes the
analysis reproducible from an archived file months later.

The vocabulary is closed. An unknown event type is surfaced as a
:class:`LogIntegrityError` rather than skipped, because a silently ignored event
is exactly the kind of hole in the instrument §3.8 warns about.

``ORIGIN_SAMPLE`` events arrive from a second file, ``results/origin-{run_id}.jsonl``
— see §5.2 open question 1 and the note in ``sinks.py``. They are joined on
timestamp, and :meth:`EventLog.clock_skew_warning` reports when the two files do
not overlap in time, since a skewed join would quietly misattribute origin state
to the wrong phase of the run.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: The closed vocabulary from §3.8. Adding to this is a CONTRACT CHANGE.
EVENT_TYPES: frozenset[str] = frozenset(
    {
        "JOIN",
        "ESTIMATE",
        "ADMIT",
        "COMPLETE",
        "TIMEOUT",
        "REQUEUE",
        "DOWNGRADE",
        "ABANDON",
        "ORIGIN_SAMPLE",
        "CONTROL",
    }
)

#: Events that must carry a ticket_id to be usable.
TICKET_EVENTS: frozenset[str] = EVENT_TYPES - {"ORIGIN_SAMPLE", "CONTROL"}


class LogIntegrityError(RuntimeError):
    """The log cannot be trusted to produce a number."""


@dataclass
class EventLog:
    """An in-memory event log, plus everything needed to describe its own gaps."""

    events: list[dict[str, Any]] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    malformed_lines: int = 0
    unknown_event_types: dict[str, int] = field(default_factory=dict)

    # -- loading ----------------------------------------------------------

    @classmethod
    def read(
        cls,
        *paths: Path | str,
        strict: bool = True,
    ) -> EventLog:
        """Load one or more JSONL files into a single time-ordered log."""
        log = cls()
        for path in paths:
            resolved = Path(path)
            if not resolved.exists():
                raise FileNotFoundError(f"event log not found: {resolved}")
            log.sources.append(str(resolved))
            with resolved.open("r", encoding="utf-8") as fh:
                for lineno, line in enumerate(fh, start=1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError as exc:
                        if strict:
                            raise LogIntegrityError(
                                f"{resolved}:{lineno}: not valid JSON ({exc.msg}). "
                                "A truncated log means missing decisions; do not "
                                "compute metrics from it."
                            ) from exc
                        log.malformed_lines += 1
                        continue
                    name = event.get("event")
                    if name not in EVENT_TYPES:
                        log.unknown_event_types[str(name)] = (
                            log.unknown_event_types.get(str(name), 0) + 1
                        )
                        if strict:
                            raise LogIntegrityError(
                                f"{resolved}:{lineno}: event type {name!r} is outside the "
                                f"closed vocabulary of §3.8. Either the contract changed "
                                f"without notice or the log is corrupt."
                            )
                        continue
                    log.events.append(event)
        log.events.sort(key=lambda e: (float(e.get("ts", 0.0)), e.get("event", "")))
        return log

    # -- views ------------------------------------------------------------

    def __len__(self) -> int:
        return len(self.events)

    def of_type(self, *names: str) -> Iterator[dict[str, Any]]:
        wanted = set(names)
        return (e for e in self.events if e.get("event") in wanted)

    def by_ticket(self) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for event in self.events:
            if event.get("event") not in TICKET_EVENTS:
                continue
            ticket_id = event.get("ticket_id")
            if ticket_id is None:
                continue
            grouped.setdefault(str(ticket_id), []).append(event)
        return grouped

    def ticket_events_without_id(self) -> int:
        """Ticket-scoped events with no ``ticket_id`` — unattributable, and a gap."""
        return sum(
            1
            for e in self.events
            if e.get("event") in TICKET_EVENTS and e.get("ticket_id") is None
        )

    @property
    def run_ids(self) -> set[str]:
        return {str(e["run_id"]) for e in self.events if "run_id" in e}

    @property
    def modes(self) -> set[str]:
        return {str(e["mode"]) for e in self.events if "mode" in e}

    def time_span(self, *names: str) -> tuple[float, float] | None:
        stamps = [
            float(e["ts"])
            for e in (self.of_type(*names) if names else iter(self.events))
            if "ts" in e
        ]
        return (min(stamps), max(stamps)) if stamps else None

    # -- integrity --------------------------------------------------------

    def single_mode(self) -> str:
        """The one mode this log describes, or an error.

        §3.4 makes mode a config flag for a whole run. Two modes in one log means
        runs were interleaved or logs concatenated, and every aggregate below
        would silently mix conditions.
        """
        modes = self.modes
        if len(modes) != 1:
            raise LogIntegrityError(
                f"expected exactly one mode in the log, found {sorted(modes) or 'none'}. "
                "Metrics computed across mixed modes are not a controlled comparison."
            )
        return next(iter(modes))

    def clock_skew_warning(self, tolerance_s: float = 2.0) -> str | None:
        """Flag a suspicious join between the admission log and the origin log.

        The two files are written by different containers (§5.2 q1). If their time
        spans barely overlap, joining them on timestamp attributes origin state to
        the wrong phase of the run.
        """
        ticket_span = self.time_span(*sorted(TICKET_EVENTS))
        origin_span = self.time_span("ORIGIN_SAMPLE")
        if ticket_span is None or origin_span is None:
            return None
        overlap = min(ticket_span[1], origin_span[1]) - max(ticket_span[0], origin_span[0])
        if overlap <= 0:
            return (
                f"admission events span [{ticket_span[0]:.1f}, {ticket_span[1]:.1f}] and "
                f"ORIGIN_SAMPLE spans [{origin_span[0]:.1f}, {origin_span[1]:.1f}]: they do "
                "not overlap at all. The two logs cannot be joined on timestamp."
            )
        span = max(ticket_span[1] - ticket_span[0], 1e-9)
        if overlap < span - tolerance_s:
            return (
                f"ORIGIN_SAMPLE covers only {overlap:.1f}s of the {span:.1f}s run. "
                "Origin stability figures describe part of the run only."
            )
        return None


def iter_jsonl(path: Path | str) -> Iterable[dict[str, Any]]:
    """Stream a JSONL file without holding it in memory. For very large logs."""
    with Path(path).open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)
