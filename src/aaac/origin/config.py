# ---------------------------------------------------------------------------
# STUB — DELETE THIS MODULE WHEN M1 SHIPS src/aaac/common/config.py
#
# CLAUDE.md §1.4 permits a clearly-marked local stub inside my own package so
# that the origin service is not blocked on M1. It parses only the top-level run
# keys and the `origin:` section, both of which M3 owns. When common/config.py
# lands, delete this file and import the real loader instead; nothing here is
# meant to survive.
# ---------------------------------------------------------------------------
"""Run-configuration loading for the mock origin (local stub)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path("configs/run.yaml")

VALID_MODES = ("none", "baseline", "aaac")


@dataclass(frozen=True)
class ServiceTimeConfig:
    """Distribution of simulated origin service time (CLAUDE.md §3.9)."""

    dist: str
    median_ms: float
    sigma: float


@dataclass(frozen=True)
class OriginConfig:
    service_time: ServiceTimeConfig
    concurrency_limit: int
    queue_limit: int
    health_window_s: float
    health_buckets: int
    sample_interval_s: float


@dataclass(frozen=True)
class RunConfig:
    seed: int
    run_id: str
    mode: str
    results_dir: Path
    origin: OriginConfig

    @property
    def origin_events_path(self) -> Path:
        """Where ORIGIN_SAMPLE events are written.

        A separate file from the admission service's event log — see the
        resolution of CLAUDE.md §5.2 open question 1 in ``README.md``.
        """
        return self.results_dir / f"origin-{self.run_id}.jsonl"


def _require(section: dict[str, Any], key: str, where: str) -> Any:
    if key not in section:
        raise ValueError(f"{where}: missing required key '{key}'")
    return section[key]


def _parse_service_time(raw: Any) -> ServiceTimeConfig:
    where = "origin.service_time_ms"
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: expected a mapping, got {type(raw).__name__}")

    dist = str(_require(raw, "dist", where))
    if dist != "lognormal":
        # Deliberately narrow. The lognormal is a modelling choice that goes in
        # the report (CLAUDE.md §4.1); silently accepting another distribution
        # would change the result without changing the write-up.
        raise ValueError(f"{where}.dist: only 'lognormal' is supported, got {dist!r}")

    median_ms = float(_require(raw, "median", where))
    sigma = float(_require(raw, "sigma", where))
    if median_ms <= 0:
        raise ValueError(f"{where}.median: must be > 0, got {median_ms}")
    if sigma <= 0:
        raise ValueError(f"{where}.sigma: must be > 0, got {sigma}")
    return ServiceTimeConfig(dist=dist, median_ms=median_ms, sigma=sigma)


def _parse_origin(raw: Any) -> OriginConfig:
    where = "origin"
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: expected a mapping, got {type(raw).__name__}")

    concurrency_limit = int(_require(raw, "concurrency_limit", where))
    queue_limit = int(_require(raw, "queue_limit", where))
    if concurrency_limit < 1:
        raise ValueError(f"{where}.concurrency_limit: must be >= 1, got {concurrency_limit}")
    if queue_limit < 0:
        raise ValueError(f"{where}.queue_limit: must be >= 0, got {queue_limit}")

    health_window_s = float(raw.get("health_window_s", 5.0))
    health_buckets = int(raw.get("health_buckets", 5))
    sample_interval_s = float(raw.get("sample_interval_s", 1.0))
    if health_window_s <= 0:
        raise ValueError(f"{where}.health_window_s: must be > 0, got {health_window_s}")
    if health_buckets < 1:
        raise ValueError(f"{where}.health_buckets: must be >= 1, got {health_buckets}")
    if sample_interval_s <= 0:
        raise ValueError(f"{where}.sample_interval_s: must be > 0, got {sample_interval_s}")

    return OriginConfig(
        service_time=_parse_service_time(_require(raw, "service_time_ms", where)),
        concurrency_limit=concurrency_limit,
        queue_limit=queue_limit,
        health_window_s=health_window_s,
        health_buckets=health_buckets,
        sample_interval_s=sample_interval_s,
    )


def parse_run_config(raw: dict[str, Any]) -> RunConfig:
    """Build a :class:`RunConfig` from an already-parsed YAML mapping."""
    mode = str(raw.get("mode", "none"))
    if mode not in VALID_MODES:
        raise ValueError(f"mode: must be one of {VALID_MODES}, got {mode!r}")

    return RunConfig(
        seed=int(_require(raw, "seed", "<root>")),
        run_id=str(raw.get("run_id", "dev")),
        mode=mode,
        results_dir=Path(str(raw.get("results_dir", "results"))),
        origin=_parse_origin(_require(raw, "origin", "<root>")),
    )


def load_run_config(path: str | os.PathLike[str] | None = None) -> RunConfig:
    """Load ``configs/run.yaml``, applying environment overrides.

    Overrides (used by the Compose testbed so a run can be parameterised without
    rewriting the config file): ``AAAC_CONFIG``, ``AAAC_SEED``, ``AAAC_RUN_ID``,
    ``AAAC_MODE``, ``AAAC_RESULTS_DIR``.
    """
    if path is not None:
        resolved = Path(path)
    else:
        resolved = Path(os.environ.get("AAAC_CONFIG", DEFAULT_CONFIG_PATH))
    with resolved.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if not isinstance(raw, dict):
        raise ValueError(f"{resolved}: expected a YAML mapping at the top level")

    for env_key, cfg_key in (
        ("AAAC_SEED", "seed"),
        ("AAAC_RUN_ID", "run_id"),
        ("AAAC_MODE", "mode"),
        ("AAAC_RESULTS_DIR", "results_dir"),
    ):
        if env_key in os.environ:
            raw[cfg_key] = os.environ[env_key]

    return parse_run_config(raw)
