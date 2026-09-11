# Cross-package integration issues

Found by M3 reading `origin/thisaru` @ `4a1acc5` and `origin/sachintha` @ `0e3b02d`
(remote refs as of 2026-09-08) against the contract in `CLAUDE.md` §3.

**M3 has not changed any M1 or M2 code, and will not.** The evaluator patching the
system under test would contaminate the result (§1.4). Each item below needs its
owner's decision; record the resolution under the item when it is made.

| # | Issue | Owner | Blocks | Status |
|---|---|---|---|---|
| A1 | `baseline` applies payload adaptation | M1 | Stage 2 | OPEN |
| A2 | A failed transfer ends the ticket instead of re-queueing it | M1 + M2 | Stage 2 | OPEN |
| A3 | M1's config loader rejects unknown keys | M3 | — | RESOLVED (M3 side) |
| A4 | Gaps in the event log | M1 (+ M2) | goodput, classifier scoring | OPEN |
| A5 | Controller reads the configured class mix | M1 | sensitivity sweep | OPEN (question) |

---

## A1 — `baseline` applies payload adaptation

**Contract, §3.4:** baseline is an access-blind queue — "fixed window `W_base`,
**full payload for everyone**, reset-on-failure re-queue".

**What the code does:**

1. `admission/api.py` `queue_estimate` has no mode check, so under `baseline` an
   accepted estimate calls `store.update_class`.
2. `queue_status` issues the admit token with `ticket.access_class`, and
   `common/tokens.py` `issue_token` sets `var = variant_for(cls)`.
3. `delivery/app.py` serves `payload["var"]`.

**Consequence:** under `baseline`, clients still on the MEDIUM join default receive
`reduced`, and clients classified LOW receive `essential`. The control condition
therefore contains C1 + C3. That narrows Δ under baseline — it **understates the
problem Stage 2 exists to measure**, and understates AAAC's improvement against it.

Windows (`window.py`) and re-queue (`requeue.py`) are mode-gated correctly; only the
class → variant path is not.

**Question for M1:** should `baseline` issue tokens as HIGH/`full` for everyone (as
`none` already does) while still accepting and logging `ESTIMATE`, so classifier
accuracy is scored identically across modes?

## A2 — A failed transfer ends the ticket instead of re-queueing it

- `client/sdk.py` `_attempt_transfer`: when `fetch_page` fails — window missed
  mid-transfer, a sub-resource failure, an origin 503 — the SDK POSTs
  `/queue/complete` with `ok=false`, then resumes polling.
- `admission/api.py` `queue_complete`: `ok=false` sets state `ABANDONED` and logs
  `ABANDON`.
- The SDK's next poll sees `ABANDONED` and exits with `Outcome.ABANDONED`.

**Consequence:** the timeout → re-queue path (reset-on-failure under `baseline`, C4
under `aaac`) only runs when the controller's sweep expires a ticket *before* the
client reports. The divergent retry loop the proposal is about is largely
short-circuited, and an origin 503 is logged as a client abandoning.

**Not verified by M3:** what happens when the sweep re-queues a ticket and a late
`ok=false` report then arrives — whether `store.complete` checks state first.
Worth a test on M1's side.

**Questions for M1 + M2:** should `ok=false` mean "this attempt failed" (→ `TIMEOUT` /
`REQUEUE`, carrying the bytes) rather than "this client is gone" (→ `ABANDON`)? If
`ABANDON` needs a reason field to separate the two, that is a `CONTRACT CHANGE`.

## A3 — M1's config loader rejects unknown keys — resolved on M3's side

`common/config.py` `_validate_keys` refuses any key outside §3.9, so M3's additions
to `configs/run.yaml` would have stopped the admission service starting.

**Resolution:** M3 removed them. `origin:` and `load:` now hold exactly the §3.9
keys; the M3-only parameters are named defaults in `src/aaac/origin/config.py`
(health window 5 s / 5 buckets, `ORIGIN_SAMPLE` every 1 s, `burst_fraction` 0.8 —
also written into every population file), and the results directory comes from
`AAAC_RESULTS_DIR`. A test pins the file to the contract keys.

