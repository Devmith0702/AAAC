"""Load generation (CLAUDE.md §4.3).

    I do not write my own HTTP logic — if the SDK is missing something, I ask
    Sachintha for it. Two independent client implementations would mean baseline
    and AAAC could differ for reasons neither of us controls.

So this module schedules and supervises; it never speaks HTTP. Each client is one
call to M2's ``aaac.client.sdk.run_client``, bound by :func:`load_sdk_runner`. If
the SDK cannot be imported the binding fails with a precise message, rather than
quietly substituting something of my own.

**This module cannot see ``mode``.** It is not a parameter, not read from config
here, and not branched on anywhere below. §4.3: "If the load generator ever
branches on `mode`, the experiment is broken." Making it structurally impossible
is cheaper than remembering.

**Client-side outcomes are supervision, never metrics.** ``run_client`` reports
how each session ended from the client's side. That is how a broken run gets
noticed — an admission service that was down, ticket state that vanished — but
every reported number still comes from the event log (§3.8).

Arrival times come from the replayed population, so every mode is driven by the
same schedule down to the millisecond.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from aaac.evaluation.access_class import AccessClass
from aaac.evaluation.population import Client, Population, load

#: The SDK's success label (``aaac.client.sdk.Outcome.COMPLETED``).
COMPLETED = "COMPLETED"
#: The runner raised. ``run_client`` promises never to, so this is an SDK defect.
EXCEPTION = "EXCEPTION"
#: The runner returned something with no readable outcome label.
UNRECOGNISED = "UNRECOGNISED"

#: Session endings that are not outcomes of the system under test. Each one
#: means the run itself was compromised for that client:
#:
#: - ``ADMISSION_UNAVAILABLE`` — no working admission service, so no event log.
#: - ``TICKET_UNKNOWN`` — ticket state lost mid-run (restart, eviction, purge).
#: - ``COMPLETED_UNREPORTED`` — a completion the event log never saw.
#: - ``EXCEPTION`` / ``UNRECOGNISED`` — see above.
#:
#: ``ORIGIN_UNAVAILABLE``, ``TIMED_OUT``, ``EXPIRED`` and ``ABANDONED`` are *not*
#: here: an overloaded origin and clients that fail to finish are exactly what
#: the experiment exists to observe.
INVALIDATING_OUTCOMES: frozenset[str] = frozenset(
    {"ADMISSION_UNAVAILABLE", "TICKET_UNKNOWN", "COMPLETED_UNREPORTED", EXCEPTION, UNRECOGNISED}
)

ClientRunner = Callable[[Client], Awaitable[Any]]

#: Keys of M2's ``estimator:`` config section that ``run_client`` needs.
ESTIMATOR_KEYS = ("probe_bytes", "min_rtt_samples", "confidence_threshold", "model_path")


class SdkUnavailableError(RuntimeError):
    """M2's client SDK is not importable."""


@dataclass(frozen=True)
class SdkSettings:
    """What ``run_client`` needs beyond one client's identity.

    The estimator values are M2's and are read from ``configs/run.yaml``, never
    defaulted here: a copy of someone else's numbers in my package is a copy that
    drifts. ``abandon_after_s`` comes from the replayed population, so every
    condition runs with the value that population was generated under.
    """

    admission_base: str
    delivery_base: str
    probe_bytes: int
    min_rtt_samples: int
    confidence_threshold: float
    model_path: str
    abandon_after_s: float


def sdk_settings_from_config(
    path: Path,
    population: Population,
    *,
    admission_base: str,
    delivery_base: str,
) -> SdkSettings:
    """Build :class:`SdkSettings` from the ``estimator:`` section of the run config."""
    # TODO(merge): read this through aaac.common.config once M1's loader is in the
    # tree and the local config stub is deleted.
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    estimator = raw.get("estimator") if isinstance(raw, dict) else None
    if not isinstance(estimator, dict):
        raise ValueError(
            f"{path} has no `estimator:` section. It is M2's, carries the probe and "
            "classifier settings run_client needs, and arrives with M2's branch."
        )
    missing = [key for key in ESTIMATOR_KEYS if key not in estimator]
    if missing:
        raise ValueError(f"{path}: `estimator:` is missing {missing}")
    return SdkSettings(
        admission_base=admission_base,
        delivery_base=delivery_base,
        probe_bytes=int(estimator["probe_bytes"]),
        min_rtt_samples=int(estimator["min_rtt_samples"]),
        confidence_threshold=float(estimator["confidence_threshold"]),
        model_path=str(estimator["model_path"]),
        abandon_after_s=population.abandon_after_s,
    )


