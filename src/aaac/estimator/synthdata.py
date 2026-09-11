"""Synthetic training data for the link estimator.

Purpose: unblock classifier work in week 1, before M3's netem testbed can
supply measured traces. Synthetic accuracy is a development signal only. The
number reported in the write-up must come from measured data.

THE DESIGN POINT -- READ BEFORE CHANGING ANY CONSTANT
----------------------------------------------------
It is trivial to generate three perfectly separable clusters and score 100%.
That number would be meaningless: it would measure the generator, not the
model. Real links are ambiguous -- a poor LTE connection and a good rural
connection genuinely look alike -- so adjacent classes here are deliberately
placed close enough in log-space to overlap at the tails. CLASS_SEP_DECADES
and the per-class sigmas below control exactly how much. Widening them to make
accuracy look better is self-deception; narrowing them makes the task harder
than reality. Report whatever separability you chose.

Samples are generated as raw LinkSample objects and then passed through the
same extract_features() used at inference time, so the training features
cannot drift from the production ones.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from aaac.common.classes import AccessClass
from aaac.common.schemas import LinkSample

from .features import FEATURE_NAMES, extract_features


@dataclass(frozen=True)
class LinkProfile:
    """Distribution parameters for one access class.

    Throughput and RTT are lognormal because network rates and latencies are
    multiplicative in nature: doubling is a more natural unit of variation than
    adding a fixed number of kbps.
    """

    name: str
    log_tput_mean: float      # mean of log10(kbps)
    log_tput_sigma: float     # spread in decades -- the overlap knob
    rtt_median_ms: float
    rtt_log_sigma: float
    jitter_frac: float        # per-sample RTT wobble, as a fraction of median
    base_fail_rate: float     # probability a poll hard-fails
    poll_count_mean: float    # how many status polls before admission


# Separation between adjacent class centres, in decades of throughput.
# 0.65 means each class is ~4.5x faster than the one below it. With sigma
# ~0.40 the adjacent distributions overlap substantially, which is the intent.
# Raising this makes the problem artificially easy.
CLASS_SEP_DECADES = 0.65

PROFILES: dict[AccessClass, LinkProfile] = {
    AccessClass.HIGH: LinkProfile(
        name="HIGH",
        log_tput_mean=4.30,          # ~20 Mbps median
        log_tput_sigma=0.40,
        rtt_median_ms=22.0,
        rtt_log_sigma=0.50,
        jitter_frac=0.12,
        base_fail_rate=0.002,
        poll_count_mean=14.0,
    ),
    AccessClass.MEDIUM: LinkProfile(
        name="MEDIUM",
        log_tput_mean=4.30 - CLASS_SEP_DECADES,   # ~3.2 Mbps median
        log_tput_sigma=0.42,
        rtt_median_ms=70.0,
        rtt_log_sigma=0.60,
        jitter_frac=0.30,
        base_fail_rate=0.015,
        poll_count_mean=20.0,
    ),
    AccessClass.LOW: LinkProfile(
        name="LOW",
        log_tput_mean=4.30 - 2 * CLASS_SEP_DECADES,  # ~500 kbps median
        log_tput_sigma=0.45,
        rtt_median_ms=210.0,
        rtt_log_sigma=0.65,
        jitter_frac=0.48,
        base_fail_rate=0.055,
        poll_count_mean=28.0,      # slower clients wait longer, so poll more
    ),
}

PROBE_BYTES = 65536  # configs/run.yaml -> estimator.probe_bytes

# Fraction of clients that get an unusually short observation window: they were
# admitted quickly, or the probe landed badly, so only a handful of RTT samples
# exist. These rows are what the confidence threshold and the fallback path
# have to cope with in production, so the model must see them in training.
SHORT_OBSERVATION_FRAC = 0.12

# Fraction of clients whose bandwidth and latency characteristics disagree.
#
# This is the single most important realism knob in the file. In an earlier
# version every LOW client was uniformly bad -- slow AND laggy AND jittery AND
# failing -- so the eight features all pointed the same way and the model hit
# 98% on synthetic data. That number measured the generator, not the model.
#
# Real links are not uniformly bad. Satellite gives decent bandwidth with
# terrible latency. A throttled office connection is slow but rock steady. A
# good rural cell has fine latency until the tower congests. Those mixed cases
# are exactly where classification is hard and where the cost of guessing HIGH
# is paid. The model has to see them in training or it will meet them for the
# first time in production.
MIXED_CHARACTER_FRAC = 0.30

# --- probe failure --------------------------------------------------------
#
# Added after the harness met a real network. The generator previously produced
# only *slow* probes, never *failed* ones: throughput was floored at 30 kbps and
# every probe returned its full 64 KB. Reality does not work that way, and the
# consequence was a measurement gap rather than a modelling nicety.
#
# Fallback condition 5 (input outside training support) fired on 0.10% of
# synthetic test rows and on the very FIRST run against real services. So the
# fallback rate -- which decides whether the classifier does any work at all
# during evaluation -- was not measurable from synthetic data. If it turns out
# high, Delta is measured over a mostly-unclassified population and the headline
# result describes the fallback path rather than C1.
#
# These fractions are conditional on class, because failure is not uniform: a
# 3%-loss rural cell drops probes, urban fibre does not.

#: Probe returns some bytes and then stalls. Reported as the low throughput it
#: really is (client/probe.py) -- a signal, not an absence.
PROBE_STALL_FRAC = {AccessClass.HIGH: 0.005, AccessClass.MEDIUM: 0.03,
                    AccessClass.LOW: 0.09}

#: Probe returns nothing at all: reset, timeout, refused. There is no
#: bytes/duration pair that expresses this, so probe_bytes is 0 and the failure
#: is carried by failed_requests.
PROBE_ZERO_FRAC = {AccessClass.HIGH: 0.002, AccessClass.MEDIUM: 0.012,
                   AccessClass.LOW: 0.04}

# DEGENERATE PROBES ARE DELIBERATELY *NOT* GENERATED. Do not add them.
#
# A first version of this change did model them, as a HIGH-class behaviour --
# plausible, since only a fast link delivers 64 KB in a single read. Retraining
# showed why it is wrong: it moved the top of log10_throughput support from
# 5.905 to 8.7196, exactly the loopback value. Condition 5 stopped catching the
# degenerate case, and the model instead learned "untimeable probe -> HIGH",
# which is precisely the optimistic error the whole component exists to prevent.
#
# The distinction that matters:
#
#   a FAILED probe    is a signal ABOUT THE LINK      -> model it, learn from it
#   a DEGENERATE probe is an ABSENCE OF MEASUREMENT   -> abstain, never infer
#
# Teaching a model to infer bandwidth from a failure to measure bandwidth is
# circular, and the harness proved the inference can be flatly wrong: loopback
# is not a "fast link" in any sense that matters to a student. Leaving
# degeneracy outside support is what makes classify() fall back to MEDIUM,
# which is the cheap direction.

#: A poll that never returns contributes no RTT sample but does count as a
#: failed request, so a bad link can end up with few samples AND a high
#: fail_ratio -- the combination the confidence threshold has to cope with.
POLL_STALL_EXTRA = {AccessClass.HIGH: 0.0, AccessClass.MEDIUM: 0.01,
                    AccessClass.LOW: 0.05}




def _simulate_sample(
    rng: np.random.Generator,
    cls: AccessClass,
    ticket_id: str,
) -> LinkSample:
    """Simulate one client's observable behaviour while it sits in the queue."""
    p = PROFILES[cls]

    # A minority of links borrow their latency character from a neighbouring
    # class while keeping their own bandwidth. The label stays the true class
    # (in the testbed the label is whatever netem was configured with), so
    # these rows are genuinely ambiguous rather than mislabelled.
    lat_p = p
    if rng.random() < MIXED_CHARACTER_FRAC:
        neighbours = [c for c in AccessClass if c != cls]
        lat_p = PROFILES[AccessClass(int(rng.choice([int(c) for c in neighbours])))]

    # --- throughput: one timed probe download ---------------------------
    tput_kbps = float(10 ** rng.normal(p.log_tput_mean, p.log_tput_sigma))
    tput_kbps = max(tput_kbps, 30.0)          # floor: nothing is truly 0
    probe_duration_ms = (PROBE_BYTES * 8.0) / tput_kbps

    # TODO before the measured-trace retrain: model probe FAILURE, not just
    # slowness. Throughput is floored at 30 kbps here, so a live probe that
    # returns zero bytes lands just below anything the model was trained on.
    # The live client clamps to this floor for that reason (client/probe.py),
    # and the zero-byte case is reported honestly and sits outside support.
    #
    # A congested link sometimes stalls mid-transfer. Occasional heavy-tailed
    # inflation, more common on worse links, keeps the probe honest.
    if rng.random() < p.base_fail_rate * 4:
        probe_duration_ms *= float(rng.uniform(1.5, 4.0))

    # --- probe failure modes, as the live client would report them ------
    probe_bytes = PROBE_BYTES
    probe_failed = False
    roll = rng.random()

    if roll < PROBE_ZERO_FRAC[cls]:
        # Nothing arrived. The client reports 0 bytes honestly rather than
        # inventing a pair, so this lands just below training support.
        probe_bytes = 0
        probe_duration_ms = 0.0
        probe_failed = True
    elif roll < PROBE_ZERO_FRAC[cls] + PROBE_STALL_FRAC[cls]:
        # Part of the payload arrived, then the transfer died. The client
        # reports bytes_received over the time they took.
        fraction = float(rng.uniform(0.05, 0.6))
        probe_bytes = max(1, int(PROBE_BYTES * fraction))
        probe_duration_ms *= fraction * float(rng.uniform(1.2, 3.0))
        probe_failed = True


    # --- RTT: timing of the ordinary /queue/status polls -----------------
    n_polls = int(max(2, rng.poisson(p.poll_count_mean)))
    if rng.random() < SHORT_OBSERVATION_FRAC:
        n_polls = int(rng.integers(2, 7))

    base_rtt = float(rng.lognormal(np.log(lat_p.rtt_median_ms), lat_p.rtt_log_sigma))
    jitter_sd = base_rtt * lat_p.jitter_frac
    rtts = rng.normal(base_rtt, jitter_sd, size=n_polls)

    # Bufferbloat: a minority of polls on a congested link queue up badly.
    spike_mask = rng.random(n_polls) < (p.base_fail_rate * 3)
    rtts[spike_mask] *= rng.uniform(2.0, 5.0, size=int(spike_mask.sum()))
    rtts = np.clip(rtts, 1.0, None)

    # --- failures: timeouts, resets, 5xx on polls ------------------------
    fail_rate = float(np.clip(rng.normal(p.base_fail_rate, p.base_fail_rate * 0.6), 0.0, 0.6))
    total_requests = n_polls + 1                       # polls plus the probe
    failed_requests = int(rng.binomial(total_requests, fail_rate))

    # Polls that hang: no timing sample, but still a counted failure. This is
    # how a bad link ends up with few RTT samples AND a high fail_ratio.
    stalled_polls = int(rng.binomial(n_polls, POLL_STALL_EXTRA[cls]))
    failed_requests = min(total_requests, failed_requests + stalled_polls)
    if probe_failed:
        failed_requests = min(total_requests, failed_requests + 1)

    # A failed poll produces no timing sample.
    kept = max(2, n_polls - failed_requests)
    rtt_samples = [float(x) for x in rtts[:kept]]

    return LinkSample(
        ticket_id=ticket_id,
        probe_bytes=probe_bytes,
        probe_duration_ms=float(probe_duration_ms),
        rtt_samples_ms=rtt_samples,
        failed_requests=failed_requests,
        total_requests=total_requests,
    )


