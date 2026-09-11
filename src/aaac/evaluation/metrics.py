"""Metrics computed from the event log, and nothing else (CLAUDE.md §4.4).

Two rules from the brief are enforced structurally here rather than left to
discipline.

**Everything is disaggregated by ``true_class``** (§4.4). Aggregates conceal
exactly the effect under study, so :class:`RunMetrics` has no way to report a
per-class metric without its per-class breakdown, and grouping uses
``true_class`` — the netem profile actually configured — never the estimated
class. Grouping by the estimate would measure the classifier instead of the
system.

**Every number carries its provenance** (§7.2). :class:`Provenance` records which
event types a figure came from, how many tickets were included, how many were
excluded and why, and whether non-completers were censored or dropped. Nothing
here silently discards a ticket: exclusions are counted by reason and travel with
the result into the report.

Censoring: time-to-completion is defined over completers only (§4.4). That is a
censored statistic, not a filtered one, so the number of non-completers is
reported next to it every time. A median time-to-completion that improves because
the slow clients never finished is not an improvement.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from aaac.evaluation.access_class import AccessClass
from aaac.evaluation.events import EventLog
from aaac.evaluation.stats import jains_index

CLASS_ORDER: tuple[AccessClass, ...] = (AccessClass.HIGH, AccessClass.MEDIUM, AccessClass.LOW)


def quantile(values: Sequence[float], q: float) -> float | None:
    """Linear-interpolated quantile. ``None`` for an empty sample, never 0.0."""
    if not values:
        return None
    if not 0.0 <= q <= 1.0:
        raise ValueError(f"q must be in [0, 1], got {q}")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = q * (len(ordered) - 1)
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    frac = pos - low
    return ordered[low] * (1.0 - frac) + ordered[high] * frac


# --------------------------------------------------------------------------
# per-ticket reconstruction
# --------------------------------------------------------------------------


@dataclass
class TicketTrace:
    """One ticket's life, reconstructed from its events."""

    ticket_id: str
    true_class: int | None = None
    estimated_class: int | None = None
    join_ts: float | None = None
    last_complete_ts: float | None = None
    completed_ok: bool = False
    admits: int = 0
    timeouts: int = 0
    timeouts_without_bytes: int = 0
    requeues: int = 0
    downgrades: int = 0
    abandoned: bool = False
    bytes_success: int = 0
    bytes_total: int = 0
    complete_missing_ok_field: bool = False
    n_events: int = 0

    @property
    def time_to_completion_s(self) -> float | None:
        if not self.completed_ok or self.join_ts is None or self.last_complete_ts is None:
            return None
        return self.last_complete_ts - self.join_ts

    @property
    def attempts(self) -> int:
        """Admissions granted. §4.4 counts ADMIT events per completed ticket."""
        return self.admits


def build_traces(log: EventLog) -> tuple[dict[str, TicketTrace], dict[str, int]]:
    """Reconstruct every ticket. Returns the traces and a count of dropped events."""
    traces: dict[str, TicketTrace] = {}
    dropped: dict[str, int] = {}

    for ticket_id, events in log.by_ticket().items():
        trace = TicketTrace(ticket_id=ticket_id)
        for event in sorted(events, key=lambda e: float(e.get("ts", 0.0))):
            trace.n_events += 1
            name = event.get("event")

            if trace.true_class is None and event.get("true_class") is not None:
                trace.true_class = int(event["true_class"])

            raw_bytes = event.get("bytes")
            if isinstance(raw_bytes, int | float) and raw_bytes > 0:
                trace.bytes_total += int(raw_bytes)

            if name == "JOIN":
                ts = event.get("ts")
                if ts is not None and (trace.join_ts is None or float(ts) < trace.join_ts):
                    trace.join_ts = float(ts)
            elif name == "ESTIMATE":
                if event.get("access_class") is not None:
                    trace.estimated_class = int(event["access_class"])
            elif name == "ADMIT":
                trace.admits += 1
            elif name == "TIMEOUT":
                trace.timeouts += 1
                # Absent is not zero: the bytes burned on this attempt are unknown.
                if event.get("bytes") is None:
                    trace.timeouts_without_bytes += 1
            elif name == "REQUEUE":
                trace.requeues += 1
            elif name == "DOWNGRADE":
                trace.downgrades += 1
            elif name == "ABANDON":
                trace.abandoned = True
            elif name == "COMPLETE":
                if "ok" in event:
                    ok = bool(event["ok"])
                else:
                    # Not defaulting silently: §4.4 defines completion as
                    # COMPLETE(ok=true), so a COMPLETE with no `ok` is a gap in
                    # the instrument and is counted as one.
                    ok = True
                    trace.complete_missing_ok_field = True
                if ok:
                    trace.completed_ok = True
                    ts = event.get("ts")
                    if ts is not None:
                        trace.last_complete_ts = float(ts)
                    success_bytes = event.get("bytes")
                    if isinstance(success_bytes, int | float) and success_bytes > 0:
                        trace.bytes_success += int(success_bytes)

        traces[ticket_id] = trace

    unattributable = log.ticket_events_without_id()
    if unattributable:
        dropped["ticket_events_without_ticket_id"] = unattributable
    return traces, dropped