**Later, if wanted:** an agreed `evaluation:` section, which M1's loader would need
to accept.

## A4 — Gaps in the event log

1. **`TIMEOUT` carries no `bytes`.** Deliberate on M1's side — the admission service
   cannot see them. But §4.4 goodput includes bytes burned on timed-out attempts, so
   goodput computed from the real log is an **upper bound**. `metrics.py` now says so
   in the provenance of every run where it applies. A real fix needs the SDK to
   report bytes on the timeout path.
2. **Rejected `/queue/estimate` calls are not logged** — `queue_estimate` returns
   before `logger.log`. The log cannot distinguish "never classified" from "estimate
   rejected", and §3.10 rule 2 says every control decision emits an event.
   `metrics.py` now warns with the coverage figure.
3. **`COMPLETE` has no `ok` field;** success is implied by the event type, because
   `ok=false` is logged as `ABANDON`. `metrics.py` counts it as success and discloses
   the inference.

## A5 — Controller reads the configured class mix

`admission/controller.py` approximates per-class waiting counts from
`cfg.load.class_mix` to compute W_mean and hence C_max. `load.class_mix` is the
testbed's ground-truth population mix — the population-level analogue of
`true_class`. A real portal would not know it, and in the LOW-share sensitivity sweep
(§4.5) the controller would be handed the answer.

**Question for M1:** could it use the actual per-class waiting counters the store
already maintains (`waiting:{class}`)?

---

## Merge-time notes

Every branch added these files independently of the initial commit, so each
conflicts:

| File | Suggested resolution |
|---|---|
| `.gitignore` | Take M3's — a superset of all four versions. One real disagreement: M1/M2 ignore all of `results/`; M3 ignores logs and populations but tracks figures and reports. Team call. |
| `Makefile`, `docker-compose.yml` | M1's and M2's are identical placeholders. Take M3's. |
| `Dockerfile` (root) | M1/M2 placeholder (`tail -f /dev/null`). M3 uses `docker/*.Dockerfile`; admission and delivery images still need writing. |
| `configs/run.yaml` | Union: M1/M2's `admission:`, `estimator:`, `delivery:` plus the (now identical) `origin:` and `load:`. |
| `pyproject.toml` | Union: `redis>=5`, `lightgbm==4.7.0`, `scikit-learn`, `joblib` from M1/M2; M3's version pins, `requires-python`, and ruff/mypy/pytest config. |
| `src/aaac/__init__.py` | M1/M2 empty; M3's has a docstring. Either. |
| `CLAUDE.md` | M2's branch **tracks** its own `CLAUDE.md`; M1 and M3 ignore theirs. Merging M2's branch will collide with an untracked local `CLAUDE.md` — back it up first. |

After the merge:

- `.gitattributes` forces LF. If files show as modified afterwards, run
  `git add --renormalize .` once, in its own commit.
- Compose wiring needs these variables:

  | Service | Reads |
  |---|---|
  | admission (M1) | `AAAC_CONFIG_PATH`, `AAAC_RUN_ID`, `AAAC_REDIS_URL` (in-memory store if unset), `AAAC_ORIGIN_URL`, `AAAC_RESULTS_DIR`, `AAAC_TOKEN_SECRET` |
  | delivery (M2) | `AAAC_ORIGIN_BASE`, `AAAC_TOKEN_SECRET` |
  | origin (M3) | `AAAC_CONFIG`, `AAAC_RUN_ID`, `AAAC_MODE`, `AAAC_SEED`, `AAAC_RESULTS_DIR` |

- M3 stubs to retire: `evaluation/access_class.py` → `common/classes.py`.
  `origin/config.py` → `common/config.py`, keeping the origin-only defaults
  somewhere M3 owns (M1's `OriginConfig` has no health fields).
- `tests/test_metrics_m1_log_shape.py` transcribes M1's event fields by hand; switch
  it to M1's real `EventLogger` so a change on their side fails a test.