def generate(
    n: int = 6000,
    class_mix: dict[AccessClass, float] | None = None,
    seed: int = 1,
) -> tuple[np.ndarray, np.ndarray, list[LinkSample]]:
    """Generate a labelled synthetic dataset.

    Returns (X, y, raw) where X is (n, 8) float32 in FEATURE_NAMES order,
    y is (n,) int labels matching AccessClass, and raw holds the underlying
    samples for inspection. Deterministic for a given seed, per contract rule 5.
    """
    mix = class_mix or {
        AccessClass.HIGH: 0.25,
        AccessClass.MEDIUM: 0.40,
        AccessClass.LOW: 0.35,
    }
    rng = np.random.default_rng(seed)

    classes = list(mix.keys())
    probs = np.array([mix[c] for c in classes], dtype=float)
    probs /= probs.sum()
    labels = rng.choice([int(c) for c in classes], size=n, p=probs)

    raw: list[LinkSample] = []
    rows = np.empty((n, len(FEATURE_NAMES)), dtype=np.float32)
    for i, lab in enumerate(labels):
        sample = _simulate_sample(rng, AccessClass(int(lab)), f"synth-{i:06d}")
        raw.append(sample)
        rows[i] = extract_features(sample)

    return rows, labels.astype(np.int64), raw


if __name__ == "__main__":
    import pandas as pd

    X, y, _ = generate(n=6000, seed=1)
    df = pd.DataFrame(X, columns=list(FEATURE_NAMES))
    df["label"] = [AccessClass(int(v)).name for v in y]

    pd.set_option("display.width", 140)
    pd.set_option("display.float_format", lambda v: f"{v:9.3f}")
    print(f"rows={len(df)}\n")
    print(df.groupby("label")[list(FEATURE_NAMES)].median(), "\n")

    # Overlap check: what fraction of each class falls inside the throughput
    # range of the neighbouring class? Zero means the task is too easy.
    print("throughput overlap between adjacent classes")
    for lo, hi in ((AccessClass.LOW, AccessClass.MEDIUM),
                   (AccessClass.MEDIUM, AccessClass.HIGH)):
        a = X[y == int(lo), 0]
        b = X[y == int(hi), 0]
        boundary = (np.median(a) + np.median(b)) / 2
        wrong_side = ((a > boundary).sum() + (b < boundary).sum()) / (len(a) + len(b))
        print(f"  {lo.name:<6} vs {hi.name:<6}  {wrong_side:6.1%} sit on the wrong side")
