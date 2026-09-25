# Cross-package integration issues

Found by M3 reading `origin/thisaru` @ `4a1acc5` and `origin/sachintha` @ `0e3b02d`
(remote refs as of 2026-09-08) against the contract in `CLAUDE.md` §3.

**M3 has not changed any M1 or M2 code, and will not.** The evaluator patching the
system under test would contaminate the result (§1.4). Each item below needs its
owner's decision; record the resolution under the item when it is made.

| # | Issue | Owner | Blocks | Status |
|---|---|---|---|---|
| A1 | `baseline` applies payload adaptation | M1 | Stage 2 | FIXED — was measured at 137/137 `reduced` |
| A2 | A failed transfer ends the ticket instead of re-queueing it | M1 + M2 | Stage 2 | OPEN |
| A3 | M1's config loader rejects unknown keys | M3 | — | RESOLVED (M3 side) |
| A4 | Gaps in the event log | M1 (+ M2) | goodput, classifier scoring | OPEN |
| A5 | Controller reads the configured class mix | M1 | sensitivity sweep | OPEN (question) |
| A6 | M1's config loader has no public path-taking entry point | M1 | — | OPEN (request) |
| A7 | `AAAC_CONFIG` vs `AAAC_CONFIG_PATH` name the same file | M1 + M3 | — | WORKED AROUND |
| A8 | `mode` cannot be set per run; the file is the only source | M1 | mode matrix (§4.5) | OPEN |
| A9 | The origin's record and the delivery templates disagreed | M3 + M2 | every real run | FIXED |
| A10 | Compose let the origin and the admission service run different modes | M3 | every real run | FIXED |
| A11 | A failed transfer downgrades the client in `none` mode too | M1 | Stage 1 control condition | FIXED |
| A12 | The capacity estimate collapses, so the queue admits nobody | M1 | every mode | FIXED |
| A13 | Redis is never flushed between runs | M3 | every run in a matrix | FIXED |
| A14 | The client reports a slow link as a dead admission service | M2 | LOW-class completion | FIXED |
| A15 | The live dashboard shows 0 completions for classes at 100% | M1 | the demo, not the data | FIXED |
| A16 | `plots.py` / `report.py` cannot read real logs, and overwrite them | M3 | every figure and report | FIXED |

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

## A11 — a failed transfer downgrades the client in `none` mode too

Found in live data, not by reading: the first shaped run of `mode: none` emitted
**4 × TIMEOUT, 4 × DOWNGRADE and 4 × REQUEUE** among 140 clients.

`none` admits every ticket immediately with a 3600 s window, so a window expiry
cannot happen inside a five-minute run. These came from the other entry to the
same code path — `queue_complete` with `ok=false` now calls `handle_timeout` —
and `requeue.py` branches only on `baseline`:

```python
if cfg.mode == "baseline":
    score = await store.next_seq()        # reset-on-failure: go to the tail
else:
    new_class = downgrade(ticket.access_class)   # AAAC behaviour
```

So under `none`, a client whose transfer fails is **downgraded** — HIGH to
MEDIUM — and its retry is served `reduced` instead of `full`. `none` is supposed
to be the portal as it exists today, with no adaptation of any kind, so the
control condition quietly acquires C3. This is A1 displaced into `none`: there,
payload adaptation leaks into `baseline`; here it leaks into `none`.

**Magnitude, stated honestly:** 4 of 140 clients in the run observed, so it moves
the headline numbers very little at this scale. It matters because the control
condition is supposed to be *definitionally* free of the mechanism, not
approximately free of it.

**Question for M1:** should the downgrade branch be gated on `mode == "aaac"`
rather than `!= "baseline"`, leaving `none` to re-queue without touching class?

Noted while a matrix was running; deliberately not patched mid-run, because
changing admission behaviour between runs would break the paired comparison the
whole experiment rests on.

## A12 — the capacity estimate collapses and the queue admits nobody — FIXED

C5 computes `C_max = ceil(mu_hat * W_mean)` and admits
`min(alpha * tick, C_max - in_flight, waiting)`. `mu_hat` was an EWMA with a
fixed coefficient:

```python
self.mu_hat = (0.3 * rate) + (0.7 * self.mu_hat)   # on a 1 s tick
```

That is a **~3 second memory**. A LOW client needs ~300 s to fetch a 411 KB page
over 512 kbit/s, so nearly every tick observes zero completions, `mu_hat` decays
to zero, `C_max = ceil(0 * W_mean) = 0`, and
`capacity_limit = max(0, 0 - in_flight) = 0` — the queue admits **nobody**. It
cannot recover on its own, because admitting nobody produces no completions to
raise `mu_hat` again.

