"""Harness, figures and report — the rest of step 6."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from aaac.evaluation import plots, report, synth
from aaac.evaluation.access_class import AccessClass
from aaac.evaluation.events import EventLog
from aaac.evaluation.experiment import (
    ComposeRunner,
    ExperimentError,
    SyntheticRunner,
    run_matrix,
)
from aaac.evaluation.population import generate
from aaac.origin.config import LoadConfig

CFG = LoadConfig(
    n_clients=240,
    scale_factor=10,
    burst_center_s=30.0,
    burst_sigma_s=15.0,
    tail_decay_s=600.0,
    class_mix={"HIGH": 0.25, "MEDIUM": 0.40, "LOW": 0.35},
    abandon_after_s=900.0,
    burst_fraction=0.8,
)
SEEDS = [1, 2, 3, 4, 5]
MODES = ["none", "baseline", "aaac"]


@pytest.fixture(scope="module")
def result(tmp_path_factory: pytest.TempPathFactory):
    out = tmp_path_factory.mktemp("experiment")
    return run_matrix(SEEDS, MODES, SyntheticRunner(), out, CFG)


# -- harness ---------------------------------------------------------------


def test_every_seed_and_mode_produced_a_run(result: Any) -> None:
    assert len(result.records) == len(SEEDS) * len(MODES)


def test_all_modes_within_a_seed_replay_the_same_population(result: Any) -> None:
    # §4.3: any difference between conditions must come from the system.
    for seed in SEEDS:
        hashes = {r.population_hash for r in result.records if r.seed == seed}
        assert len(hashes) == 1


def test_different_seeds_use_different_populations(result: Any) -> None:
    assert len({r.population_hash for r in result.records}) == len(SEEDS)


def test_a_population_mismatch_aborts_the_comparison(tmp_path: Path) -> None:
    from aaac.evaluation.experiment import RunRecord, _assert_identical_load

    fake = [
        RunRecord(1, "baseline", "s1-baseline", tmp_path / "a", "hash-a", None),  # type: ignore[arg-type]
        RunRecord(1, "aaac", "s1-aaac", tmp_path / "b", "hash-b", None),  # type: ignore[arg-type]
    ]
    with pytest.raises(ExperimentError, match="different populations"):
        _assert_identical_load(fake)


def test_series_stay_ordered_by_seed(result: Any) -> None:
    assert [r.seed for r in result.for_mode("aaac")] == SEEDS


def test_an_unknown_mode_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ExperimentError, match="unknown mode"):
        run_matrix([1], ["turbo"], SyntheticRunner(), tmp_path, CFG)


def test_the_population_is_generated_once_and_reused(tmp_path: Path) -> None:
    run_matrix([1], ["none"], SyntheticRunner(), tmp_path, CFG)
    assert (tmp_path / "population-1.json").exists()
    before = (tmp_path / "population-1.json").read_bytes()
    run_matrix([1], ["baseline"], SyntheticRunner(), tmp_path, CFG)
    assert (tmp_path / "population-1.json").read_bytes() == before


def test_the_real_runner_says_exactly_what_is_missing(tmp_path: Path) -> None:
    runner = ComposeRunner()
    with pytest.raises(ExperimentError) as exc:
        runner.run(1, "none", generate(1, CFG), tmp_path)
    message = str(exc.value)
    assert "admission service" in message
    assert "run_client" in message


def test_synthetic_logs_are_named_so_they_cannot_be_mistaken(result: Any) -> None:
    for record in result.records:
        assert Path(record.events_path).name.startswith(synth.SYNTHETIC_PREFIX)


def test_refusing_to_write_fabricated_events_under_an_innocent_name(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="synthetic"):
        synth.write([], tmp_path / "events-r07.jsonl")


# -- verdict ---------------------------------------------------------------


def test_verdict_runs_end_to_end(result: Any) -> None:
    verdict = result.verdict()
    assert len(verdict.conditions) == 3
    assert verdict.paired_difference is not None


def test_verdict_needs_both_modes(tmp_path: Path) -> None:
    partial = run_matrix([1, 2], ["none"], SyntheticRunner(), tmp_path, CFG)
    with pytest.raises(ExperimentError, match="without mode"):
        partial.verdict()


# Load generation is tested in test_loadgen.py.


# -- figures ---------------------------------------------------------------


def test_all_five_figures_render(result: Any, tmp_path: Path) -> None:
    representative = result.for_mode("aaac")[0]
    events = EventLog.read(representative.events_path).events
    paths = plots.render_all(result, tmp_path, representative.metrics, events)
    assert len(paths) == 5
    assert all(p.exists() and p.stat().st_size > 5000 for p in paths)


def test_mode_colours_are_a_fixed_order_never_cycled() -> None:
    assert list(plots.MODE_COLOUR) == list(plots.MODE_ORDER)
    assert len(set(plots.MODE_COLOUR.values())) == len(plots.MODE_COLOUR)


# -- report ----------------------------------------------------------------


def test_report_is_disaggregated_by_true_class(result: Any) -> None:
    text = report.render(result, "test")
    for cls in AccessClass:
        assert f"| {cls.name} |" in text


def test_report_carries_provenance_for_every_table(result: Any) -> None:
    text = report.render(result, "test")
    assert "Provenance for the table above" in text
    assert "disaggregated by : true_class" in text
    assert "non-completers censored" in text


def test_report_banners_fabricated_data(result: Any) -> None:
    text = report.render(result, "test")
    assert "FABRICATED DATA" in text
    assert "appear in the write-up" in text


def test_report_states_the_identical_load_check(result: Any) -> None:
    assert "Identical-load check" in report.render(result, "test")


def test_report_withholds_a_wilcoxon_it_could_not_have_rejected(result: Any) -> None:
    # Five seeds: the smallest attainable two-sided p is 0.0625.
    text = report.render(result, "test")
    assert "Wilcoxon signed-rank withheld" in text


def test_report_shows_the_headline_verdict(result: Any) -> None:
    text = report.render(result, "test")
    assert "HYPOTHESIS" in text
    assert "Δ" in text


def test_report_references_the_figures(result: Any) -> None:
    text = report.render(result, "test", Path("figures"))
    for name in ("fig1-completion-rate.png", "fig5-confusion-matrix.png"):
        assert name in text
