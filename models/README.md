# models/

## What is and isn't in git

`link_classifier.joblib` is **gitignored** (`.gitignore` → `models/*.joblib`). Only
`metrics.json` and this file are committed. The bundle is a build artefact: it is
reproducible from source plus a seed, so committing a 190 KB binary on every
retrain would add nothing but history weight.

That reproducibility is the whole reason the bundle can be left out, and it holds
only if the toolchain matches. `lightgbm` is therefore pinned exactly in
`pyproject.toml`; a different build can produce a different tree ensemble from
identical inputs.

## Regenerate the bundle

```powershell
.\.venv\Scripts\Activate.ps1
$env:PYTHONPATH="src"
python -m aaac.estimator.train --n 12000 --seed 1
```

This writes both `models/link_classifier.joblib` and `models/metrics.json`. The
numbers it prints should match the committed `metrics.json` exactly. If they do
not, something in the toolchain has moved — investigate before trusting the
model, and do not update `metrics.json` to make the mismatch go away.

## Current bundle

| | |
|---|---|
| `model_version` | `v3-synthetic-7f-ranges` |
| Features | 7, in `FEATURE_NAMES` order (see `src/aaac/estimator/features.py`) |
| Training data | synthetic, `n=12000`, `seed=1` |
| Size | 189.7 KB (target < 200 KB) |
| Inference | 0.361 ms/call median (target < 2 ms) |

Built with:

| Package | Version |
|---|---|
| Python | 3.11.9 |
| lightgbm | 4.7.0 (pinned) |
| scikit-learn | 1.9.0 |
| numpy | 2.4.6 |
| joblib | 1.6.0 |

## Version history

- **`v3-synthetic-7f-ranges`** — identical model, but the bundle now carries
  `feature_ranges`: the per-feature min/max of the training split. `infer.py`
  uses them for fallback condition 5 (input outside training support) and
  **rejects any bundle that lacks them** — a bundle that cannot say what it was
  trained on cannot be range-checked, and skipping the check silently would mean
  the guard quietly does not exist. Any `v1`/`v2` bundle therefore falls back.
- **`v2-synthetic-7f`** — dropped `loss_ratio` from the feature vector, taking it
  from 8 features to 7. It duplicated `fail_ratio` exactly, because `LinkSample`
  carries only `failed_requests / total_requests`. See the DECISION note in
  `features.py`. `LinkEstimate.loss_ratio` is unaffected and is still reported.
- **`v1-synthetic`** — initial 8-feature model. Any `v1` bundle now **fails
  `infer.py`'s bundle validation** and falls back to MEDIUM, because its
  `feature_names` no longer match `FEATURE_NAMES`. That is the intended
  behaviour, not a bug.

## Model card

`MODEL_CARD.md` is not written yet. When it is, generate it with the `model-card`
skill, which builds every number from `metrics.json` and the exported bundle
rather than from memory.
