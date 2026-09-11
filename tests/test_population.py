"""The replayable population — the guarantee that all modes see identical load."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from aaac.evaluation.access_class import AccessClass
from aaac.evaluation.population import apportion, generate, load, write
from aaac.origin.config import LoadConfig

CFG = LoadConfig(
    n_clients=1000,
    scale_factor=10,
    burst_center_s=30.0,
    burst_sigma_s=15.0,
    tail_decay_s=600.0,
    class_mix={"HIGH": 0.25, "MEDIUM": 0.40, "LOW": 0.35},
    abandon_after_s=900.0,
    burst_fraction=0.8,
)


def test_same_seed_replays_exactly() -> None:
    assert generate(1, CFG) == generate(1, CFG)


def test_different_seeds_give_different_populations() -> None:
    assert generate(1, CFG).population_hash != generate(2, CFG).population_hash


def test_class_counts_are_exact_not_sampled() -> None:
    # Sampling would give each seed slightly different class counts, adding
    # variance that has nothing to do with the system under test.
    for seed in (1, 2, 3, 4, 5):
        counts = generate(seed, CFG).class_counts
        assert counts == {"HIGH": 250, "MEDIUM": 400, "LOW": 350}


def test_apportion_always_sums_to_n() -> None:
    for n in (1, 7, 999, 1000, 20_001):
        counts = apportion(n, CFG.class_mix)
        assert sum(counts.values()) == n


def test_apportion_handles_awkward_remainders() -> None:
    counts = apportion(10, {"HIGH": 1 / 3, "MEDIUM": 1 / 3, "LOW": 1 / 3})
    assert sum(counts.values()) == 10
    assert max(counts.values()) - min(counts.values()) <= 1


def test_index_numbers_are_unique() -> None:
    population = generate(1, CFG)
    numbers = [c.index_no for c in population.clients]
    assert len(set(numbers)) == len(numbers)


def test_client_seeds_follow_seed_plus_index() -> None:
    population = generate(7, CFG)
    assert {c.seed for c in population.clients} == {7 + i for i in range(CFG.n_clients)}


def test_arrivals_are_never_negative() -> None:
    assert all(c.arrival_s >= 0.0 for c in generate(1, CFG).clients)


def test_arrivals_are_sorted() -> None:
    arrivals = [c.arrival_s for c in generate(1, CFG).clients]
    assert arrivals == sorted(arrivals)


def test_arrival_process_is_a_burst_with_a_tail() -> None:
    # A flash crowd, not steady state (§4.3): most arrivals cluster near the
    # burst centre, and a minority stretch well past it.
    arrivals = [c.arrival_s for c in generate(1, CFG).clients]
    near_burst = sum(1 for a in arrivals if abs(a - CFG.burst_center_s) <= 2 * CFG.burst_sigma_s)
    assert near_burst / len(arrivals) > 0.6
    assert max(arrivals) > CFG.burst_center_s + CFG.tail_decay_s / 4


def test_class_is_independent_of_arrival_time() -> None:
    # If class correlated with arrival order, the burst would hit one class
    # first and the comparison would confound class with timing.
    population = generate(1, CFG)
    first_half = population.clients[: len(population.clients) // 2]
    share_low = sum(1 for c in first_half if c.true_class == int(AccessClass.LOW))
    assert 0.28 < share_low / len(first_half) < 0.42


def test_round_trip_through_disk(tmp_path: Path) -> None:
    original = generate(3, CFG)
    path = write(original, tmp_path)
    assert load(path) == original


def test_a_tampered_population_file_is_refused(tmp_path: Path) -> None:
    # The hash exists so the analysis can prove all modes saw the same load
    # rather than assuming it.
    path = write(generate(3, CFG), tmp_path)
    raw = json.loads(path.read_text())
    raw["clients"][0]["true_class"] = 0
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="hash"):
        load(path)


def test_by_class_partitions_the_population() -> None:
    population = generate(1, CFG)
    total = sum(len(population.by_class(c)) for c in AccessClass)
    assert total == CFG.n_clients


def _identifiers(path: Path) -> set[str]:
    """Every name, attribute and non-docstring string constant in a module."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                docstrings.add(doc)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.arg) or (isinstance(node, ast.keyword) and node.arg):
            found.add(node.arg)
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value not in docstrings
        ):
            found.add(node.value)
    return found


@pytest.mark.parametrize("module", ["population.py", "loadgen.py"])
def test_load_generation_cannot_see_mode(module: str) -> None:
    # §4.3: "If the load generator ever branches on `mode`, the experiment is
    # broken." Structural, not a promise to be careful.
    path = Path(__file__).resolve().parents[1] / "src" / "aaac" / "evaluation" / module
    assert "mode" not in _identifiers(path), f"{module} references `mode`"