def load_sdk_runner(settings: SdkSettings) -> ClientRunner:
    """Bind M2's ``run_client``. Raises rather than substituting a local client."""
    try:
        from aaac.client.sdk import run_client  # type: ignore[import-not-found]
    except ImportError as exc:
        raise SdkUnavailableError(
            f"M2's client SDK (`aaac.client.sdk.run_client`) cannot be imported ({exc}), "
            "so no real load can be generated. §4.3 forbids me writing my own HTTP client: "
            "two implementations would let baseline and AAAC differ for reasons neither of "
            "us controls. Merge M2's branch and install its dependencies, or pass an "
            "explicit runner for testing."
        ) from exc

    async def runner(client: Client) -> Any:
        # true_class is passed straight through as an opaque label so it can be
        # echoed onto events (§3.8). It must never influence an SDK decision.
        # poll_jitter_frac is left at the SDK's default: spreading the herd would
        # change the experiment being measured, which is not this module's call.
        return await run_client(
            client_id=client.client_id,
            true_class=AccessClass(client.true_class),
            index_no=str(client.index_no),
            admission_base=settings.admission_base,
            delivery_base=settings.delivery_base,
            probe_bytes=settings.probe_bytes,
            min_rtt_samples=settings.min_rtt_samples,
            confidence_threshold=settings.confidence_threshold,
            model_path=settings.model_path,
            abandon_after_s=settings.abandon_after_s,
            seed=client.seed,
        )

    return runner


def classify(result: Any) -> tuple[str, str | None]:
    """Read the SDK's ``ClientOutcome`` as ``(label, error)`` without importing it."""
    outcome = getattr(result, "outcome", None)
    label = getattr(outcome, "value", outcome)
    if not isinstance(label, str) or not label:
        return UNRECOGNISED, f"runner returned {type(result).__name__} with no readable outcome"
    error = getattr(result, "error", None)
    return label, (str(error) if error else None)


@dataclass
class ClientRecord:
    client_index: int
    client_id: str
    true_class: int
    scheduled_s: float
    started_s: float
    finished_s: float
    outcome: str
    """The SDK's label for how the session ended, or EXCEPTION / UNRECOGNISED."""
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome == COMPLETED

    @property
    def launch_lag_s(self) -> float:
        """How late this client actually started against its scheduled arrival.

        The honest measure of whether the machine kept up with the arrival
        process. §5.2 q4 flags that 20k concurrent clients may not be achievable;
        a large lag here is that ceiling showing up, and it must be reported, not
        absorbed.
        """
        return self.started_s - self.scheduled_s


@dataclass
class LoadReport:
    records: list[ClientRecord]
    n_clients: int
    concurrency_cap: int
    wall_clock_s: float
    population_hash: str
    max_launch_lag_s: float = 0.0
    p95_launch_lag_s: float = 0.0
    by_outcome: dict[str, int] = field(default_factory=dict)

    @property
    def completed(self) -> int:
        return sum(1 for r in self.records if r.ok)

    def lag_warning(self, tolerance_s: float = 1.0) -> str | None:
        if self.p95_launch_lag_s > tolerance_s:
            return (
                f"p95 launch lag was {self.p95_launch_lag_s:.2f}s (max "
                f"{self.max_launch_lag_s:.2f}s). The machine did not keep up with the "
                "arrival process, so the flash crowd actually delivered is flatter than "
                "the one configured. Reduce n_clients honestly or raise the cap — do not "
                "report this run as the configured arrival profile."
            )
        return None

    def infrastructure_warning(self) -> str | None:
        compromised = {
            label: count
            for label, count in sorted(self.by_outcome.items())
            if label in INVALIDATING_OUTCOMES
        }
        if not compromised:
            return None
        return (
            f"{sum(compromised.values())} of {self.n_clients} client(s) ended in ways that "
            f"are not outcomes of the system under test: {compromised}. The event log is "
            "incomplete for them. Do not report this run as a clean measurement — fix the "
            "infrastructure and re-run the same seed."
        )


