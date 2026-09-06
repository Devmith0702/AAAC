"""Fallback condition 5: input outside the model's training support.

The finding this exists for: on first contact with a real network the probe
finished too fast to time, reported 524,288,000 kbps, and classify() returned
HIGH at 0.967 confidence with fallback=False. log10 of that value is 8.72
against a training maximum of 5.905 -- nearly three decades outside support.

The model was not wrong. It was asked a question from a region it had never
seen, and a tree ensemble answers such questions confidently, from whichever
leaf sits at the boundary. Confidence guards the MODEL's uncertainty, not the
INPUT's validity, and neither class weights nor the expected-cost rule can help,
because both operate on probabilities the model had no basis to produce.

Every feature is tested independently, because the ceiling clamp on throughput
closed one instance and the class of problem stayed open -- RTT on loopback sits
at 3.0 ms against a trained minimum of 3.018 ms.
"""

from __future__ import annotations

import joblib
import numpy as np
import pytest

from aaac.common.classes import AccessClass
from aaac.common.schemas import LinkSample
from aaac.estimator.features import FEATURE_NAMES, extract_features
from aaac.estimator.infer import classify, out_of_support, reset_model_cache
from aaac.estimator.train import COST_MATRIX

MODEL_VERSION = "v-test-support"
THRESHOLD = 0.60

#: Wide enough that an in-range vector classifies normally, tight enough that a
#: deliberate excursion is unambiguous.
RANGES = {
    "log10_throughput_kbps": [1.0, 6.0],
    "rtt_mean_ms": [3.0, 2400.0],
    "rtt_p95_ms": [3.5, 8300.0],
    "rtt_jitter_ms": [0.0, 2300.0],
    "fail_ratio": [0.0, 0.5],
    "stability": [0.0, 1.0],
    "n_rtt_samples": [2.0, 50.0],
}


class ConfidentModel:
    """Always returns a confident HIGH -- so any fallback must come from the
    support check, never from the confidence gate."""

    def predict_proba(self, X):  # noqa: N803
        n = np.asarray(X).shape[0]
        return np.repeat(np.array([[0.97, 0.02, 0.01]]), n, axis=0)


@pytest.fixture(autouse=True)
def _clear_cache():
    reset_model_cache()
    yield
    reset_model_cache()


def make_bundle(path, ranges=None, **overrides):
    bundle = {
        "model": ConfidentModel(),
        "feature_names": list(FEATURE_NAMES),
        "feature_ranges": dict(ranges if ranges is not None else RANGES),
        "model_version": MODEL_VERSION,
        "cost_matrix": COST_MATRIX.tolist(),
    }
    bundle.update(overrides)
    path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, path)
    return path


def in_range_sample(ticket_id: str = "t-ok") -> LinkSample:
    """A perfectly ordinary MEDIUM-ish client, comfortably inside support."""
    return LinkSample(
        ticket_id=ticket_id,
        probe_bytes=65536,
        probe_duration_ms=1100.0,          # ~477 kbps -> log10 ~2.68
        rtt_samples_ms=[190.0, 250.0, 210.0, 230.0, 205.0, 220.0],
        failed_requests=1,
        total_requests=20,
    )


def run(sample, path):
    return classify(
        sample, model_path=path, min_rtt_samples=5, confidence_threshold=THRESHOLD
    )


# --- the check cannot fire vacuously --------------------------------------


def test_in_range_input_classifies_normally(tmp_path):
    """If this ever falls back, every other test in this file is meaningless."""
    est = run(in_range_sample(), make_bundle(tmp_path / "m.joblib"))
    assert est.fallback is False
    assert est.access_class is AccessClass.HIGH
    assert est.confidence == pytest.approx(0.97)


def test_in_range_vector_reports_no_offenders():
    features = extract_features(in_range_sample())
    assert out_of_support(features, RANGES) == []


# --- every feature, independently -----------------------------------------


@pytest.mark.parametrize("index,name", list(enumerate(FEATURE_NAMES)))
def test_each_feature_triggers_fallback_when_above_its_range(index, name):
    features = extract_features(in_range_sample()).astype(float)
    features[index] = RANGES[name][1] + 1.0
    offenders = out_of_support(features, RANGES)
    assert len(offenders) == 1
    assert offenders[0].startswith(name)


@pytest.mark.parametrize("index,name", list(enumerate(FEATURE_NAMES)))
def test_each_feature_triggers_fallback_when_below_its_range(index, name):
    features = extract_features(in_range_sample()).astype(float)
    features[index] = RANGES[name][0] - 1.0
    offenders = out_of_support(features, RANGES)
    assert len(offenders) == 1
    assert offenders[0].startswith(name)


