"""Train and export the link classifier (C1).

Run:
    python -m aaac.estimator.train --n 12000 --seed 1

THE CENTRAL IDEA -- COST ASYMMETRY
----------------------------------
Accuracy is the wrong objective here. The two error directions have very
different consequences:

    LOW predicted as HIGH   -> short window + 450 KB payload sent down a
                               512 kbit link -> guaranteed timeout. This is
                               precisely the exclusion the project exists to
                               remove. EXPENSIVE.

    HIGH predicted as LOW   -> a fibre user receives a plain 6 KB page and a
                               generous window. Finishes instantly, page is
                               less pretty. CHEAP.

So the model is deliberately biased pessimistic, in two places:

  1. Training: class_weight raises the cost of misclassifying LOW examples.
  2. Decision: instead of taking argmax of the predicted probabilities, we
     take the class with the lowest EXPECTED COST under COST_MATRIX. A HIGH
     prediction must therefore clear a much higher bar than a LOW one.

A model at 88% accuracy with almost no LOW->HIGH errors is better than one at
92% that makes them regularly. Report the LOW->HIGH rate on its own line; it
is the number that matters, not the headline accuracy.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import joblib
import lightgbm as lgb
import numpy as np
from sklearn.metrics import confusion_matrix
from sklearn.model_selection import train_test_split

from aaac.common.classes import AccessClass

from .features import FEATURE_NAMES
from .synthdata import generate

MODEL_PATH = Path("models/link_classifier.joblib")
MODEL_VERSION = "v3-synthetic-7f-ranges"

CLASS_NAMES = [AccessClass(i).name for i in range(3)]

# COST_MATRIX[true][predicted]. Diagonal is zero -- a correct call costs
# nothing. The asymmetry across the diagonal is the whole design: the upper
# right (a slow link called fast) is expensive, the lower left (a fast link
# called slow) is nearly free. These numbers are a judgement call and belong
# in MODEL_CARD.md with this reasoning attached.
COST_MATRIX = np.array(
    [
        # pred: HIGH  MEDIUM   LOW
        [0.0, 0.4, 0.8],    # true HIGH   -- being downgraded is cheap
        [2.5, 0.0, 0.4],    # true MEDIUM -- MEDIUM called HIGH is the error
        [6.0, 2.0, 0.0],    # true LOW      that actually occurs in practice
    ],
    dtype=float,
)

# Training-time weights, same intuition applied at the sample level.
CLASS_WEIGHT = {0: 1.0, 1: 1.5, 2: 3.0}

CONFIDENCE_THRESHOLD = 0.60  # configs/run.yaml -> estimator.confidence_threshold


def cost_sensitive_predict(proba: np.ndarray) -> np.ndarray:
    """Pick the class with the lowest expected cost, not the highest probability.

    expected_cost[k] = sum_j P(true=j) * COST_MATRIX[j][k]

    With COST_MATRIX as defined, predicting HIGH while carrying even a small
    residual probability of LOW is heavily penalised, so the model abstains
    from HIGH unless it is genuinely confident.
    """
    expected = proba @ COST_MATRIX      # (n, 3): cost of each possible call
    return np.argmin(expected, axis=1)


def low_to_high_rate(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    """Fraction of true-LOW clients wrongly called HIGH.

    The brief names this as the headline error, but note what the numbers show:
    it sits at zero because LOW and HIGH are over a decade apart in throughput
    and are essentially never confused. Reporting only this figure would be
    self-congratulatory. See optimistic_error_rate() for the error that
    actually occurs.
    """
    low = y_true == int(AccessClass.LOW)
    if low.sum() == 0:
        return 0.0
    return float((y_pred[low] == int(AccessClass.HIGH)).mean())


def optimistic_error_rate(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    """Fraction of clients judged MORE capable than they really are.

    AccessClass is ordered HIGH=0 < MEDIUM=1 < LOW=2, so pred < true means the
    system over-estimated the link and will send too heavy a payload with too
    short a window. This is the whole family of dangerous errors, of which
    LOW->HIGH is only the rarest and most extreme member. In practice the
    MEDIUM->HIGH case dominates, and this is the number to drive down.
    """
    return float((y_pred < y_true).mean())


def report(y_true: np.ndarray, y_pred: np.ndarray, label: str) -> dict:
    cm = confusion_matrix(y_true, y_pred, labels=[0, 1, 2])
    acc = float((y_true == y_pred).mean())
    recall = (cm.diagonal() / np.maximum(cm.sum(axis=1), 1)).astype(float)
    l2h = low_to_high_rate(y_true, y_pred)
    opt = optimistic_error_rate(y_true, y_pred)
    mean_cost = float(COST_MATRIX[y_true, y_pred].mean())

    print(f"\n=== {label} ===")
    print(f"accuracy            {acc:.3f}")
    print(f"mean decision cost  {mean_cost:.3f}   (lower is better)")
    print(f"optimistic errors   {opt:.4f}   <-- judged better than reality: THE number")
    print(f"LOW -> HIGH rate    {l2h:.4f}   (near zero by construction: far apart)")
    print("per-class recall    " + "  ".join(
        f"{n}={r:.3f}" for n, r in zip(CLASS_NAMES, recall)))
    print("\nconfusion matrix (rows = true, cols = predicted)")
    print(f"{'':>8}" + "".join(f"{n:>9}" for n in CLASS_NAMES))
    for i, name in enumerate(CLASS_NAMES):
        print(f"{name:>8}" + "".join(f"{v:>9d}" for v in cm[i]))

    return {
        "accuracy": acc,
        "mean_cost": mean_cost,
        "low_to_high_rate": l2h,
        "optimistic_error_rate": opt,
        "recall": {n: float(r) for n, r in zip(CLASS_NAMES, recall)},
        "confusion_matrix": cm.tolist(),
    }


def train(n: int = 12000, seed: int = 1) -> dict:
    X, y, _ = generate(n=n, seed=seed)
    X_tr, X_te, y_tr, y_te = train_test_split(
        X, y, test_size=0.25, random_state=seed, stratify=y
    )
    print(f"train={len(X_tr)}  test={len(X_te)}  features={len(FEATURE_NAMES)}")

    model = lgb.LGBMClassifier(
        objective="multiclass",
        num_class=3,
        n_estimators=100,       # brief: ~100 trees
        max_depth=4,            # brief: shallow, so it generalises
        num_leaves=15,          # <= 2**max_depth - 1, keeps trees honest
        learning_rate=0.1,
        min_child_samples=30,   # no leaf built on a handful of odd rows
        subsample=0.9,
        subsample_freq=1,
        colsample_bytree=0.9,
        class_weight=CLASS_WEIGHT,
        random_state=seed,
        n_jobs=1,               # determinism over speed, per contract rule 5
        verbose=-1,
    )
    # Train on plain arrays; FEATURE_NAMES travels in the exported bundle
    # instead, which is what infer.py validates against.
    model.fit(X_tr, y_tr)

    proba = model.predict_proba(X_te)

    # Both decision rules, side by side. The comparison IS the argument: the
    # cost-sensitive rule should trade a little accuracy for a large drop in
    # LOW->HIGH errors. If it does not, the cost matrix needs revisiting.
    argmax_metrics = report(y_te, np.argmax(proba, axis=1), "argmax (naive)")
    cost_pred = cost_sensitive_predict(proba)
    cost_metrics = report(y_te, cost_pred, "cost-sensitive (shipped)")

    # How often would inference fall back to MEDIUM for low confidence?
    abstain = float((proba.max(axis=1) < CONFIDENCE_THRESHOLD).mean())
    print(f"\nbelow confidence threshold ({CONFIDENCE_THRESHOLD}): {abstain:.1%} "
          f"-> these become fallback=True, access_class=MEDIUM at inference")

    # Latency check -- the brief asks for under 2 ms per call.
    single = X_te[:1]
    t0 = time.perf_counter()
    for _ in range(200):
        model.predict_proba(single)
    per_call_ms = (time.perf_counter() - t0) / 200 * 1000
    print(f"inference latency   {per_call_ms:.3f} ms/call  (target < 2 ms)")

    # Per-feature training support, so infer.py can refuse to answer about
    # inputs from a region the model has never seen. Computed on the TRAINING
    # split, because that is what "trained on" means -- not the test split, and
    # not the full generated set.
    feature_ranges = {
        name: [float(X_tr[:, i].min()), float(X_tr[:, i].max())]
        for i, name in enumerate(FEATURE_NAMES)
    }
    print()
    print("per-feature training support (infer.py falls back outside this)")
    for name, (lo, hi) in feature_ranges.items():
        print(f"  {name:<24} {lo:12.4f} .. {hi:12.4f}")

    bundle = {
        "model": model,
        "feature_names": list(FEATURE_NAMES),   # infer.py validates against this
        "feature_ranges": feature_ranges,       # fallback condition 5
        "model_version": MODEL_VERSION,
        "cost_matrix": COST_MATRIX.tolist(),
        "class_weight": CLASS_WEIGHT,
        "confidence_threshold": CONFIDENCE_THRESHOLD,
        "trained_on": {"source": "synthetic", "n": n, "seed": seed},
        "metrics": {"argmax": argmax_metrics, "cost_sensitive": cost_metrics},
    }
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, MODEL_PATH, compress=6)
    size_kb = MODEL_PATH.stat().st_size / 1024
    print(f"\nsaved {MODEL_PATH}  ({size_kb:.1f} KB, target < 200 KB)")
    if size_kb > 200:
        print("WARNING: model exceeds the 200 KB budget -- reduce n_estimators")

    Path("models/metrics.json").write_text(
        json.dumps(bundle["metrics"], indent=2), encoding="utf-8"
    )
    return bundle["metrics"]


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=12000)
    ap.add_argument("--seed", type=int, default=1)
    train(**vars(ap.parse_args()))
