"""Feature extraction for the in-band link estimator (C1).

The feature ORDER defined here is a contract. The trained model learned
position-by-position, so reordering, inserting or removing a feature silently
invalidates every saved model. Any change here must be mirrored in
models/MODEL_CARD.md and force a model_version bump.

Everything is derived from traffic the client generates anyway while it waits
in the queue: one timed probe download and the ordinary /queue/status polls.
No client-declared values, no user agent, no IP heuristics, no personal data.
"""

from __future__ import annotations

import math
import statistics

import numpy as np

from aaac.common.schemas import LinkSample

# --- contract -------------------------------------------------------------

FEATURE_NAMES: tuple[str, ...] = (
    "log10_throughput_kbps",
    "rtt_mean_ms",
    "rtt_p95_ms",
    "rtt_jitter_ms",
    "fail_ratio",
    "stability",
    "n_rtt_samples",
)

N_FEATURES = len(FEATURE_NAMES)


# --- primitives -----------------------------------------------------------


def throughput_kbps(probe_bytes: int, probe_duration_ms: float) -> float:
    """Kilobits per second from one timed probe download.

    The probe payload is incompressible random bytes, so gzip cannot shrink it
    in transit and inflate the measurement.
    """
    if probe_duration_ms <= 0:
        return 0.0
    return (probe_bytes * 8.0) / probe_duration_ms


def percentile(values: list[float], q: float) -> float:
    """Linear-interpolated percentile. q in 0..100."""
    if not values:
        return 0.0
    return float(np.percentile(np.asarray(values, dtype=float), q))


def stability(rtt_samples_ms: list[float]) -> float:
    """1 - coefficient of variation, clamped to 0..1.

    A link whose RTT swings between 190 ms and 450 ms is materially worse than
    one that sits steadily at 300 ms, even though the means are similar.
    Dividing the spread by the mean makes the score scale-free so it is
    comparable across a 15 ms fibre link and a 300 ms cell link.
    """
    if len(rtt_samples_ms) < 2:
        return 0.0
    mean = statistics.fmean(rtt_samples_ms)
    if mean <= 0:
        return 0.0
    cv = statistics.stdev(rtt_samples_ms) / mean
    return float(max(0.0, min(1.0, 1.0 - cv)))


# --- main entry point -----------------------------------------------------


def extract_features(sample: LinkSample) -> np.ndarray:
    """LinkSample -> float32 vector of length N_FEATURES, in FEATURE_NAMES order.

    Used by BOTH training and inference. Routing every path through this one
    function is what prevents train/serve skew: if the synthetic generator and
    the live probe disagree about how jitter is computed, the model silently
    degrades in production and nothing fails loudly.
    """
    rtts = [float(r) for r in sample.rtt_samples_ms if r is not None and r > 0]
    n = len(rtts)

    tput = throughput_kbps(sample.probe_bytes, sample.probe_duration_ms)
    # +1 keeps log10 finite when the probe failed outright (tput == 0).
    log_tput = math.log10(tput + 1.0)

    rtt_mean = statistics.fmean(rtts) if n else 0.0
    rtt_p95 = percentile(rtts, 95.0) if n else 0.0
    rtt_jitter = statistics.stdev(rtts) if n >= 2 else 0.0

    total = max(sample.total_requests, 1)
    fail_ratio = sample.failed_requests / total

    # DECISION (2026-09-06, C1 owner) -- open question 1, resolved.
    # The vector carries fail_ratio only; loss_ratio was dropped, taking the
    # feature count from 8 to 7.
    #
    # The brief listed loss_ratio and fail_ratio as separate features, but
    # LinkSample carries only failed_requests / total_requests, which is the
    # brief's own definition of loss_ratio -- so the two columns held the same
    # number. Two identical columns give a tree model nothing and split
    # feature importance between duplicates, which understates how much loss
    # actually matters. No honest packet-loss proxy can be derived from
    # LinkSample's fields, and defending a removal is easier than defending a
    # fabricated derivation.
    #
    # This does NOT touch LinkEstimate.loss_ratio (section 3.6), which is a
    # reported field and is still populated by infer.py from the same ratio.
    # The shared contract is unaffected; this is the internal feature vector.

    return np.array(
        [
            log_tput,
            rtt_mean,
            rtt_p95,
            rtt_jitter,
            fail_ratio,
            stability(rtts),
            float(n),
        ],
        dtype=np.float32,
    )


def features_to_dict(vec: np.ndarray) -> dict[str, float]:
    """Named view of a feature vector, for logging and debugging."""
    return {name: float(v) for name, v in zip(FEATURE_NAMES, vec, strict=True)}