# --------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------


@dataclass
class Provenance:
    """Where a number came from, and what it left out (§7.2)."""

    sources: list[str]
    run_id: str
    mode: str
    event_types_used: list[str]
    tickets_seen: int
    tickets_included: int
    exclusions: dict[str, int] = field(default_factory=dict)
    completers: int = 0
    non_completers_censored: int = 0
    disaggregated_by: str = "true_class"
    warnings: list[str] = field(default_factory=list)

    def describe(self) -> str:
        lines = [
            f"source(s)        : {', '.join(self.sources)}",
            f"run / mode       : {self.run_id} / {self.mode}",
            f"events used      : {', '.join(self.event_types_used)}",
            f"tickets seen     : {self.tickets_seen}",
            f"tickets included : {self.tickets_included}",
            f"disaggregated by : {self.disaggregated_by}",
        ]
        if self.exclusions:
            for reason, count in sorted(self.exclusions.items()):
                lines.append(f"  excluded       : {count} ({reason})")
        else:
            lines.append("  excluded       : 0")
        lines.append(
            f"completers       : {self.completers} "
            f"(non-completers censored from time-to-completion: "
            f"{self.non_completers_censored})"
        )
        for warning in self.warnings:
            lines.append(f"WARNING          : {warning}")
        return "\n".join(lines)


@dataclass
class ClassMetrics:
    """Every §4.4 metric for one ``true_class`` (or for the whole population)."""

    label: str
    access_class: int | None
    joined: int
    completed: int
    completion_rate: float
    ttc_median_s: float | None
    ttc_p95_s: float | None
    ttc_completers: int
    ttc_censored: int
    attempts_mean: float | None
    attempts_p95: float | None
    attempts_max: int | None
    attempts_ccdf: list[tuple[int, float]]
    bytes_success: int
    bytes_total: int
    goodput: float | None


@dataclass
class OriginStability:
    """§4.4: confirms protection is not sacrificed."""

    samples: int
    err_rate_mean: float | None
    err_rate_max: float | None
    p99_mean_ms: float | None
    p99_max_ms: float | None
    rejected_total: int
    served_peak: int


@dataclass
class ClassifierMetrics:
    """§4.4: ESTIMATE.access_class against true_class."""

    n: int
    accuracy: float | None
    confusion: dict[int, dict[int, int]]
    optimistic_error_rate: float | None
    """A client judged *more capable than it is* (estimate < true, since
    HIGH=0 < MEDIUM=1 < LOW=2). M2's framing, and the error that matters:
    it is the one that sends a slow client a payload it cannot fetch."""
    pessimistic_error_rate: float | None
    per_class_recall: dict[int, float | None]


@dataclass
class RunMetrics:
    provenance: Provenance
    per_class: dict[int, ClassMetrics]
    overall: ClassMetrics
    delta: float | None
    """completion_rate[HIGH] - completion_rate[LOW]. The headline result (§4.4)."""
    jain: float | None
    origin: OriginStability
    classifier: ClassifierMetrics

    def completion_rates(self) -> dict[int, float]:
        return {c: m.completion_rate for c, m in sorted(self.per_class.items())}


# --------------------------------------------------------------------------
# computation
# --------------------------------------------------------------------------


