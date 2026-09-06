"""Fallback behaviour of the link estimator (C1).

The proposal claims no component depends on classifier correctness for safety
or liveness. The headline test here is the acceptance test for that claim:
delete models/link_classifier.joblib and the classify path still returns a
usable estimate -- MEDIUM, confidence 0.0, fallback=True -- instead of raising.

Every test builds its own bundle in tmp_path. None of them read the real
models/ directory, which does not exist in this repository yet: a test that
relied on it would pass for the trivial reason that no model was ever exported,
and would keep passing after someone broke the model-loading path.

No Redis, no network (contract rule 4).
"""

from __future__ import annotations

import joblib
import numpy as np
import pytest

from aaac.common.classes import AccessClass
from aaac.common.schemas import LinkSample
from aaac.estimator.features import FEATURE_NAMES
from aaac.estimator.infer import NO_MODEL_VERSION, classify, reset_model_cache
from aaac.estimator.train import COST_MATRIX

MODEL_VERSION = "v-test"

# Settings passed explicitly everywhere, so these tests never depend on
# configs/run.yaml being present or on its current values.
MIN_RTT_SAMPLES = 5
CONFIDENCE_THRESHOLD = 0.60


class FixedProbaModel:
    """Deterministic stand-in for the LightGBM classifier.

    Defined at module scope so joblib can pickle it. Using a fake rather than a
    trained model keeps these tests about the fallback logic: the probabilities
    are inputs to the decision rule, so they should be chosen, not sampled.
    """

    def __init__(self, proba: list[float]) -> None:
        self.proba = np.asarray(proba, dtype=float)

    def predict_proba(self, X):  # noqa: N803 - sklearn's argument name
        n = np.asarray(X).shape[0]
        return np.repeat(self.proba[None, :], n, axis=0)


class ExplodingModel:
    """A model that raises on predict. A broken model, not a missing one."""

    def predict_proba(self, X):  # noqa: N803
        raise RuntimeError("model is corrupt")


@pytest.fixture(autouse=True)
def _clear_cache():
    """infer caches the loaded bundle; tests must not leak one into the next."""
    reset_model_cache()
    yield
    reset_model_cache()


def make_bundle(path, proba=(0.95, 0.04, 0.01), **overrides):
    """Write a valid bundle to `path`, with fields overridable per test."""
    bundle = {
        "model": FixedProbaModel(list(proba)),
        "feature_names": list(FEATURE_NAMES),
        "model_version": MODEL_VERSION,
        "cost_matrix": COST_MATRIX.tolist(),
        "confidence_threshold": CONFIDENCE_THRESHOLD,
    }
    bundle.update(overrides)
    path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, path)
    return path


def make_sample(n_rtts: int = 6, ticket_id: str = "t-001") -> LinkSample:
    """A plausible waiting client: 64 KB probe, a handful of status polls."""
    return LinkSample(
        ticket_id=ticket_id,
        probe_bytes=65536,
        probe_duration_ms=1100.0,
        rtt_samples_ms=[190.0, 450.0, 260.0, 310.0, 280.0, 240.0][:n_rtts],
        failed_requests=2,
        total_requests=20,
    )


def run(sample, path, **kwargs):
    return classify(
        sample,
        model_path=path,
        min_rtt_samples=kwargs.pop("min_rtt_samples", MIN_RTT_SAMPLES),
        confidence_threshold=kwargs.pop(
            "confidence_threshold", CONFIDENCE_THRESHOLD
        ),
    )


# --- the acceptance test --------------------------------------------------


def test_deleting_the_model_file_degrades_to_medium(tmp_path):
    """Delete the bundle mid-life and the pipeline keeps completing.

    Asserts a real prediction first, so the fallback afterwards is known to be
    caused by the deletion rather than by the model never having worked.
    """
    path = make_bundle(tmp_path / "models" / "link_classifier.joblib")
    sample = make_sample()

    before = run(sample, path)
    assert before.fallback is False
    assert before.access_class is AccessClass.HIGH

    path.unlink()
    reset_model_cache()

    after = run(sample, path)
    assert after.fallback is True
    assert after.access_class is AccessClass.MEDIUM
    assert after.confidence == 0.0
    assert after.model_version == NO_MODEL_VERSION
    assert after.ticket_id == sample.ticket_id


def test_model_file_never_existed(tmp_path):
    est = run(make_sample(), tmp_path / "models" / "link_classifier.joblib")
    assert est.fallback is True
    assert est.access_class is AccessClass.MEDIUM
    assert est.confidence == 0.0


def test_unreadable_bundle_falls_back(tmp_path):
    """A corrupt pickle is condition 1, not a crash."""
    path = tmp_path / "link_classifier.joblib"
    path.write_bytes(b"not a joblib file")
    est = run(make_sample(), path)
    assert est.fallback is True
    assert est.access_class is AccessClass.MEDIUM


# --- condition 2: too few RTT samples -------------------------------------


def test_too_few_rtt_samples_falls_back(tmp_path):
    path = make_bundle(tmp_path / "m.joblib")
    est = run(make_sample(n_rtts=MIN_RTT_SAMPLES - 1), path)
    assert est.fallback is True
    assert est.access_class is AccessClass.MEDIUM
    assert est.confidence == 0.0
    # The model loaded fine; we chose not to ask it. Report which model.
    assert est.model_version == MODEL_VERSION


