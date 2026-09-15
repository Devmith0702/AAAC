# Cross-package integration issues

Found by M3 reading `origin/thisaru` @ `4a1acc5` and `origin/sachintha` @ `0e3b02d`
(remote refs as of 2026-09-08) against the contract in `CLAUDE.md` §3.

**M3 has not changed any M1 or M2 code, and will not.** The evaluator patching the
system under test would contaminate the result (§1.4). Each item below needs its
owner's decision; record the resolution under the item when it is made.

| # | Issue | Owner | Blocks | Status |
|---|---|---|---|---|
| A1 | `baseline` applies payload adaptation | M1 | Stage 2 | OPEN — confirmed in a live run |
| A2 | A failed transfer ends the ticket instead of re-queueing it | M1 + M2 | Stage 2 | OPEN |
| A3 | M1's config loader rejects unknown keys | M3 | — | RESOLVED (M3 side) |
| A4 | Gaps in the event log | M1 (+ M2) | goodput, classifier scoring | OPEN |
| A5 | Controller reads the configured class mix | M1 | sensitivity sweep | OPEN (question) |
| A6 | M1's config loader has no public path-taking entry point | M1 | — | OPEN (request) |
| A7 | `AAAC_CONFIG` vs `AAAC_CONFIG_PATH` name the same file | M1 + M3 | — | WORKED AROUND |
| A8 | `mode` cannot be set per run; the file is the only source | M1 | mode matrix (§4.5) | OPEN |
| A9 | The origin's record and the delivery templates disagreed | M3 + M2 | every real run | FIXED |
| A10 | Compose let the origin and the admission service run different modes | M3 | every real run | FIXED |

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

## A6 — M1's config loader has no public path-taking entry point

`common/config.py` exposes `get_config()`, which reads `AAAC_CONFIG_PATH` and
caches a module-level singleton. There is no public way to validate *a named
file*, so M3's loader calls the module-private `_load_config_from_file` to check
that `configs/run.yaml` is a file the admission service would accept.

**Request for M1:** a public `load_config(path)` alongside `get_config()`. One
line on their side, and it removes M3's only reach into a private name.

## A7 — `AAAC_CONFIG` vs `AAAC_CONFIG_PATH`

M1's loader reads `AAAC_CONFIG_PATH`; M3's origin reads `AAAC_CONFIG`. Same file,
two spellings, and a run configured with one of them silently leaves the other
service on its default.

**Worked around:** `docker-compose.yml` sets **both** for the admission service,
so neither depends on the other's spelling. Worth collapsing to one name.

## A8 — `mode` cannot be set per run

M1's loader applies no environment overrides, so the admission service takes
`mode` from `configs/run.yaml` and nothing else. M3's origin honours `AAAC_MODE`.
Two consequences:

1. **Compose cannot switch mode.** `make run MODE=baseline` re-modes the origin
   and leaves the admission service running whatever the file says — the two
   services would disagree about which experiment is running. The Makefile
   default and the file are therefore pinned to the same value, and the §4.5 mode
   matrix cannot be driven until this is settled.
2. **The committed value is load-bearing for M1's own tests.** Five tests in
   `tests/admission/test_api.py` fail under `mode: none`, because `none` admits
   every ticket immediately (`state: ADMITTED`, estimates refused). The merge set
   `mode: aaac`, which both M1's and M2's branches used and under which every
   admission, client, delivery and common test passes.

**Question for M1:** can the loader read `AAAC_MODE`, `AAAC_RUN_ID` and
`AAAC_SEED` as overrides, the way M3's does? Without it the experiment harness
has to rewrite `configs/run.yaml` between runs, which makes the run parameters a
moving target in the middle of the matrix.

## A9 — the origin's record and the delivery templates disagreed — FIXED

The first end-to-end run of the merged system failed completely: every client
abandoned, zero bytes transferred, and the delivery service answered 500 on every
`/result`.

    jinja2.exceptions.UndefinedError: 'dict object' has no attribute 'exam'
    templates/reduced.html, line 51:  <h1>{{ r.exam }}</h1>

M3's origin emitted the three fields §4.1 names — `index_no`, `name`, and a
subject/grade list under the key `results`. M2's templates render fourteen
(`exam`, `year`, `centre`, `district`, `z_score`, `district_rank`,
`island_rank`, `outcome`, `passed`, `issued`, `reference`, …) and iterate
`r.subjects`, where each subject also carries a `medium`. Jinja renders under
`StrictUndefined`, so the first missing field is a 500, not a blank.

**Why no test caught it.** M2 rendered its own `variants.py:sample_record()`,
which has all fourteen. M3 asserted its own record's shape and never rendered
anything. Both suites were green, and the two records never met until a real
client fetched a real page. This is the class of defect that only a run of the
assembled system can find.

**Resolution:** the origin now emits the full record the result page renders,
still deterministic from `(seed, index)` and still size-stable (32-byte spread
across 2,000 indices, ~1 KB each). `tests/test_origin_records.py` now renders
every variant from an origin record, so the two sides are pinned together by a
test that fails at build time instead of at run time.

Measured immediately afterwards, from an origin record through M2's renderer:

| variant | document | total transferred | requests | budget (§3.9) |
|---|---|---|---|---|
| `full` | 6,694 B | 411,655 B | 6 | 460,800 B |
| `reduced` | 4,969 B | 4,969 B | 1 | 61,440 B |
| `essential` | 1,909 B | 1,909 B | 1 | 6,144 B |