def _ccdf(attempt_counts: Sequence[int]) -> list[tuple[int, float]]:
    """P(attempts > k) for k = 0 .. max. Empty sample gives an empty CCDF."""
    if not attempt_counts:
        return []
    n = len(attempt_counts)
    highest = max(attempt_counts)
    return [
        (k, sum(1 for a in attempt_counts if a > k) / n) for k in range(0, highest + 1)
    ]


def _class_metrics(label: str, access_class: int | None, traces: list[TicketTrace]) -> ClassMetrics:
    joined = len(traces)
    completers = [t for t in traces if t.completed_ok]
    completed = len(completers)

    ttcs = [t.time_to_completion_s for t in completers]
    ttc_values = [v for v in ttcs if v is not None]

    # §4.4 counts ADMIT per *completed* ticket. Counting over all tickets would
    # mix in clients still queued at the end of the run.
    attempts = [t.attempts for t in completers]

    bytes_success = sum(t.bytes_success for t in traces)
    bytes_total = sum(t.bytes_total for t in traces)

    return ClassMetrics(
        label=label,
        access_class=access_class,
        joined=joined,
        completed=completed,
        completion_rate=(completed / joined) if joined else 0.0,
        ttc_median_s=quantile(ttc_values, 0.50),
        ttc_p95_s=quantile(ttc_values, 0.95),
        ttc_completers=len(ttc_values),
        ttc_censored=joined - completed,
        attempts_mean=(sum(attempts) / len(attempts)) if attempts else None,
        attempts_p95=quantile([float(a) for a in attempts], 0.95),
        attempts_max=max(attempts) if attempts else None,
        attempts_ccdf=_ccdf(attempts),
        bytes_success=bytes_success,
        bytes_total=bytes_total,
        # Includes bytes burned on timed-out attempts, per §4.4. A goodput that
        # ignored them would hide the cost of the retry loop entirely.
        goodput=(bytes_success / bytes_total) if bytes_total else None,
    )


def _origin_stability(log: EventLog) -> OriginStability:
    samples = list(log.of_type("ORIGIN_SAMPLE"))
    if not samples:
        return OriginStability(0, None, None, None, None, 0, 0)

    def column(key: str) -> list[float]:
        return [float(s[key]) for s in samples if s.get(key) is not None]

    err_rates = column("err_rate_1s")
    p99s = column("p99_ms")
    return OriginStability(
        samples=len(samples),
        # Each sample's err_rate_1s covers a distinct one-second bucket, so the
        # mean over samples is a legitimate run-level rate. The window totals are
        # deliberately NOT summed: consecutive samples overlap by design.
        err_rate_mean=(sum(err_rates) / len(err_rates)) if err_rates else None,
        err_rate_max=max(err_rates) if err_rates else None,
        p99_mean_ms=(sum(p99s) / len(p99s)) if p99s else None,
        p99_max_ms=max(p99s) if p99s else None,
        rejected_total=int(max((float(s.get("rejected", 0)) for s in samples), default=0)),
        served_peak=int(max((float(s.get("served", 0)) for s in samples), default=0)),
    )


def _classifier_metrics(traces: Sequence[TicketTrace]) -> ClassifierMetrics:
    pairs = [
        (t.true_class, t.estimated_class)
        for t in traces
        if t.true_class is not None and t.estimated_class is not None
    ]
    confusion: dict[int, dict[int, int]] = {
        int(t): {int(e): 0 for e in CLASS_ORDER} for t in CLASS_ORDER
    }
    for true_c, est_c in pairs:
        if true_c in confusion and est_c in confusion[true_c]:
            confusion[true_c][est_c] += 1

    n = len(pairs)
    if n == 0:
        empty_recall: dict[int, float | None] = {int(c): None for c in CLASS_ORDER}
        return ClassifierMetrics(0, None, confusion, None, None, empty_recall)

    correct = sum(1 for t, e in pairs if t == e)
    optimistic = sum(1 for t, e in pairs if e < t)
    pessimistic = sum(1 for t, e in pairs if e > t)

    recall: dict[int, float | None] = {}
    for cls in CLASS_ORDER:
        row_total = sum(confusion[int(cls)].values())
        recall[int(cls)] = (confusion[int(cls)][int(cls)] / row_total) if row_total else None

    return ClassifierMetrics(
        n=n,
        accuracy=correct / n,
        confusion=confusion,
        optimistic_error_rate=optimistic / n,
        pessimistic_error_rate=pessimistic / n,
        per_class_recall=recall,
    )


