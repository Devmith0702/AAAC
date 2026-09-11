"""Experiment harness (CLAUDE.md §4.5).

    make experiment SEEDS=1,2,3,4,5

For each seed, every mode runs against the **identical replayed population**.
That is the whole basis of the comparison, so it is enforced rather than trusted:
each run records the SHA-256 of the population it was driven with, and
:func:`run_matrix` aborts if two runs of the same seed disagree.

Runners are pluggable. :class:`SyntheticRunner` fabricates a log so the analysis
chain — metrics, statistics, falsification, figures, report — is exercisable and
tested today. :class:`ComposeRunner` is the real one and is **not yet
implementable**: it needs M1's admission service and M2's client SDK, neither of
which exists. It therefore fails with a message naming exactly what is missing,
rather than quietly producing something that looks like a measurement.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from aaac.evaluation import synth
from aaac.evaluation.access_class import AccessClass
from aaac.evaluation.events import EventLog
from aaac.evaluation.falsification import Verdict, evaluate
from aaac.evaluation.metrics import RunMetrics, compute
from aaac.evaluation.population import Population, generate, load, population_path, write
from aaac.origin.config import LoadConfig, RunConfig, load_run_config

MODES: tuple[str, ...] = ("none", "baseline", "aaac")


class ExperimentError(RuntimeError):
    """The experiment cannot be run or compared as specified."""


def run_id_for(seed: int, mode: str) -> str:
    return f"s{seed}-{mode}"


# --------------------------------------------------------------------------
# runners
# --------------------------------------------------------------------------


class Runner(Protocol):
    """Drives one (seed, mode) run and returns the path to its event log."""

    prefix: str

    def prepare(self) -> None: ...

    def run(self, seed: int, mode: str, population: Population, results_dir: Path) -> Path: ...


@dataclass
class SyntheticRunner:
    """FABRICATED. Produces `synthetic-*.jsonl`; never a measurement.

    Exists so the analysis chain and the §4.7 falsification branch are tested
    before the real services land.
    """

    scenario_for: Callable[[str], synth.Scenario] = field(
        default_factory=lambda: _default_scenarios
    )
    prefix: str = synth.SYNTHETIC_PREFIX

    def prepare(self) -> None:
        return None

    def run(self, seed: int, mode: str, population: Population, results_dir: Path) -> Path:
        run_id = run_id_for(seed, mode)
        events = synth.synthesize(
            population, mode, self.scenario_for(mode), run_id=run_id, seed=seed
        )
        return synth.write(events, results_dir / f"{self.prefix}events-{run_id}.jsonl")


def _default_scenarios(mode: str) -> synth.Scenario:
    if mode == "aaac":
        return synth.helpful_scenario()
    return synth.null_scenario(mode)


@dataclass
class ComposeRunner:
    """The real runner: drives the docker-compose stack for one (seed, mode).

    §4.5 requires flushing Redis, rotating the event log and restarting services
    between runs, all of which is straightforward. What is not yet possible is the
    run itself.
    """

    prefix: str = ""
    compose_file: Path = Path("docker-compose.yml")

    def prepare(self) -> None:
        if shutil.which("docker") is None:
            raise ExperimentError("`docker` is not on PATH")

    def reset_between_runs(self) -> None:
        """Flush Redis and restart services (§4.5). Safe to call today."""
        subprocess.run(
            ["docker", "compose", "exec", "-T", "redis", "redis-cli", "FLUSHALL"],
            check=False, capture_output=True, text=True,
        )
        subprocess.run(
            ["docker", "compose", "restart", "origin"],
            check=False, capture_output=True, text=True,
        )

    def run(self, seed: int, mode: str, population: Population, results_dir: Path) -> Path:
        raise ExperimentError(
            "ComposeRunner cannot run yet. A real run needs:\n"
            "  - M1's admission service at http://admission:8000 (the single writer of\n"
            "    the event log, §3.8) — on M1's branch, not yet merged or in compose\n"
            "  - M2's SDK (`aaac.client.sdk.run_client`), which the load generator\n"
            "    drives (§4.3 forbids me writing my own HTTP logic) — on M2's branch,\n"
            "    not yet merged\n"
            "  - M2's delivery service at http://delivery:8001, wired into compose\n"
            "Until then, use --synthetic to exercise the analysis chain, and read its\n"
            "output as fabricated data rather than a measurement."
        )


# --------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------


@dataclass
class RunRecord:
    seed: int
    mode: str
    run_id: str
    events_path: Path
    population_hash: str
    metrics: RunMetrics


@dataclass
class ExperimentResult:
    records: list[RunRecord]
    seeds: list[int]
    modes: list[str]
    synthetic: bool

    def for_mode(self, mode: str) -> list[RunRecord]:
        """Records for one mode, ordered by seed so series stay paired."""
        return sorted((r for r in self.records if r.mode == mode), key=lambda r: r.seed)

    def series(self, mode: str, extract: Callable[[RunMetrics], float | None]) -> list[float]:
        values = [extract(r.metrics) for r in self.for_mode(mode)]
        missing = [s for s, v in zip(self.seeds, values, strict=False) if v is None]
        if missing:
            raise ExperimentError(
                f"mode {mode!r}: metric undefined for seed(s) {missing}. Dropping them "
                "would unpair the comparison; fix the run instead."
            )
        return [float(v) for v in values if v is not None]

    def delta_series(self, mode: str) -> list[float]:
        return self.series(mode, lambda m: m.delta)

    def origin_err_series(self, mode: str) -> list[float]:
        return self.series(mode, lambda m: m.origin.err_rate_mean or 0.0)

    def high_p95_series(self, mode: str) -> list[float]:
        return self.series(mode, lambda m: m.per_class[int(AccessClass.HIGH)].ttc_p95_s)

    def aggregate_completion_series(self, mode: str) -> list[float]:
        return self.series(mode, lambda m: m.overall.completion_rate)

    def verdict(self) -> Verdict:
        """Apply the §4.7 rule to baseline vs aaac."""
        for mode in ("baseline", "aaac"):
            if mode not in self.modes:
                raise ExperimentError(
                    f"cannot evaluate the hypothesis without mode {mode!r} in the run matrix"
                )
        return evaluate(
            delta_baseline=self.delta_series("baseline"),
            delta_aaac=self.delta_series("aaac"),
            origin_err_baseline=self.origin_err_series("baseline"),
            origin_err_aaac=self.origin_err_series("aaac"),
            high_p95_baseline=self.high_p95_series("baseline"),
            high_p95_aaac=self.high_p95_series("aaac"),
            aggregate_completion_baseline=self.aggregate_completion_series("baseline"),
            aggregate_completion_aaac=self.aggregate_completion_series("aaac"),
        )


# --------------------------------------------------------------------------
# driving
# --------------------------------------------------------------------------


def ensure_population(seed: int, load_cfg: LoadConfig, results_dir: Path) -> Population:
    """Load the population for ``seed``, generating it once if absent (§4.3)."""
    path = population_path(results_dir, seed)
    if path.exists():
        population = load(path)
        if population.n_clients != load_cfg.n_clients:
            raise ExperimentError(
                f"{path} has n_clients={population.n_clients} but configs/run.yaml says "
                f"{load_cfg.n_clients}. Regenerate the population rather than comparing "
                "runs driven by different loads."
            )
        return population
    population = generate(seed, load_cfg)
    write(population, results_dir)
    return population


def run_matrix(
    seeds: Sequence[int],
    modes: Sequence[str],
    runner: Runner,
    results_dir: Path,
    load_cfg: LoadConfig,
    *,
    origin_log: Callable[[str], Path] | None = None,
) -> ExperimentResult:
    """Run every (seed, mode) against the identical replayed population."""
    unknown = [m for m in modes if m not in MODES]
    if unknown:
        raise ExperimentError(f"unknown mode(s) {unknown}; §3.4 defines exactly {list(MODES)}")

    runner.prepare()
    results_dir = Path(results_dir)
    records: list[RunRecord] = []

    for seed in seeds:
        population = ensure_population(seed, load_cfg, results_dir)
        for mode in modes:
            events_path = runner.run(seed, mode, population, results_dir)
            sources: list[Path] = [events_path]
            if origin_log is not None:
                candidate = origin_log(run_id_for(seed, mode))
                if candidate.exists():
                    sources.append(candidate)
            log = EventLog.read(*sources)
            log.single_mode()
            records.append(
                RunRecord(
                    seed=seed,
                    mode=mode,
                    run_id=run_id_for(seed, mode),
                    events_path=events_path,
                    population_hash=population.population_hash,
                    metrics=compute(log),
                )
            )

    _assert_identical_load(records)
    return ExperimentResult(
        records=records,
        seeds=list(seeds),
        modes=list(modes),
        synthetic=bool(getattr(runner, "prefix", "")),
    )


def _assert_identical_load(records: Sequence[RunRecord]) -> None:
    """§4.3: any difference between conditions must come from the system."""
    by_seed: dict[int, set[str]] = {}
    for record in records:
        by_seed.setdefault(record.seed, set()).add(record.population_hash)
    broken = {seed: hashes for seed, hashes in by_seed.items() if len(hashes) > 1}
    if broken:
        raise ExperimentError(
            f"modes were driven by different populations for seed(s) {sorted(broken)}. "
            "The comparison is not controlled and the results are not usable."
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the AAAC experiment matrix (§4.5)")
    parser.add_argument("--seeds", default="1,2,3,4,5")
    parser.add_argument("--modes", default=",".join(MODES))
    parser.add_argument("--results", type=Path, default=Path("results"))
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument(
        "--synthetic",
        action="store_true",
        help="FABRICATED DATA. Exercises the analysis chain with invented events.",
    )
    args = parser.parse_args(argv)

    config: RunConfig = load_run_config(args.config)
    load_cfg = config.require_load()
    seeds = [int(s) for s in args.seeds.split(",") if s.strip()]
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]

    runner: Runner = SyntheticRunner() if args.synthetic else ComposeRunner()
    if args.synthetic:
        print("!" * 72)
        print("SYNTHETIC RUN — every event below is fabricated. Not a measurement.")
        print("!" * 72)

    try:
        result = run_matrix(seeds, modes, runner, args.results, load_cfg)
    except ExperimentError as exc:
        print(f"experiment: {exc}", file=sys.stderr)
        return 2

    header = (
        f"\n{'seed':>5} {'mode':>9} {'Delta':>9} {'HIGH':>7} {'MED':>7} "
        f"{'LOW':>7} {'aggregate':>10}"
    )
    print(header)
    for record in sorted(result.records, key=lambda r: (r.seed, r.mode)):
        m = record.metrics
        rates = m.completion_rates()
        delta = f"{m.delta:.4f}" if m.delta is not None else "n/a"
        print(
            f"{record.seed:>5} {record.mode:>9} {delta:>9} "
            f"{rates.get(0, 0):>7.4f} {rates.get(1, 0):>7.4f} {rates.get(2, 0):>7.4f} "
            f"{m.overall.completion_rate:>10.4f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