def test_non_finite_feature_is_out_of_support():
    features = extract_features(in_range_sample()).astype(float)
    features[0] = np.nan
    assert out_of_support(features, RANGES)


# --- end to end: the real finding -----------------------------------------


def test_the_loopback_probe_no_longer_classifies_confidently(tmp_path):
    """The exact failure: an unmeasurably fast probe on loopback.

    65,536 bytes in 0.001 ms is 524,288,000 kbps, log10 8.72. Before condition
    5 this returned HIGH, confidence 0.967, fallback False.
    """
    sample = LinkSample(
        ticket_id="t-loopback",
        probe_bytes=65536,
        probe_duration_ms=0.001,
        rtt_samples_ms=[3.0, 3.1, 2.9, 3.0, 3.2, 3.0],
        failed_requests=0,
        total_requests=7,
    )
    est = run(sample, make_bundle(tmp_path / "m.joblib"))

    assert est.fallback is True
    assert est.access_class is AccessClass.MEDIUM
    assert est.confidence == 0.0
    # The model loaded fine; we declined to trust it for this input.
    assert est.model_version == MODEL_VERSION


def test_rtt_below_trained_minimum_also_falls_back(tmp_path):
    """Not just throughput. Loopback RTT is ~3 ms against a 3.018 ms minimum.

    The throughput ceiling clamp closed one instance; this is the class.
    """
    sample = LinkSample(
        ticket_id="t-fast-rtt",
        probe_bytes=65536,
        probe_duration_ms=1100.0,       # throughput safely in range
        rtt_samples_ms=[0.4, 0.5, 0.45, 0.42, 0.48, 0.44],
        failed_requests=0,
        total_requests=7,
    )
    est = run(sample, make_bundle(tmp_path / "m.joblib"))
    assert est.fallback is True
    assert est.access_class is AccessClass.MEDIUM


def test_offending_feature_is_named_in_the_log(tmp_path, caplog):
    """A silent fallback would hide the very thing worth knowing."""
    sample = LinkSample(
        ticket_id="t-named",
        probe_bytes=65536,
        probe_duration_ms=0.001,
        rtt_samples_ms=[190.0, 250.0, 210.0, 230.0, 205.0, 220.0],
        failed_requests=0,
        total_requests=7,
    )
    with caplog.at_level("WARNING", logger="aaac.estimator.infer"):
        run(sample, make_bundle(tmp_path / "m.joblib"))

    assert "log10_throughput_kbps" in caplog.text
    assert "t-named" in caplog.text
    assert "outside" in caplog.text


# --- bundle validation ----------------------------------------------------


def test_bundle_without_feature_ranges_falls_back(tmp_path):
    """A bundle that cannot say what it was trained on cannot be range-checked.

    Skipping the check silently would mean the guard quietly does not exist,
    which is the same class of failure as substituting a default cost matrix.
    """
    path = tmp_path / "m.joblib"
    joblib.dump(
        {
            "model": ConfidentModel(),
            "feature_names": list(FEATURE_NAMES),
            "model_version": MODEL_VERSION,
            "cost_matrix": COST_MATRIX.tolist(),
        },
        path,
    )
    est = run(in_range_sample(), path)
    assert est.fallback is True
    assert est.access_class is AccessClass.MEDIUM


def test_bundle_with_incomplete_ranges_falls_back(tmp_path):
    partial = {k: v for k, v in RANGES.items() if k != "stability"}
    est = run(in_range_sample(), make_bundle(tmp_path / "m.joblib", ranges=partial))
    assert est.fallback is True


def test_bundle_with_inverted_range_falls_back(tmp_path):
    bad = dict(RANGES)
    bad["fail_ratio"] = [1.0, 0.0]
    est = run(in_range_sample(), make_bundle(tmp_path / "m.joblib", ranges=bad))
    assert est.fallback is True


# --- the shipped bundle carries ranges ------------------------------------


def test_exported_bundle_carries_ranges_for_every_feature():
    """models/link_classifier.joblib is the artefact that actually ships."""
    from aaac.estimator.infer import load_bundle

    bundle = load_bundle("models/link_classifier.joblib")
    if bundle is None:
        pytest.skip("no exported bundle on disk; run aaac.estimator.train")
    assert set(bundle["feature_ranges"]) == set(FEATURE_NAMES)
    for name in FEATURE_NAMES:
        lo, hi = bundle["feature_ranges"][name]
        assert lo <= hi
