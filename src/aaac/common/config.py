from __future__ import annotations
import os
import yaml
from dataclasses import dataclass
from typing import Literal

@dataclass(frozen=True)
class AdmissionConfig:
    w_base_s: float
    kappa: dict[str, float]
    w_max_s: float
    alpha_min: float
    alpha_max: float
    alpha_increase: float
    alpha_decrease: float
    control_tick_s: float
    target_origin_p95_ms: float
    target_origin_err_rate: float
    max_attempts: int
    poll_interval_ms: int

@dataclass(frozen=True)
class EstimatorConfig:
    probe_bytes: int
    min_rtt_samples: int
    confidence_threshold: float
    model_path: str

@dataclass(frozen=True)
class DeliveryConfig:
    budgets_bytes: dict[str, int]

@dataclass(frozen=True)
class OriginConfig:
    service_time_ms: dict[str, str | float]
    concurrency_limit: int
    queue_limit: int

@dataclass(frozen=True)
class LoadConfig:
    n_clients: int
    scale_factor: int
    burst_center_s: float
    burst_sigma_s: float
    tail_decay_s: float
    class_mix: dict[str, float]
    abandon_after_s: float

@dataclass(frozen=True)
class RunConfig:
    run_id: str
    mode: Literal["none", "baseline", "aaac"]
    seed: int
    admission: AdmissionConfig
    estimator: EstimatorConfig
    delivery: DeliveryConfig
    origin: OriginConfig
    load: LoadConfig

def _validate_keys(data: dict, allowed: set, path: str):
    unknown = set(data.keys()) - allowed
    if unknown:
        raise ValueError(f"Unknown config keys in {path}: {unknown}")

def _load_config_from_file(filepath: str) -> RunConfig:
    with open(filepath, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
        
    _validate_keys(data, {"run_id", "mode", "seed", "admission", "estimator", "delivery", "origin", "load"}, "root")
    
    _validate_keys(data["admission"], {
        "w_base_s", "kappa", "w_max_s", "alpha_min", "alpha_max", 
        "alpha_increase", "alpha_decrease", "control_tick_s", 
        "target_origin_p95_ms", "target_origin_err_rate", 
        "max_attempts", "poll_interval_ms"
    }, "admission")
    
    _validate_keys(data["estimator"], {
        "probe_bytes", "min_rtt_samples", "confidence_threshold", "model_path"
    }, "estimator")
    
    _validate_keys(data["delivery"], {"budgets_bytes"}, "delivery")
    
    _validate_keys(data["origin"], {
        "service_time_ms", "concurrency_limit", "queue_limit"
    }, "origin")
    
    _validate_keys(data["load"], {
        "n_clients", "scale_factor", "burst_center_s", "burst_sigma_s", 
        "tail_decay_s", "class_mix", "abandon_after_s"
    }, "load")

    return RunConfig(
        run_id=data["run_id"],
        mode=data["mode"],
        seed=data["seed"],
        admission=AdmissionConfig(**data["admission"]),
        estimator=EstimatorConfig(**data["estimator"]),
        delivery=DeliveryConfig(**data["delivery"]),
        origin=OriginConfig(**data["origin"]),
        load=LoadConfig(**data["load"])
    )

_CONFIG_CACHE: RunConfig | None = None

def get_config() -> RunConfig:
    global _CONFIG_CACHE
    if _CONFIG_CACHE is None:
        path = os.environ.get("AAAC_CONFIG_PATH", "configs/run.yaml")
        _CONFIG_CACHE = _load_config_from_file(path)
    return _CONFIG_CACHE

def reset_config() -> None:
    """Clear the cached config singleton. For use in tests only."""
    global _CONFIG_CACHE
    _CONFIG_CACHE = None