**Measured on the shaped testbed, before the fix:**

| run | C_max ≤ 1 | mu_hat median |
|---|---|---|
| s1-baseline | **82%** of ticks | 0.000 |
| s1-aaac | **90%** of ticks | 0.000 |

This starved LOW in *every* mode (completion 0.041 / 0.163 / 0.143), which would
have masked any real AAAC benefit — `aaac` scored no better than `baseline`
because neither was admitting anyone.

**Resolution, in three parts — the first two were not enough on their own:**

1. **The EWMA horizon.** The coefficient is now derived as
   `control_tick_s / MU_HAT_HORIZON_S` with a 60 s horizon, so the estimator's
   memory exceeds the service time it is estimating.

2. **The unbounded cold start had to go.** `C_max` was `inf` until the first
   completion, which deadlocked in the opposite direction: if nothing completes,
   the gate never lifts, every waiting client is admitted at once, they contend
   for one shared link, and none finishes inside its window. Measured under
   `baseline`: **C_max was the inf sentinel on 736 of 736 ticks**, `in_flight`
   median 47 of 49, 1,829 admits, 1,820 timeouts, zero completions.

3. **The floor must not itself be the bottleneck.** A floor of **1 was tried and
   is wrong.** The reasoning — one client at a time gets the whole link — is true
   of the *transfer* and false of the *queue*: 49 LOW clients are then served
   sequentially through a single slot while all of them burn the same abandon
   timer. Measured under `aaac`: C_max pinned at 1 for all 637 ticks, **15 admits
   for 49 clients in 10 minutes, 47 abandoned without ever being admitted,
   1/49 completions**.

   The floor is now `origin.concurrency_limit // MIN_C_MAX_ORIGIN_FRACTION`,
   tying it to the thing `C_max` exists to protect. The binding constraint is
   the shaped link, not the origin: at the gate's measured 16 KB/s TCP goodput
   for LOW, all 49 clients sharing the link fetch `essential` (1,909 B) in
   ~5.7 s against a 50 s window, while the same 49 sharing it for `full`
   (411 KB) need ~1,231 s against a 20 s window and all miss. **Payload
   adaptation is what separates those outcomes**, so the cap must not stop
   clients reaching it. The origin was measured at max 3 in-flight with 0
   rejections, so a quarter of its limit is conservative.

**Verified on the shaped testbed.** The same probe — 49 LOW clients under
`aaac`, seed 1 — run against each state of the controller:

| controller state | C_max | LOW completion | wall clock |
|---|---|---|---|
| unbounded cold start | `inf` on 736/736 ticks | **0/49** | 658 s of retry cycles |
| floor = 1 | 1 on 637/637 ticks | **1/49** | 636 s, 47 never admitted |
| floor = `concurrency_limit // 4` = 16 | 16, `in_flight` median 15 | **45/49 = 0.918** | **72.8 s** |

`mu_hat` recovered to a median of 0.077 in the last of these, so the estimator
is tracking capacity rather than sitting at zero. For contrast, the same 49 LOW
clients under `baseline` — where the A1 gate correctly pins the payload to
`full` — complete **0/49**: 411 KB cannot cross a 512 kbit/s link inside a 20 s
window at the 16 KB/s TCP goodput the gate measured.

Two tests pin it: the EWMA arithmetic against the horizon, and the property that
`mu_hat` survives a 30-tick gap between completions. The I3 invariant test was
also rewritten — it had been recomputing `C_max` from a copy of the
implementation's formula (including a `mu_hat > 0` gate the controller itself
documents as a bug), so it compared against a number the controller never used.
It now reads `C_max` from the controller's own `CONTROL` event.

## A13 — Redis is never flushed between runs — FIXED

`ComposeRunner.reset_between_runs()` exists and issues `FLUSHALL`. Nothing ever
called it.

Every run in a matrix therefore inherited every previous run's tickets. Measured
after eight runs: **1,608 keys in Redis** — `aaac:s1-none:ticket:*` (420),
`aaac:s1-baseline:*` (280), `aaac:s1-aaac:*` (280), plus `s2-*`, `s3-none` and
even the `smoke` probe. The controller sweeps those dead tickets on every tick,
and their TIMEOUT / REQUEUE / ADMIT events are written into **whatever log is
current** — 253 foreign events landed in one measured run, inflating ADMIT
counts and corrupting attempt and timeout statistics for tickets that no longer
existed.

This is the single largest source of the run-to-run chaos seen all afternoon,
and it is invisible in any single log: the events look perfectly well-formed.

**Resolution:** `ComposeRunner.run()` now flushes Redis after the services are
up and before load starts, so the store is empty exactly when the run begins.

