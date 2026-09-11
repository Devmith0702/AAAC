"""Config loading (local stub — see the banner in ``aaac/origin/config.py``)."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

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
    import yaml

    config_file = tmp_path / "run.yaml"
    config_file.write_text(yaml.safe_dump(VALID), encoding="utf-8")

    monkeypatch.setenv("AAAC_SEED", "7")
    monkeypatch.setenv("AAAC_RUN_ID", "r99")
    monkeypatch.setenv("AAAC_MODE", "baseline")

    cfg = load_run_config(config_file)
    assert (cfg.seed, cfg.run_id, cfg.mode) == (7, "r99", "baseline")


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
    import yaml

    raw = yaml.safe_load(REPO_CONFIG.read_text(encoding="utf-8"))
    assert set(raw["origin"]) <= CONTRACT_ORIGIN_KEYS
    assert set(raw["load"]) <= CONTRACT_LOAD_KEYS
    assert "results_dir" not in raw


def test_m3_only_parameters_default_when_absent_from_the_file() -> None:
    cfg = load_run_config(REPO_CONFIG)
    assert cfg.require_load().burst_fraction == DEFAULT_BURST_FRACTION
    assert cfg.origin.health_window_s == DEFAULT_HEALTH_WINDOW_S
    assert cfg.origin.sample_interval_s == DEFAULT_SAMPLE_INTERVAL_S