def compute(log: EventLog) -> RunMetrics:
    """Compute every §4.4 metric for one run's log."""
    traces, dropped = build_traces(log)
    exclusions: dict[str, int] = dict(dropped)
    warnings: list[str] = []

    skew = log.clock_skew_warning()
    if skew:
        warnings.append(skew)
    if log.unknown_event_types:
        warnings.append(f"events outside the §3.8 vocabulary: {log.unknown_event_types}")
    if log.malformed_lines:
        warnings.append(f"{log.malformed_lines} malformed line(s) skipped")

    all_traces = list(traces.values())

    no_join = [t for t in all_traces if t.join_ts is None]
    if no_join:
        exclusions["no JOIN event (cannot compute a completion rate)"] = len(no_join)

    usable = [t for t in all_traces if t.join_ts is not None]

    missing_true_class = [t for t in usable if t.true_class is None]
    if missing_true_class:
        exclusions["no true_class (cannot disaggregate)"] = len(missing_true_class)
        warnings.append(
            f"{len(missing_true_class)} of {len(usable)} tickets carry no true_class. "
            "§3.8 requires it echoed on every event; without it the per-class "
            "breakdown - which is the entire point of §4.4 - is incomplete."
        )

    missing_ok = sum(1 for t in usable if t.complete_missing_ok_field)
    if missing_ok:
        warnings.append(
            f"{missing_ok} COMPLETE event(s) carried no `ok` field and were counted as "
            "successful. §4.4 defines completion as COMPLETE(ok=true); this inference "
            "holds only while the admission service logs a failed completion as ABANDON "
            "rather than COMPLETE(ok=false). If that convention changes, this count is wrong."
        )

    timeouts_without_bytes = sum(t.timeouts_without_bytes for t in usable)
    if timeouts_without_bytes:
        warnings.append(
            f"{timeouts_without_bytes} TIMEOUT event(s) carried no `bytes` field, so the "
            "bytes burned on those attempts are unknown and missing from the denominator. "
            "Goodput is therefore an UPPER BOUND, not the §4.4 figure."
        )

    classed = [t for t in usable if t.true_class is not None]

    estimated = sum(1 for t in classed if t.estimated_class is not None)
    if estimated and estimated < len(classed):
        warnings.append(
            f"classifier accuracy covers {estimated} of {len(classed)} tickets. The other "
            f"{len(classed) - estimated} have no ESTIMATE event: never classified, or an "
            "estimate that was rejected. Rejected estimates are not logged, so the log "
            "cannot tell the two apart."
        )

    per_class: dict[int, ClassMetrics] = {}
    for cls in CLASS_ORDER:
        members = [t for t in classed if t.true_class == int(cls)]
        per_class[int(cls)] = _class_metrics(cls.name, int(cls), members)

    overall = _class_metrics("ALL", None, usable)

    high = per_class[int(AccessClass.HIGH)]
    low = per_class[int(AccessClass.LOW)]
    delta = (high.completion_rate - low.completion_rate) if high.joined and low.joined else None
    if delta is None:
        warnings.append(
            "Delta is undefined: HIGH or LOW has no joined tickets in this run."
        )

    rates = [per_class[int(c)].completion_rate for c in CLASS_ORDER]
    jain = jains_index(rates) if all(per_class[int(c)].joined for c in CLASS_ORDER) else None

    event_types_used = sorted({str(e.get("event")) for e in log.events})

    provenance = Provenance(
        sources=list(log.sources),
        run_id=next(iter(log.run_ids), "unknown"),
        mode=next(iter(log.modes), "unknown"),
        event_types_used=event_types_used,
        tickets_seen=len(all_traces),
        tickets_included=len(classed),
        exclusions=exclusions,
        completers=sum(1 for t in classed if t.completed_ok),
        non_completers_censored=sum(1 for t in classed if not t.completed_ok),
        warnings=warnings,
    )

    return RunMetrics(
        provenance=provenance,
        per_class=per_class,
        overall=overall,
        delta=delta,
        jain=jain,
        origin=_origin_stability(log),
        classifier=_classifier_metrics(classed),
    )


def compute_from_paths(*paths: Any, strict: bool = True) -> RunMetrics:
    return compute(EventLog.read(*paths, strict=strict))
