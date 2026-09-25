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
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from aaac.common.classes import AccessClass
from aaac.evaluation import synth
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


def load_matrix(
    seeds: Sequence[int],
    modes: Sequence[str],
    results_dir: Path,
) -> ExperimentResult:
    """Build an ExperimentResult from event logs already on disk.

    The figure and report generators used to refuse to run without
    ``--synthetic`` ("no real event logs exist yet — M1's admission service is
    not in the repository"), a guard written before M1 was merged. Worse, the
    ``--synthetic`` path called ``run_matrix(..., SyntheticRunner(), ...)``,
    which FABRICATES events into the same results directory — so rendering the
    figures for a real run would have overwritten the measurement with invented
    data (INTEGRATION-ISSUES.md A16).

    This reads what the admission service actually wrote and computes nothing
    it did not observe.
    """
    results_dir = Path(results_dir)
    records: list[RunRecord] = []

    for seed in seeds:
        pop_file = population_path(results_dir, seed)
        if not pop_file.exists():
            raise ExperimentError(
                f"{pop_file} is missing; the population is what proves every mode "
                "replayed the same load (§4.3)"
            )
        population = load(pop_file)

        for mode in modes:
            run_id = run_id_for(seed, mode)
            events_path = results_dir / f"events-{run_id}.jsonl"
            if not events_path.exists():
                raise ExperimentError(
                    f"{events_path} is missing. Run the experiment for seed {seed} "
                    f"mode {mode!r} first, or pass --synthetic to exercise the "
                    "analysis chain on fabricated data instead."
                )
            sources = [events_path]
            origin_path = results_dir / f"origin-{run_id}.jsonl"
            if origin_path.exists():
                sources.append(origin_path)

            log = EventLog.read(*sources)
            log.single_mode()
            records.append(
                RunRecord(
                    seed=seed,
                    mode=mode,
                    run_id=run_id,
                    events_path=events_path,
                    population_hash=population.population_hash,
                    metrics=compute(log),
                )
            )

    _assert_identical_load(records)
    return ExperimentResult(
        records=records, seeds=list(seeds), modes=list(modes), synthetic=False
    )


def _wait_for_admission(
    url: str = "http://127.0.0.1:8000/admin/snapshot", timeout_s: float = 90.0
) -> None:
    """Block until the admission service answers, or raise.

    A fixed sleep after `--force-recreate` is a race: the load generator would
    start against a service that is still binding its port, and every client
    would return ADMISSION_UNAVAILABLE — an infrastructure failure that looks
    nothing like a client that failed to finish, and would poison the run.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:  # noqa: S310
                if response.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.5)
    raise ExperimentError(
        f"the admission service did not answer {url} within {timeout_s:.0f}s after "
        "being recreated; the run would have measured an outage, not the system"
    )


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
        run_id = run_id_for(seed, mode)
        
        # 1. Update configs/run.yaml using string replacement to preserve M3's comments
        config_path = self.compose_file.parent / "configs" / "run.yaml"
        config_text = config_path.read_text()
        import re
        config_text = re.sub(r"^seed:.*", f"seed: {seed}", config_text, flags=re.MULTILINE)
        config_text = re.sub(r"^run_id:.*", f"run_id: {run_id}", config_text, flags=re.MULTILINE)
        config_text = re.sub(r"^mode:.*", f"mode: {mode}", config_text, flags=re.MULTILINE)
        config_path.write_text(config_text)
        
        # 2. Recreate the services carrying THIS run's id.
        #
        # `restart` is not enough. AAAC_RUN_ID is fixed in the container
        # environment when the stack comes up, and M1 reads it in preference to
        # the run_id just written into run.yaml — so every run in the matrix
        # would append to one event log, and EventLog.single_mode() would refuse
        # the result for holding more than one mode. The origin needs the same
        # id, or its ORIGIN_SAMPLE log cannot be paired with M1's.
        env = {**os.environ, "AAAC_RUN_ID": run_id}
        subprocess.run(
            ["docker", "compose", "up", "-d", "--force-recreate",
             "origin", "admission", "delivery"],
            check=True, capture_output=True, text=True, env=env,
        )
        _wait_for_admission()

        # Flush the queue state. `reset_between_runs` existed but was never
        # called, so every run inherited its predecessors' tickets: 1,608 keys
        # were found in Redis across eight runs (aaac:s1-none:ticket:* and so
        # on). The controller sweeps those dead tickets every tick and their
        # TIMEOUT/REQUEUE/ADMIT events land in the CURRENT run's log — 253
        # foreign events in one measured run. Flushed after the services are up
        # so the store is empty exactly when load starts.
        subprocess.run(
            ["docker", "compose", "exec", "-T", "redis", "redis-cli", "FLUSHALL"],
            check=True, capture_output=True, text=True,
        )
        
        # 3. Drive the load generator against the compose stack
        pop_file = population_path(results_dir, seed)
        procs = []
        for cls_name in ["HIGH", "MEDIUM", "LOW"]:
            p = subprocess.Popen(
                [
                    "docker", "compose", "exec", "-T", f"client-{cls_name.lower()}",
                    "python", "-m", "aaac.evaluation.loadgen",
                    "--population", str(pop_file),
                    "--class", cls_name,
                ]
            )
            procs.append(p)
            
        # Wait for all load generators to finish
        try:
            for p in procs:
                p.wait()
                if p.returncode != 0:
                    raise ExperimentError("Load generator failed in one or more containers")
        finally:
            for p in procs:
                if p.poll() is None:
                    p.kill()
                    p.wait()
        
        # 4. Return the path to the event log written by the admission service
        return results_dir / f"{self.prefix}events-{run_id}.jsonl"


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
