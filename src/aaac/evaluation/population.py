"""Replayable client population (CLAUDE.md §4.3).

    make population SEEDS=1,2,3,4,5

Generates the population **once** per seed into ``results/population-{seed}.json``
and replays it across every mode. §4.3 is unambiguous about why:

    Any difference between conditions must come from the system, not the load.
    If the load generator ever branches on `mode`, the experiment is broken.

Nothing in this module can see ``mode``. It is not passed in, not read from
config here, and not present in the output file. That is deliberate — the
guarantee is structural rather than a promise to be careful.

Two further properties the comparison depends on:

* **Exact class counts.** The class mix is apportioned by largest remainder, not
  sampled. Sampling would give each seed slightly different class counts, adding
  variance that has nothing to do with the system under test.
* **A population hash.** Every run records the SHA-256 of the population it was
  driven with, so the analysis can *prove* all three modes saw identical load
  rather than assuming it.

The arrival process is a flash crowd, not steady state: a Gaussian burst centred
at ``burst_center_s`` carrying ``burst_fraction`` of arrivals, plus an
exponential tail with mean ``tail_decay_s`` for the rest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from aaac.evaluation.access_class import AccessClass
from aaac.origin.config import LoadConfig, RunConfig, load_run_config

POPULATION_VERSION = 1

#: Index numbers are drawn from this block so they look like real A/L numbers
#: and never collide with anything else in a log.
INDEX_BASE = 900_000


@dataclass(frozen=True)
class Client:
    """One synthetic student. Immutable, and identical across all three modes."""

    client_index: int
    client_id: str
    index_no: int
    true_class: int
    arrival_s: float
    seed: int


@dataclass(frozen=True)
class Population:
    seed: int
    n_clients: int
    scale_factor: int
    class_mix: dict[str, float]
    class_counts: dict[str, int]
    burst_center_s: float
    burst_sigma_s: float
    tail_decay_s: float
    burst_fraction: float
    abandon_after_s: float
    version: int
    population_hash: str
    clients: list[Client]

    def to_json(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["clients"] = [asdict(c) for c in self.clients]
        return payload

    def by_class(self, access_class: AccessClass) -> list[Client]:
        return [c for c in self.clients if c.true_class == int(access_class)]


def apportion(n_clients: int, class_mix: dict[str, float]) -> dict[str, int]:
    """Split ``n_clients`` by ``class_mix`` using largest remainder.

    Deterministic and exact: the counts always sum to ``n_clients``, so two seeds
    differ in *who* is in each class but never in *how many*.
    """
    raw = {name: n_clients * share for name, share in class_mix.items()}
    counts = {name: int(value) for name, value in raw.items()}
    shortfall = n_clients - sum(counts.values())
    # Largest fractional remainder first; ties broken by the fixed class order so
    # the result does not depend on dict iteration order.
    order = sorted(
        raw, key=lambda name: (-(raw[name] - counts[name]), list(class_mix).index(name))
    )
    for name in order[:shortfall]:
        counts[name] += 1
    return counts


def _arrival_s(rng: random.Random, load: LoadConfig) -> float:
    """One arrival time from the burst/tail mixture. Never negative."""
    if rng.random() < load.burst_fraction:
        arrival = rng.gauss(load.burst_center_s, load.burst_sigma_s)
    else:
        arrival = load.burst_center_s + rng.expovariate(1.0 / load.tail_decay_s)
    return max(0.0, arrival)


def _hash_clients(clients: list[Client]) -> str:
    payload = json.dumps([asdict(c) for c in clients], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def generate(seed: int, load: LoadConfig) -> Population:
    """Build the population for ``seed``. Pure: same inputs, same output, always."""
    counts = apportion(load.n_clients, load.class_mix)

    # Class labels laid out in a fixed order, then permuted once. This keeps the
    # exact counts of `apportion` while making class independent of arrival time.
    labels: list[str] = []
    for name in load.class_mix:
        labels.extend([name] * counts[name])
    rng = random.Random(seed)
    rng.shuffle(labels)

    # Index numbers permuted from a contiguous block so they are unique and
    # carry no information about class or arrival order.
    index_numbers = list(range(INDEX_BASE, INDEX_BASE + load.n_clients))
    rng.shuffle(index_numbers)

    clients: list[Client] = []
    for i, (label, index_no) in enumerate(zip(labels, index_numbers, strict=True)):
        # §4.3: each client seeded from `seed + client_index` so runs replay
        # exactly and a single client can be reproduced in isolation.
        client_seed = seed + i
        clients.append(
            Client(
                client_index=i,
                client_id=f"c-{seed}-{i:06d}",
                index_no=index_no,
                true_class=int(AccessClass[label]),
                arrival_s=round(_arrival_s(random.Random(client_seed), load), 6),
                seed=client_seed,
            )
        )

    clients.sort(key=lambda c: (c.arrival_s, c.client_index))

    return Population(
        seed=seed,
        n_clients=load.n_clients,
        scale_factor=load.scale_factor,
        class_mix=dict(load.class_mix),
        class_counts=counts,
        burst_center_s=load.burst_center_s,
        burst_sigma_s=load.burst_sigma_s,
        tail_decay_s=load.tail_decay_s,
        burst_fraction=load.burst_fraction,
        abandon_after_s=load.abandon_after_s,
        version=POPULATION_VERSION,
        population_hash=_hash_clients(clients),
        clients=clients,
    )


def population_path(results_dir: Path, seed: int) -> Path:
    return Path(results_dir) / f"population-{seed}.json"


def write(population: Population, results_dir: Path) -> Path:
    path = population_path(results_dir, population.seed)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(population.to_json(), indent=2), encoding="utf-8")
    return path


def load(path: Path) -> Population:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    clients = [Client(**c) for c in raw.pop("clients")]
    population = Population(clients=clients, **raw)
    if population.population_hash != _hash_clients(clients):
        raise ValueError(
            f"{path}: population hash does not match its contents. The file has been "
            "edited or truncated; regenerate it rather than running against it."
        )
    return population


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate replayable client populations")
    parser.add_argument("--seeds", default="1,2,3,4,5", help="comma-separated seeds")
    parser.add_argument("--out", type=Path, default=Path("results"))
    parser.add_argument("--config", type=Path, default=None)
    args = parser.parse_args(argv)

    config: RunConfig = load_run_config(args.config)
    load_cfg = config.require_load()

    print(f"{'seed':>6}  {'clients':>8}  {'HIGH':>6} {'MEDIUM':>7} {'LOW':>6}  "
          f"{'p50 arr':>8} {'p95 arr':>8}  hash")
    for seed in (int(s) for s in args.seeds.split(",") if s.strip()):
        population = generate(seed, load_cfg)
        path = write(population, args.out)
        arrivals = [c.arrival_s for c in population.clients]
        p50 = arrivals[len(arrivals) // 2]
        p95 = arrivals[int(0.95 * len(arrivals))]
        counts = population.class_counts
        print(
            f"{seed:>6}  {population.n_clients:>8}  {counts['HIGH']:>6} {counts['MEDIUM']:>7} "
            f"{counts['LOW']:>6}  {p50:>8.1f} {p95:>8.1f}  "
            f"{population.population_hash[:12]}  -> {path}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