async def _drive_one(
    client: Client,
    runner: ClientRunner,
    semaphore: asyncio.Semaphore,
    t0: float,
    records: list[ClientRecord],
) -> None:
    delay = (t0 + client.arrival_s) - time.monotonic()
    if delay > 0:
        await asyncio.sleep(delay)

    async with semaphore:
        started = time.monotonic()
        try:
            label, error = classify(await runner(client))
        except Exception as exc:  # noqa: BLE001 - every failure is data
            label, error = EXCEPTION, f"{type(exc).__name__}: {exc}"
        records.append(
            ClientRecord(
                client_index=client.client_index,
                client_id=client.client_id,
                true_class=client.true_class,
                scheduled_s=client.arrival_s,
                started_s=started - t0,
                finished_s=time.monotonic() - t0,
                outcome=label,
                error=error,
            )
        )


async def run_population(
    clients: Sequence[Client],
    runner: ClientRunner,
    *,
    concurrency_cap: int,
    population_hash: str = "",
) -> LoadReport:
    """Drive ``clients`` on their scheduled arrivals, capped at ``concurrency_cap``."""
    if concurrency_cap < 1:
        raise ValueError(f"concurrency_cap must be >= 1, got {concurrency_cap}")

    records: list[ClientRecord] = []
    semaphore = asyncio.Semaphore(concurrency_cap)
    t0 = time.monotonic()

    await asyncio.gather(*(_drive_one(c, runner, semaphore, t0, records) for c in clients))
    wall = time.monotonic() - t0

    lags = sorted(r.launch_lag_s for r in records)
    by_outcome: dict[str, int] = {}
    for record in records:
        by_outcome[record.outcome] = by_outcome.get(record.outcome, 0) + 1

    return LoadReport(
        records=sorted(records, key=lambda r: r.client_index),
        n_clients=len(clients),
        concurrency_cap=concurrency_cap,
        wall_clock_s=wall,
        population_hash=population_hash,
        max_launch_lag_s=lags[-1] if lags else 0.0,
        p95_launch_lag_s=lags[int(0.95 * (len(lags) - 1))] if lags else 0.0,
        by_outcome=by_outcome,
    )


async def run_class(
    population: Population,
    access_class: AccessClass,
    runner: ClientRunner,
    *,
    concurrency_cap: int,
) -> LoadReport:
    """Drive only one class — each shaped container runs its own class's clients."""
    return await run_population(
        population.by_class(access_class),
        runner,
        concurrency_cap=concurrency_cap,
        population_hash=population.population_hash,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Drive the client population (§4.3)")
    parser.add_argument("--population", type=Path, required=True)
    parser.add_argument("--class", dest="access_class", required=True,
                        choices=[c.name for c in AccessClass])
    parser.add_argument("--config", type=Path, default=Path("configs/run.yaml"))
    parser.add_argument("--admission-base", default="http://admission:8000")
    parser.add_argument("--delivery-base", default="http://delivery:8001")
    parser.add_argument("--concurrency", type=int, default=500)
    args = parser.parse_args(argv)

    population = load(args.population)
    access_class = AccessClass[args.access_class]

    try:
        settings = sdk_settings_from_config(
            args.config,
            population,
            admission_base=args.admission_base,
            delivery_base=args.delivery_base,
        )
        runner = load_sdk_runner(settings)
    except (ValueError, SdkUnavailableError) as exc:
        print(f"loadgen: {exc}")
        return 2

    report = asyncio.run(
        run_class(population, access_class, runner, concurrency_cap=args.concurrency)
    )
    print(
        f"{access_class.name}: {report.n_clients} clients in {report.wall_clock_s:.1f}s "
        f"(cap {report.concurrency_cap})"
    )
    print(f"client-side outcomes (supervision only, not a metric): {report.by_outcome}")
    for warning in (report.lag_warning(), report.infrastructure_warning()):
        if warning:
            print(f"WARNING: {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