def test_exactly_min_rtt_samples_is_enough(tmp_path):
    path = make_bundle(tmp_path / "m.joblib")
    est = run(make_sample(n_rtts=MIN_RTT_SAMPLES), path)
    assert est.fallback is False


# --- condition 3: low confidence ------------------------------------------


def test_low_confidence_falls_back(tmp_path):
    """Top class 0.40, below the 0.60 threshold -- abstain."""
    path = make_bundle(tmp_path / "m.joblib", proba=(0.40, 0.35, 0.25))
    est = run(make_sample(), path)
    assert est.fallback is True
    assert est.access_class is AccessClass.MEDIUM
    assert est.confidence == 0.0
    assert est.model_version == MODEL_VERSION


# --- a valid estimate is not marked fallback ------------------------------


def test_valid_estimate_is_not_fallback(tmp_path):
    path = make_bundle(tmp_path / "m.joblib", proba=(0.95, 0.04, 0.01))
    est = run(make_sample(), path)

    assert est.fallback is False
    assert est.access_class is AccessClass.HIGH
    assert est.confidence == pytest.approx(0.95)
    assert est.model_version == MODEL_VERSION


def test_confident_low_is_served_as_low(tmp_path):
    path = make_bundle(tmp_path / "m.joblib", proba=(0.05, 0.10, 0.85))
    est = run(make_sample(), path)
    assert est.fallback is False
    assert est.access_class is AccessClass.LOW


def test_cost_rule_overrides_argmax_towards_pessimism(tmp_path):
    """argmax says HIGH at 0.62; expected cost says LOW.

    This is the design working: a residual 18% chance of LOW makes a HIGH call
    too expensive to justify.

    It also pins the confidence semantics. confidence is the model's certainty
    about its READ of the link (0.62, the top class probability), not about the
    POLICY call layered on top. Reporting the returned class's own probability
    (0.18) would make a deliberate override look like uncertainty and would
    stop confidence being comparable with confidence_threshold.
    """
    path = make_bundle(tmp_path / "m.joblib", proba=(0.62, 0.20, 0.18))
    est = run(make_sample(), path)

    assert est.fallback is False
    assert est.access_class is AccessClass.LOW
    assert est.confidence == pytest.approx(0.62)
    # The returned class is deliberately NOT argmax.
    assert est.access_class is not AccessClass.HIGH


# --- bundle validation ----------------------------------------------------


def test_bundle_without_cost_matrix_falls_back(tmp_path):
    """A bundle missing the matrix is a fallback, not a guessed matrix.

    The decision rule is part of the model. Substituting a default would serve
    a different policy than the one that was measured, silently.
    """
    path = tmp_path / "m.joblib"
    joblib.dump(
        {
            "model": FixedProbaModel([0.95, 0.04, 0.01]),
            "feature_names": list(FEATURE_NAMES),
            "model_version": MODEL_VERSION,
        },
        path,
    )
    est = run(make_sample(), path)
    assert est.fallback is True
    assert est.access_class is AccessClass.MEDIUM
    assert est.model_version == NO_MODEL_VERSION


def test_feature_name_drift_falls_back(tmp_path):
    """Train/serve skew must not be served as a confident answer."""
    drifted = list(FEATURE_NAMES)
    drifted[0], drifted[1] = drifted[1], drifted[0]
    path = make_bundle(tmp_path / "m.joblib", feature_names=drifted)
    est = run(make_sample(), path)
    assert est.fallback is True
    assert est.access_class is AccessClass.MEDIUM


def test_exploding_model_falls_back(tmp_path):
    path = make_bundle(tmp_path / "m.joblib", model=ExplodingModel())
    est = run(make_sample(), path)
    assert est.fallback is True
    assert est.access_class is AccessClass.MEDIUM


# --- measured link metrics survive the fallback ---------------------------


def test_link_metrics_are_reported_even_on_fallback(tmp_path):
    """A fallback estimate still carries real measured numbers.

    M1 and M3 get the link signals regardless, which is what makes a fallback
    useful rather than merely safe.
    """
    sample = make_sample()
    est = run(sample, tmp_path / "absent.joblib")

    assert est.fallback is True
    # 65536 bytes * 8 / 1100 ms
    assert est.throughput_kbps == pytest.approx(65536 * 8.0 / 1100.0)
    assert est.rtt_mean_ms == pytest.approx(288.3333, rel=1e-4)
    assert est.rtt_jitter_ms > 0.0
    assert est.loss_ratio == pytest.approx(2 / 20)
    assert 0.0 <= est.stability <= 1.0


def test_classify_never_raises_on_a_degenerate_sample(tmp_path):
    """No probe, no polls, everything failed. Still an estimate, not a crash."""
    sample = LinkSample(
        ticket_id="t-degenerate",
        probe_bytes=0,
        probe_duration_ms=0.0,
        rtt_samples_ms=[],
        failed_requests=5,
        total_requests=5,
    )
    est = run(sample, tmp_path / "absent.joblib")
    assert est.fallback is True
    assert est.access_class is AccessClass.MEDIUM
    assert est.throughput_kbps == 0.0
