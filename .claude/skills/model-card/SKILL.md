---
name: model-card
description: Write, refresh, or audit models/MODEL_CARD.md for the AAAC link classifier. Generates every number from models/metrics.json and the exported joblib bundle — never from memory or from the conversation — and refuses to emit a card with any required section missing.
---

# Model card

`MODEL_CARD.md` is the document an examiner reads to decide whether the C1
classifier is trustworthy. Every number in it is a claim. This skill makes those
claims traceable to an artefact on disk.

## Sources of truth — read these, do not recall them

| Fact | Comes from |
|---|---|
| Feature order | `FEATURE_NAMES` in `src/aaac/estimator/features.py`, verbatim |
| Metrics | `models/metrics.json` |
| Hyperparameters, model version | the joblib bundle at `models/link_classifier.joblib` |
| Cost matrix | the training source, `src/aaac/estimator/train.py` |
| Thresholds | `configs/run.yaml` → `estimator.confidence_threshold` |

If a number appears in the conversation but not in one of these files, it does not
go in the card. If a file is missing, stop and say which one.

## Required sections — all ten

Refuse to emit a card missing any of these. An incomplete card is worse than none,
because it reads as complete.

1. **Model version and export date** — from the bundle
2. **Feature order** — the eight names, numbered, in fixed order, copied verbatim
   from `FEATURE_NAMES`. State that the order is a contract and that changing it
   forces a `model_version` bump (§4.1)
3. **Training data: source and size** — synthetic or measured, generator seed,
   `n` samples, class balance
4. **Hyperparameters** — n_estimators, max_depth, learning rate, class weights
5. **Accuracy** — overall, with the evaluation split described
6. **Confusion matrix** — full 3×3, true rows × predicted columns, labelled
   HIGH / MEDIUM / LOW, raw counts not just percentages
7. **Per-class recall** — all three classes
8. **`optimistic_error_rate`** — the rate of clients judged *more capable than
   they are*, under both naive argmax and the cost-sensitive rule, so the
   improvement is visible. Explain why this is the headline error metric and why
   LOW→HIGH alone is not (§5.1)
9. **Cost matrix and its justification** — the values, plus the reasoning: a slow
   link called HIGH is a guaranteed timeout and is exactly the exclusion the
   project exists to remove; a fast link called LOW is a plainer page. State the
   price paid — HIGH-class recall — as a number, not as a hedge
10. **Fallback behaviour** — the three conditions that produce
    `access_class=MEDIUM, confidence=0.0, fallback=True`: model missing or
    unloadable, fewer than `min_rtt_samples` RTT samples, top-class probability
    below `confidence_threshold`. State the acceptance test: delete the joblib
    file, run the full `aaac` pipeline, it completes with `fallback: true` on
    every `ESTIMATE`

## Synthetic vs measured — say which, prominently

Put a line at the top of the card, not buried: **"Numbers in this card are from
SYNTHETIC data"** or **"...from MEASURED netem traces"**. If the card mixes both,
label every table individually. Never let a reader assume synthetic figures are
measured ones.

Where the figures are synthetic, carry the §5.2 caveat: the netem profiles
(HIGH 50 Mbit/15 ms vs LOW 512 kbit/250 ms/3%) are over a decade apart and make
classification easy by construction. Real links are messier. The confidence
threshold plus fallback is what makes the design safe when the task is not easy.

## Refuse-and-report behaviour

If a required section cannot be filled from the files on disk:

- Do not write the card
- List exactly which sections are unfillable and which file each one needs
- Offer to write the card once the training run has produced them

## Never

- Never round a number in a flattering direction, or drop a class from the
  confusion matrix because it looks bad. §1.5 is absolute.
- Never re-run training to get a nicer number for the card. The card documents the
  exported model, whatever it scored.
- Never describe a provisional choice as settled. The cost matrix is provisional
  (§5.1) and the card must say so.
