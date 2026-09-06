"""Inference for the in-band link estimator (C1).

Turns a LinkSample -- the traffic a client generated anyway while it waited in
the queue -- into a LinkEstimate that M1's admission service can act on.

TWO PROPERTIES MATTER MORE THAN ACCURACY HERE
---------------------------------------------
1. Pessimism. The decision rule is the expected-cost rule from train.py, not
   argmax. Calling a slow link fast is the exclusion this project exists to
   remove; calling a fast link slow costs someone a prettier page. The cost
   matrix travels inside the exported bundle so the rule at serve time is
   exactly the rule the model was evaluated under.

2. Liveness without the model. Nothing here may raise because a classifier is
   missing, stale, or unsure. Every such path returns MEDIUM with
   fallback=True, which degrades the system to baseline-like behaviour rather
   than breaking it. The proposal claims no component depends on classifier
   correctness for safety or liveness; classify() is where that claim is kept.

FALLBACK CONDITIONS (section 4.1), evaluated in this order:

    1. the bundle is missing, unreadable, or cannot be deserialised
    2. fewer than min_rtt_samples usable RTT samples were collected
    3. the top class probability is below confidence_threshold
    4. the bundle fails validation: its feature_names have drifted from
       FEATURE_NAMES, or it carries no cost_matrix

Conditions 1 and 4 are both decided at load time, so a bundle failing either
one behaves identically from the caller's side -- what differs is why.

Condition 4 exists because it is the worst failure mode available: nothing
raises. A bundle whose feature_names have drifted is fed the right number of
floats in the wrong order and returns confident nonsense forever. Likewise, the
decision rule is part of the model, so a bundle without its cost_matrix cannot
be served -- substituting a default would silently apply a different policy
than the one that was measured.

Config (model_path, min_rtt_samples, confidence_threshold) is deployment state,
not link state. A missing or malformed configs/run.yaml raises rather than
falling back: that is a service that cannot start, not a client that cannot be
classified.
"""

from __future__ import annotations

import statistics
import threading
from pathlib import Path
from typing import Any

import numpy as np

from aaac.common.classes import AccessClass
from aaac.common.schemas import LinkEstimate, LinkSample

from .features import FEATURE_NAMES, extract_features, stability, throughput_kbps

# --- contract -------------------------------------------------------------

#: Class returned whenever classification is unavailable or low-confidence.
#: MEDIUM by contract (section 3.3) -- never HIGH, which is the optimistic error.
FALLBACK_CLASS = AccessClass.MEDIUM

#: model_version reported when no bundle could be loaded at all. A real version
#: string is reported for conditions 2 and 3, because there the model loaded
#: fine and we chose not to trust its answer for this particular sample.
NO_MODEL_VERSION = "unavailable"

_REQUIRED_BUNDLE_KEYS = ("model", "feature_names", "model_version", "cost_matrix")


# --- bundle loading -------------------------------------------------------

_cache_lock = threading.Lock()
_cache_key: tuple[str, int, int] | None = None
_cache_bundle: dict[str, Any] | None = None


def reset_model_cache() -> None:
    """Drop the cached bundle. For tests, and for a model swapped on disk."""
    global _cache_key, _cache_bundle
    with _cache_lock:
        _cache_key = None
        _cache_bundle = None


def _validate_bundle(bundle: Any) -> dict[str, Any] | None:
    """Return the bundle if it is servable, else None. Never raises.

    Validation is not defensive boilerplate. A bundle whose feature_names have
    drifted from FEATURE_NAMES is train/serve skew: the model would be fed the
    right number of floats in the wrong order and would return confident
    nonsense. Falling back is strictly better than serving that.
    """
    if not isinstance(bundle, dict):
        return None
    if any(k not in bundle for k in _REQUIRED_BUNDLE_KEYS):
        return None
    if not hasattr(bundle["model"], "predict_proba"):
        return None
    if tuple(bundle["feature_names"]) != tuple(FEATURE_NAMES):
        return None
    try:
        cost = np.asarray(bundle["cost_matrix"], dtype=float)
    except (TypeError, ValueError):
        return None
    if cost.shape != (len(AccessClass), len(AccessClass)):
        return None
    return bundle


def load_bundle(model_path: str | Path) -> dict[str, Any] | None:
    """Load and validate the joblib bundle, or return None. Never raises.

    Cached on (path, mtime, size) so a retrained model appearing on disk
    replaces the cached one without a restart.
    """
    global _cache_key, _cache_bundle

    path = Path(model_path)
    try:
        st = path.stat()
        key = (str(path.resolve()), st.st_mtime_ns, st.st_size)
    except OSError:
        # Missing, unreadable, or a directory -- fallback condition 1.
        return None

    with _cache_lock:
        if _cache_key == key:
            return _cache_bundle

    try:
        import joblib

        loaded = joblib.load(path)
    except Exception:
        # A corrupt or version-mismatched pickle must not take the service
        # down; it is condition 1 like any other unloadable model.
        return None

    bundle = _validate_bundle(loaded)
    with _cache_lock:
        _cache_key = key
        _cache_bundle = bundle
    return bundle


# --- decision rule --------------------------------------------------------


