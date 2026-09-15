"""Run configuration: M3's own sections, layered over M1's loader.

``parse_run_config`` is M3's parsing of the M3-owned sections and is tested with
partial dicts. ``load_run_config`` reads a real file and first puts it through
M1's loader, so the fixtures it uses carry every §3.9 section — a file missing
one of them is a file the admission service would refuse to start on.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from aaac.origin.config import (
    CONTRACT_LOAD_KEYS,
    CONTRACT_ORIGIN_KEYS,
    DEFAULT_BURST_FRACTION,
    DEFAULT_HEALTH_WINDOW_S,
    DEFAULT_SAMPLE_INTERVAL_S,
    load_run_config,
    parse_run_config,
)

VALID: dict[str, Any] = {
    "seed": 1,
    "run_id": "r07",
    "mode": "aaac",
    "results_dir": "results",
    "origin": {
        "service_time_ms": {"dist": "lognormal", "median": 60, "sigma": 0.5},
        "concurrency_limit": 64,
        "queue_limit": 256,
    },
}

#: The other owners' sections. M1's loader requires all of them to be present,
#: so any file-level fixture has to carry them; M3 never reads their contents.
OTHER_OWNERS_SECTIONS: dict[str, Any] = {
    "admission": {
        "w_base_s": 20.0,
        "kappa": {"HIGH": 1.0, "MEDIUM": 1.5, "LOW": 2.5},
        "w_max_s": 60.0,
        "alpha_min": 5.0,
        "alpha_max": 400.0,
        "alpha_increase": 2.0,
        "alpha_decrease": 0.7,
        "control_tick_s": 1.0,
        "target_origin_p95_ms": 400,
        "target_origin_err_rate": 0.005,
        "max_attempts": 5,
        "poll_interval_ms": 2000,
    },
    "estimator": {
        "probe_bytes": 65536,
        "min_rtt_samples": 5,
        "confidence_threshold": 0.60,
        "model_path": "models/link_classifier.joblib",
    },
    "delivery": {"budgets_bytes": {"full": 460800, "reduced": 61440, "essential": 6144}},
}

LOAD_SECTION: dict[str, Any] = {
    "n_clients": 20000,
    "scale_factor": 10,
    "burst_center_s": 30,
    "burst_sigma_s": 15,
    "tail_decay_s": 600,
    "class_mix": {"HIGH": 0.25, "MEDIUM": 0.40, "LOW": 0.35},
    "abandon_after_s": 900,
}


def write_contract_file(tmp_path: Path, **overrides: Any) -> Path:
    """A complete §3.9 run.yaml — the shape every service has to accept."""
    raw: dict[str, Any] = {
        "seed": VALID["seed"],
        "run_id": VALID["run_id"],
        "mode": VALID["mode"],
        "origin": copy.deepcopy(VALID["origin"]),
        "load": copy.deepcopy(LOAD_SECTION),
        **copy.deepcopy(OTHER_OWNERS_SECTIONS),
    }
    raw.update(overrides)
    path = tmp_path / "run.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def test_parses_a_valid_configuration() -> None:
    cfg = parse_run_config(copy.deepcopy(VALID))
    assert cfg.seed == 1
    assert cfg.run_id == "r07"
    assert cfg.mode == "aaac"
    assert cfg.origin.concurrency_limit == 64
    assert cfg.origin.queue_limit == 256
    assert cfg.origin.service_time.median_ms == 60.0
    assert cfg.origin.service_time.sigma == 0.5


def test_health_and_sampling_defaults_match_section_4_1() -> None:
    cfg = parse_run_config(copy.deepcopy(VALID))
    assert cfg.origin.health_window_s == 5.0
    assert cfg.origin.sample_interval_s == 1.0


def test_origin_events_path_is_derived_from_run_id() -> None:
    cfg = parse_run_config(copy.deepcopy(VALID))
    assert cfg.origin_events_path == Path("results/origin-r07.jsonl")


def test_a_non_lognormal_distribution_is_refused() -> None:
    # The lognormal is a modelling choice that goes in the report. Accepting a
    # different distribution silently would change the result without changing
    # the write-up.
    raw = copy.deepcopy(VALID)
    raw["origin"]["service_time_ms"]["dist"] = "exponential"
    with pytest.raises(ValueError, match="lognormal"):
        parse_run_config(raw)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("origin", "concurrency_limit"), 0),
        (("origin", "queue_limit"), -1),
        (("origin", "service_time_ms", "median"), 0),
        (("origin", "service_time_ms", "sigma"), 0),
    ],
)
def test_out_of_range_values_are_refused(path: tuple[str, ...], value: Any) -> None:
    raw = copy.deepcopy(VALID)
    target: Any = raw
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValueError):
        parse_run_config(raw)


def test_an_unknown_mode_is_refused() -> None:
    raw = copy.deepcopy(VALID)
    raw["mode"] = "adaptive"
    with pytest.raises(ValueError, match="mode"):
        parse_run_config(raw)


def test_a_missing_required_key_is_refused() -> None:
    raw = copy.deepcopy(VALID)
    del raw["origin"]["concurrency_limit"]
    with pytest.raises(ValueError, match="concurrency_limit"):
        parse_run_config(raw)


def test_environment_overrides_are_applied(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config_file = write_contract_file(tmp_path)

    monkeypatch.setenv("AAAC_SEED", "7")
    monkeypatch.setenv("AAAC_RUN_ID", "r99")
    monkeypatch.setenv("AAAC_MODE", "baseline")

    cfg = load_run_config(config_file)
    assert (cfg.seed, cfg.run_id, cfg.mode) == (7, "r99", "baseline")


def test_a_file_m1s_loader_rejects_is_refused_here_too(tmp_path: Path) -> None:
    # The admission service is the single writer of the event log, so a file it
    # refuses is a run that cannot happen. M3 fails on it at load time rather
    # than after `docker compose up`.
    config_file = write_contract_file(tmp_path, evaluation={"extra_key": 1})
    with pytest.raises(ValueError, match="M1's loader"):
        load_run_config(config_file)


def test_a_file_missing_another_owners_section_is_refused(tmp_path: Path) -> None:
    raw = yaml.safe_load(write_contract_file(tmp_path).read_text(encoding="utf-8"))
    del raw["admission"]
    path = tmp_path / "no-admission.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="M1's loader"):
        load_run_config(path)


REPO_CONFIG = Path(__file__).resolve().parents[1] / "configs" / "run.yaml"


def test_the_repository_config_loads() -> None:
    # configs/run.yaml is the single source of run parameters (§3.2). If this
    # fails, every service in the compose environment fails to start.
    cfg = load_run_config(REPO_CONFIG)
    assert cfg.origin.concurrency_limit == 64
    assert cfg.origin.queue_limit == 256
    assert cfg.origin.service_time.median_ms == 60.0


def test_repository_config_m3_sections_hold_only_contract_keys() -> None:
    # M1's loader rejects unknown keys, so an M3-only key added to `origin:` or
    # `load:` stops the admission service starting. Those parameters live as
    # defaults in aaac/origin/config.py instead.
    raw = yaml.safe_load(REPO_CONFIG.read_text(encoding="utf-8"))
    assert set(raw["origin"]) <= CONTRACT_ORIGIN_KEYS
    assert set(raw["load"]) <= CONTRACT_LOAD_KEYS
    assert "results_dir" not in raw


def test_m3_only_parameters_default_when_absent_from_the_file() -> None:
    cfg = load_run_config(REPO_CONFIG)
    assert cfg.require_load().burst_fraction == DEFAULT_BURST_FRACTION
    assert cfg.origin.health_window_s == DEFAULT_HEALTH_WINDOW_S
    assert cfg.origin.sample_interval_s == DEFAULT_SAMPLE_INTERVAL_S
