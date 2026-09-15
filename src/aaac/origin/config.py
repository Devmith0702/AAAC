# ---------------------------------------------------------------------------
# M3's view of the run configuration. NOT a stub any more.
#
# This module was a stub standing in for `common/config.py` until M1 shipped it.
# M1's loader has now landed and is the authority on every key in CLAUDE.md
# §3.9: `load_run_config()` parses through it, so a file this module accepts is
# a file the admission service accepts. What survives here is only what M1's
# config cannot express, and all of it is M3-owned:
#
#   - the origin health window / bucket count / sample interval (§4.1)
#   - `burst_fraction`, the Gaussian/exponential split of arrivals (§4.3)
#   - `results_dir` and the derived origin event-log path
#   - the environment overrides the Compose testbed parameterises runs with
#   - range checks on the shared values (lognormal-only, positive, in-bounds)
#
# Those are deliberately absent from configs/run.yaml: M1's loader rejects any
# key outside §3.9, so putting them in the file stops the admission service
# starting. See INTEGRATION-ISSUES.md A3.
# ---------------------------------------------------------------------------
"""Run configuration for the M3-owned sections, layered over M1's loader."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from aaac.common import config as common_config

DEFAULT_CONFIG_PATH = Path("configs/run.yaml")

VALID_MODES = ("none", "baseline", "aaac")

# Parameters M3 needs that are deliberately NOT in configs/run.yaml (see the
# banner). They live here as named defaults; the parser still honours the key if
# an agreed section for them is added to run.yaml later.
DEFAULT_HEALTH_WINDOW_S = 5.0  # §4.1: "rolling 5 s window"
DEFAULT_HEALTH_BUCKETS = 5
DEFAULT_SAMPLE_INTERVAL_S = 1.0  # §4.1: "ORIGIN_SAMPLE once per second"
DEFAULT_BURST_FRACTION = 0.8  # not specified by §4.3; written into every population file
DEFAULT_RESULTS_DIR = "results"  # override with AAAC_RESULTS_DIR, which M1 also reads

#: The exact §3.9 keys of the two M3-owned sections of configs/run.yaml.
CONTRACT_ORIGIN_KEYS = frozenset({"service_time_ms", "concurrency_limit", "queue_limit"})
CONTRACT_LOAD_KEYS = frozenset(
    {
        "n_clients",
        "scale_factor",
        "burst_center_s",
        "burst_sigma_s",
        "tail_decay_s",
        "class_mix",
        "abandon_after_s",
    }
)


@dataclass(frozen=True)
class ServiceTimeConfig:
    """Distribution of simulated origin service time (CLAUDE.md §3.9)."""

    dist: str
    median_ms: float
    sigma: float


@dataclass(frozen=True)
class LoadConfig:
    """Client population and arrival process (CLAUDE.md §3.9, §4.3)."""

    n_clients: int
    scale_factor: int
    burst_center_s: float
    burst_sigma_s: float
    tail_decay_s: float
    class_mix: dict[str, float]
    abandon_after_s: float
    burst_fraction: float
    """ADDED by M3. §4.3 specifies "a Gaussian burst ... plus an exponential
    tail" but not their relative weight, which a mixture needs. Defaults to
    ``DEFAULT_BURST_FRACTION`` (not a run.yaml key — see the note on the
    defaults above) and is written into every population file, so the split
    stays a stated parameter of each run."""


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
    load: LoadConfig | None = None

    def require_load(self) -> LoadConfig:
        """The ``load:`` section, or a clear error naming what is missing."""
        if self.load is None:
            raise ValueError(
                "configs/run.yaml has no `load:` section; it is required for "
                "population generation and load generation (CLAUDE.md §3.9)"
            )
        return self.load

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

    health_window_s = float(raw.get("health_window_s", DEFAULT_HEALTH_WINDOW_S))
    health_buckets = int(raw.get("health_buckets", DEFAULT_HEALTH_BUCKETS))
    sample_interval_s = float(raw.get("sample_interval_s", DEFAULT_SAMPLE_INTERVAL_S))
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


CLASS_MIX_KEYS = ("HIGH", "MEDIUM", "LOW")


def _parse_load(raw: Any) -> LoadConfig:
    where = "load"
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: expected a mapping, got {type(raw).__name__}")

    n_clients = int(_require(raw, "n_clients", where))
    if n_clients < 1:
        raise ValueError(f"{where}.n_clients: must be >= 1, got {n_clients}")

    mix_raw = _require(raw, "class_mix", where)
    if not isinstance(mix_raw, dict) or set(mix_raw) != set(CLASS_MIX_KEYS):
        raise ValueError(f"{where}.class_mix: must have exactly the keys {CLASS_MIX_KEYS}")
    class_mix = {key: float(mix_raw[key]) for key in CLASS_MIX_KEYS}
    if any(v < 0 for v in class_mix.values()):
        raise ValueError(f"{where}.class_mix: shares must be >= 0")
    total = sum(class_mix.values())
    if abs(total - 1.0) > 1e-6:
        raise ValueError(f"{where}.class_mix: shares must sum to 1.0, got {total}")

    burst_sigma_s = float(_require(raw, "burst_sigma_s", where))
    tail_decay_s = float(_require(raw, "tail_decay_s", where))
    if burst_sigma_s <= 0:
        raise ValueError(f"{where}.burst_sigma_s: must be > 0, got {burst_sigma_s}")
    if tail_decay_s <= 0:
        raise ValueError(f"{where}.tail_decay_s: must be > 0, got {tail_decay_s}")

    burst_fraction = float(raw.get("burst_fraction", DEFAULT_BURST_FRACTION))
    if not 0.0 <= burst_fraction <= 1.0:
        raise ValueError(f"{where}.burst_fraction: must be in [0, 1], got {burst_fraction}")

    scale_factor = int(raw.get("scale_factor", 1))
    if scale_factor < 1:
        raise ValueError(f"{where}.scale_factor: must be >= 1, got {scale_factor}")

    return LoadConfig(
        n_clients=n_clients,
        scale_factor=scale_factor,
        burst_center_s=float(_require(raw, "burst_center_s", where)),
        burst_sigma_s=burst_sigma_s,
        tail_decay_s=tail_decay_s,
        class_mix=class_mix,
        abandon_after_s=float(_require(raw, "abandon_after_s", where)),
        burst_fraction=burst_fraction,
    )


def parse_run_config(raw: dict[str, Any]) -> RunConfig:
    """Build a :class:`RunConfig` from an already-parsed YAML mapping.

    This is M3's own parsing of the M3-owned sections. It does **not** apply
    M1's whole-file validation — :func:`load_run_config` does that, because only
    a real file can be checked against the contract as a whole.
    """
    mode = str(raw.get("mode", "none"))
    if mode not in VALID_MODES:
        raise ValueError(f"mode: must be one of {VALID_MODES}, got {mode!r}")

    return RunConfig(
        seed=int(_require(raw, "seed", "<root>")),
        run_id=str(raw.get("run_id", "dev")),
        mode=mode,
        results_dir=Path(str(raw.get("results_dir", DEFAULT_RESULTS_DIR))),
        origin=_parse_origin(_require(raw, "origin", "<root>")),
        load=_parse_load(raw["load"]) if "load" in raw else None,
    )


def _check_against_m1_loader(resolved: Path) -> None:
    """Fail here if M1's loader would reject this file.

    The admission service is the single writer of the event log, so a file it
    refuses is a run that cannot happen. Finding that out when the origin starts
    — rather than after `docker compose up` — is the whole point of this check.

    M1's loader exposes no public path-taking entry point (``get_config()``
    reads ``AAAC_CONFIG_PATH`` and caches a module-level singleton, which would
    make the check depend on call order), so the module-private loader is used
    deliberately. A public ``load_config(path)`` on their side would replace
    this; raised as A6 in INTEGRATION-ISSUES.md.
    """
    try:
        common_config._load_config_from_file(str(resolved))
    except Exception as exc:  # noqa: BLE001 — re-raised with context below
        raise ValueError(
            f"{resolved}: rejected by M1's loader (aaac.common.config), so the "
            f"admission service would refuse to start: {exc}"
        ) from exc


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

    _check_against_m1_loader(resolved)

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