def expected_cost_decision(proba: np.ndarray, cost_matrix: np.ndarray) -> int:
    """Class with the lowest expected cost. Mirrors train.cost_sensitive_predict.

    expected_cost[k] = sum_j P(true=j) * cost_matrix[j][k]

    Predicting HIGH while carrying even a small residual probability of LOW is
    heavily penalised, so HIGH must clear a much higher bar than LOW.
    """
    return int(np.argmin(np.asarray(proba, dtype=float) @ cost_matrix))


# --- link metrics ---------------------------------------------------------


def _usable_rtts(sample: LinkSample) -> list[float]:
    """Same filter extract_features() applies, so the two never disagree."""
    return [float(r) for r in sample.rtt_samples_ms if r is not None and r > 0]


def _link_metrics(sample: LinkSample) -> dict[str, float]:
    """The four raw signals of section 4.1, reported on every estimate.

    These are computed whether or not a model runs -- M1 and M3 get real
    measured link numbers even on a fallback, which is what makes a fallback
    estimate useful rather than merely safe.
    """
    rtts = _usable_rtts(sample)
    total = max(sample.total_requests, 1)
    return {
        "throughput_kbps": throughput_kbps(
            sample.probe_bytes, sample.probe_duration_ms
        ),
        "rtt_mean_ms": statistics.fmean(rtts) if rtts else 0.0,
        "rtt_jitter_ms": statistics.stdev(rtts) if len(rtts) >= 2 else 0.0,
        # NOTE: open question 1 -- LinkSample carries only request-level
        # failures, so this is fail_ratio under the brief's name for it. Same
        # duplication flagged in features.py. Team decision, then retrain and
        # bump model_version. Do not resolve it here.
        "loss_ratio": sample.failed_requests / total,
        "stability": stability(rtts),
    }


def _settings(
    model_path: str | Path | None,
    min_rtt_samples: int | None,
    confidence_threshold: float | None,
) -> tuple[Path, int, float]:
    """Explicit arguments win; anything left None comes from configs/run.yaml."""
    if model_path is None or min_rtt_samples is None or confidence_threshold is None:
        from aaac.common.config import get_config

        est = get_config().estimator
        if model_path is None:
            model_path = est.model_path
        if min_rtt_samples is None:
            min_rtt_samples = est.min_rtt_samples
        if confidence_threshold is None:
            confidence_threshold = est.confidence_threshold
    return Path(model_path), int(min_rtt_samples), float(confidence_threshold)


# --- main entry point -----------------------------------------------------


def classify(
    sample: LinkSample,
    *,
    model_path: str | Path | None = None,
    min_rtt_samples: int | None = None,
    confidence_threshold: float | None = None,
) -> LinkEstimate:
    """LinkSample -> LinkEstimate. Never raises on a model problem.

    `confidence` is the TOP class probability: how certain the model is about
    its read of the link. `access_class` is the POLICY decision layered on top
    of that read, and the expected-cost rule may deliberately return a class
    other than argmax. These are different layers, and the returned class may
    therefore differ from the model's most likely class.

    A sample can be read at 0.62 certainty and still be served as LOW, because
    a residual 18% chance of LOW makes a HIGH call too expensive. Reporting the
    returned class's own probability (0.18) instead would make that deliberate
    policy act look like uncertainty, and would break the comparability of
    `confidence` with `confidence_threshold` -- which sit next to each other in
    the contract (section 3.6), where someone will eventually compare them.
    """
    path, min_rtts, threshold = _settings(
        model_path, min_rtt_samples, confidence_threshold
    )
    metrics = _link_metrics(sample)
    n_rtts = len(_usable_rtts(sample))

    def _fallback(model_version: str) -> LinkEstimate:
        return LinkEstimate(
            ticket_id=sample.ticket_id,
            access_class=FALLBACK_CLASS,
            confidence=0.0,
            model_version=model_version,
            fallback=True,
            **metrics,
        )

    # Condition 1 -- no servable model.
    bundle = load_bundle(path)
    if bundle is None:
        return _fallback(NO_MODEL_VERSION)

    version = str(bundle["model_version"])

    # Condition 2 -- too little evidence to ask the model at all.
    if n_rtts < min_rtts:
        return _fallback(version)

    features = extract_features(sample).reshape(1, -1)
    try:
        proba = np.asarray(
            bundle["model"].predict_proba(features), dtype=float
        ).ravel()
    except Exception:
        # An exploding model is a broken model: same treatment as a missing one.
        return _fallback(version)

    if proba.shape != (len(AccessClass),) or not np.isfinite(proba).all():
        return _fallback(version)

    # Condition 3 -- the model is not confident enough to be trusted.
    if float(proba.max()) < threshold:
        return _fallback(version)

    cost_matrix = np.asarray(bundle["cost_matrix"], dtype=float)
    choice = expected_cost_decision(proba, cost_matrix)

    return LinkEstimate(
        ticket_id=sample.ticket_id,
        access_class=AccessClass(choice),
        # The model's certainty about the link, not about the policy call.
        confidence=float(proba.max()),
        model_version=version,
        fallback=False,
        **metrics,
    )