## A14 — the client reports a slow link as a dead admission service — FIXED

`timed_poll` defaulted to a 10 s timeout, and any `httpx.HTTPError` returns
`response=None`, which counts toward `MAX_CONSECUTIVE_ADMISSION_FAILURES` (5).
On the LOW profile — 250 ms RTT, 3% loss, 512 kbit/s shared by up to 16 admitted
clients while a 411 KB transfer is in flight — a tiny status poll routinely
exceeds 10 s, and five in a row is unremarkable.

Measured: a `none` cell returned `ADMISSION_UNAVAILABLE` for **17 of 49 LOW
clients** while HIGH and MEDIUM returned zero, against an admission service that
was demonstrably healthy (origin served 1,257 requests, 0 rejected). Those 17
sit in LOW's denominator, so the headline completion rate was being moved by a
client-side timeout.

The outcome exists precisely to separate "the infrastructure is broken" from
"this client failed", and it was conflating them.

**Resolution:** `ADMISSION_POLL_TIMEOUT_S = 45.0` and the tolerance raised to 15
— five consecutive failures is a low bar when 3% of packets are dropped by
design. `test_status_failing_repeatedly_gives_up_as_unavailable` now bounds
retries against the constant rather than a hard-coded 8, so a genuinely dead
server is still given up on quickly.

## A15 — the live dashboard shows 0 completions for classes at 100% — OPEN

M2's dashboard reads `/admin/snapshot`, whose per-class counters are keyed on
the ticket's **current** `access_class`. C4 downgrades tickets as they retry, so
a HIGH ticket that degrades to MEDIUM and completes increments the wrong class.

Observed mid-run: the dashboard showed `HIGH completed 101, MEDIUM 0, LOW 0`
while the event log for the same instant showed **HIGH 35/35, MEDIUM 56/56,
LOW 10/49** by `true_class`. 101 was every completion in the run, attributed to
one class.

This changes no measurement — every number in the report is computed from the
event log and disaggregated by `true_class` (§4.4) — but it is **actively
misleading in a live demo**, which is the one place the dashboard is used.

**Resolution:** every counter mutation in `admission/store.py` is now keyed by
`true_class` instead of `access_class` — in the admit Lua script, `create_ticket`,
`reinsert` and `complete`, for both the Redis and in-memory stores. `update_class`
no longer touches counters at all: `true_class` is immutable, so moving counts
there was the defect itself.

Nothing in the control path was affected — `get_counters()` is read in exactly
one place, `api.py:admin_snapshot`; the controller uses `waiting_count()` and
`inflight_count()` and approximates per-class from the configured mix (A5).

**Verified at runtime**, which no unit test could show, since the dashboard path
only exists against a live store. 49 LOW clients under `aaac`:

| source | completed | timed_out |
|---|---|---|
| `/admin/snapshot` | `{2: 49}` | `{2: 17}` |
| event log by `true_class` | `{2: 49}` | `{2: 17}` |

An exact match, where the same panel previously reported `HIGH 101, MEDIUM 0,
LOW 0` for a run in which HIGH was 35/35 and MEDIUM 56/56.

## A16 — `plots.py` and `report.py` cannot read real logs — OPEN

Both refuse without `--synthetic`:

> no real event logs exist yet — M1's admission service is the single writer of
> the event log (§3.8) and is not in the repository.

That guard was written before M1 was merged and is now false. Worse, the
`--synthetic` path calls `run_matrix(..., SyntheticRunner(), ...)`, which
**fabricates events into the results directory** — running either script to
"render the figures" would overwrite a real measurement with invented data.

**Worked around:** figures and `report-final.md` were produced by building an
`ExperimentResult` from the logs on disk and calling `render_all()` and
`report.render()` directly. Both render fine; only the CLI entry points are
wrong.

**Resolution:** `experiment.load_matrix(seeds, modes, results_dir)` builds an
`ExperimentResult` from `events-{run_id}.jsonl` (paired with
`origin-{run_id}.jsonl` where present), calling `single_mode()` on each log and
`_assert_identical_load` across them, so a log holding two runs or a mismatched
population is refused rather than averaged. Both CLIs now use it by default;
`SyntheticRunner` is reachable only behind an explicit `--synthetic`.

**Verified** against the seed-1 logs: `report.py --seeds 1` with no flag
reproduced the measured numbers exactly (Δ 0.7959 / 1.0000 / 0.0612; LOW 10/49,
0/49, 46/49), and a missing population still fails loudly —

    report: .../population-9.json is missing; the population is what proves
    every mode replayed the same load (§4.3)

The final figures and `report-final.md` for all three seeds were generated
through this path.

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