## A10 — compose let the two services run different modes — FIXED

A10 is A8 with teeth. M1 takes `mode` from `configs/run.yaml` and honours no
override; M3's origin honours `AAAC_MODE`. The compose file passed
`AAAC_MODE: ${AAAC_MODE:-none}` to the origin, so a stack brought up without
that variable ran **the origin in `none` and the admission service in `aaac`**,
silently.

It was caught by the analysis rather than by a service: `ORIGIN_SAMPLE` events
carried `mode: none`, the admission events carried `mode: aaac`, and
`EventLog.single_mode()` refused the pair —

    LogIntegrityError: expected exactly one mode in the log, found ['aaac', 'none'].
    Metrics computed across mixed modes are not a controlled comparison.

which is exactly the guard doing its job. Had the origin's samples not carried
the mode, the run would have produced plausible numbers from two different
experiments.

**Resolution:** `AAAC_MODE` and `AAAC_SEED` are no longer set on the origin in
`docker-compose.yml`, and the Makefile no longer has a `MODE` variable to pass.
`configs/run.yaml` is the single source of both for every service, which is what
§3.2 already said. `AAAC_RUN_ID` stays — both services need the *same* one so
their two logs can be paired — with the caveat below.

**Still open, and it belongs with A8:** `AAAC_RUN_ID` defaults to `dev`. Two runs
that both take the default append to one `events-dev.jsonl`, and the analysis
then refuses it for holding more than one run. Every real run must pass its own
(`make up RUN_ID=s1-aaac`). M1's service generates a unique id when the variable
is absent, but the origin would not know it, so the two logs could not be paired
— which is why the variable cannot simply be dropped.

---

## Merge resolutions

All three branches are merged into `dev`. Each had added these files
independently of the initial commit, so each conflicted. What was taken:

| File | Resolution |
|---|---|
| `.gitignore` | M3's — a superset. One disagreement is **still a team call**: M1/M2 ignore all of `results/`; M3 ignores event logs and populations but tracks figures and reports so the write-up can cite them. |
| `Makefile` | M3's, with `run` / `stop` kept as aliases for `up` / `down`. |
| `docker-compose.yml` | M3's testbed, plus real `admission` and `delivery` services (below). M1/M2's placeholder `origin` and `delivery` stubs dropped — the real services exist now. |
| `Dockerfile` (root) | Left as M1/M2's placeholder; nothing builds from it any more. Deletable once the team agrees. |
| `configs/run.yaml` | Union of all five sections. `mode: aaac` — see A8. |
| `pyproject.toml` | Union of both dependency sets. Added `jinja2` (M2's `delivery/variants.py` imports it but no branch declared it) and `[tool.setuptools.package-data]`, so the delivery templates and their static sub-resources travel into the installed package — the images run `pip install .` with nothing bind-mounted, and the variants are the payload under measurement. |
| `src/aaac/__init__.py` | M3's, with the docstring. |
| `CLAUDE.md` | M2's tracked copy. M3's brief stays local as `CLAUDE-M3.md`. |

`.gitattributes` renormalisation was not needed: `git ls-files --eol` shows no
CRLF in the index after the merge.

### Stubs retired

- **`evaluation/access_class.py` — deleted.** Every M3 module now imports
  `AccessClass` from `common/classes.py`. The two enums agreed value for value,
  so the contract held. This also removed a real defect: M3 was passing
  `evaluation.access_class.AccessClass` into M2's `run_client`, which expects
  `common.classes.AccessClass` — a different type, on the load generator's only
  route into M2's SDK.
- **`origin/config.py` — kept, and no longer a stub.** M1's `common/config.py`
  cannot express what M3 needs: the origin health window / bucket count / sample
  interval, `burst_fraction`, `results_dir`, the `AAAC_*` run overrides, and the
  range checks. It therefore survives as M3's view *over* M1's loader —
  `load_run_config()` parses through `common/config.py` first, so a file M3
  accepts is a file the admission service accepts, and a file M1 rejects fails
  here with a message saying so rather than at `docker compose up`.

`tests/test_metrics_m1_log_shape.py` now produces its records through M1's real
`EventLogger`, so the envelope and the closed vocabulary come from their code and
a change to either fails the test. Only `ts` is supplied by the test, to keep the
timing assertions deterministic.

### Compose wiring

`docker/admission.Dockerfile` and `docker/delivery.Dockerfile` install and run
M1's and M2's packages unmodified. Both services are now in `docker-compose.yml`:

| Service | Reads |
|---|---|
| admission (M1) | `AAAC_CONFIG_PATH` (and `AAAC_CONFIG`, see A7), `AAAC_RUN_ID`, `AAAC_REDIS_URL` (in-memory store if unset), `AAAC_ORIGIN_URL`, `AAAC_RESULTS_DIR`, `AAAC_TOKEN_SECRET` |
| delivery (M2) | `AAAC_CONFIG_PATH`, `AAAC_ORIGIN_BASE`, `AAAC_TOKEN_SECRET` |
| origin (M3) | `AAAC_CONFIG`, `AAAC_RUN_ID`, `AAAC_MODE`, `AAAC_SEED`, `AAAC_RESULTS_DIR` |

`AAAC_TOKEN_SECRET` is required rather than defaulted: M1 signs the admit token
with it and M2 verifies with it, and an empty shared secret is not a thing to
discover during a run. Copy `.env.example` to `.env` before `make up`.

**Not verified:** the images have not been built or run yet. The compose file
parses (`docker compose config`), and that is all that has been checked.
