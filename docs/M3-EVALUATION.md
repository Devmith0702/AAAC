# M3 — Evaluation package: what is built and why

Owner: Devmith. Scope: mock origin, netem testbed, load generation, metrics,
statistics, figures, report.

This document is the engineering record. Every non-obvious decision is stated with
the alternatives that were considered and why they were rejected. Where a choice
deviates from the brief in `CLAUDE.md`, that is called out explicitly rather than
buried.

> Project-level context is in [`PROJECT.md`](./PROJECT.md). The pre-registered
> falsification rule is in `src/aaac/evaluation/README.md`.

**State:** **223 tests passing in ~4 s** across 16 test files. `ruff` and `mypy`
clean. No test touches the network, Docker or Redis. M1's and M2's code exists on
their branches but is not yet merged; issues found reading it are in
[`INTEGRATION-ISSUES.md`](./INTEGRATION-ISSUES.md).

---

## 0. The constraint that shapes everything

M3 measures a system it does not build. That independence is the only reason the
resulting numbers are worth anything, and it produces two hard rules:

1. **M3 never modifies M1's or M2's code.** Where something of theirs is needed and
   absent, a clearly-marked local stub goes in M3's own package, tagged for
   deletion.
2. **A bad number gets reported, not fixed.** No adjusting a threshold, dropping a
   class, re-running with a different seed, or quietly widening a tolerance.

Several design decisions below exist only to make the second rule *structural*
rather than a matter of discipline — a promise you cannot break is better than one
you have to remember.

### Environment

| | |
|---|---|
| Python | 3.11.14 (venv at `.venv`; system default is 3.14, so 3.11 is pinned explicitly) |
| Runtime deps | fastapi 0.141, pydantic 2.13, httpx 0.28, uvicorn, pyyaml, pandas 3.0, matplotlib 3.11 |
| Host | macOS + Docker Desktop — containers run in a Linux VM (see §2.6) |
| Quality | pytest, ruff, mypy (non-strict) |

`pandas` and `matplotlib` are named in the contract's stack table. `pyyaml` is not,
but `configs/run.yaml` is mandated by the layout, so a YAML parser is implied —
flagged rather than assumed. **scipy is deliberately absent**; see §3.6.

---

## 1. Mock origin (`src/aaac/origin/`)

A deliberately overloadable stand-in for the results portal. Everyone is blocked on
it, so it shipped first.

```
service.py       FastAPI app: /origin/result, /origin/health
service_time.py  seeded lognormal draw
records.py       synthetic index -> name / subject / grade record
health.py        rolling-window p99 and error rate, O(1) read
sinks.py         EventSink protocol + JSONL writer for ORIGIN_SAMPLE
config.py        STUB run-config loader (delete when M1 ships common/config.py)
__main__.py      uvicorn entry point, binds 0.0.0.0:8002
```

### 1.1 Deterministic service time — the sharpest trade-off in the package

**The requirement:** runs must replay exactly given a seed.

**The problem:** service times drawn from one shared RNG *in arrival order* are not
reproducible in a concurrent server, because arrival order itself varies between
runs. Two runs with the same seed would assign different service times to the same
request.

**What is done:** each service time is derived from a hash of `(seed, index)`.

```python
def uniform01(*parts) -> float:
    digest = hashlib.blake2b(":".join(map(str, parts)).encode(), digest_size=8).digest()
    return (int.from_bytes(digest, "big") + 0.5) / 2**64

def service_time_ms(cfg, seed, index) -> float:
    z = NormalDist().inv_cdf(uniform01("svc", seed, index))
    return cfg.median_ms * math.exp(cfg.sigma * z)
```

`blake2b` rather than Python's `hash()`, which is salted per process and would make
runs irreproducible across restarts. Inverse-CDF rather than `random.lognormvariate`
so a single uniform maps to a single draw with no hidden RNG state.

| Alternative | Why rejected |
|---|---|
| Shared seeded RNG, drawn in arrival order | Not reproducible under concurrency — the actual defect being solved |
| Per-request counter mixed into the seed | Counter order varies with scheduling; same defect |
| Pre-generate a service-time table indexed by arrival number | Same ordering problem, plus O(n) memory |
| Fixed service time | Understates queueing effects and flatters the system |

**The cost, stated because it belongs in the report:** a retry for the same index
number draws the *same* service time. Service time is therefore correlated across
attempts rather than independent. It is defensible — it is the same record, so
arguably the same work — but it is a modelling choice, not a free win.

The lognormal (median 60 ms, σ 0.5) comes from the contract. Its right tail is
deliberately **not truncated**: a cap would quietly remove the slow requests that
produce realistic queueing.

### 1.2 Load shedding — the mechanism that makes `mode: none` collapse

```python
if state.waiting >= state.config.origin.queue_limit:
    state.tracker.record_rejected()
    return JSONResponse({"detail": "origin queue full"}, status_code=503)

async with _serving_slot(state):   # semaphore(concurrency_limit)
    ...
```

An `asyncio.Semaphore` bounds concurrency; an explicit `waiting` counter bounds the
queue in front of it. The check-then-increment has **no `await` between the two
lines**, which makes it atomic under asyncio's single-threaded scheduling — the
bound cannot be overshot by simultaneous arrivals. There is a test for exactly
that.

| Alternative | Why rejected |
|---|---|
| Read the semaphore's internal waiter list | Private API, and it counts waiters *after* they commit to waiting — too late to shed |
| `asyncio.Queue(maxsize=…)` of pending requests | Adds a second scheduling layer; harder to reason about who is in service vs queued |
| Rely on uvicorn's backlog / OS accept queue | Not observable, not configurable per-run, and not the thing the report describes |

This path is load-bearing, not defensive plumbing: if the origin does not collapse
under `mode: none`, the problem statement is undemonstrated. It was verified by
**mutation check** rather than by the test passing — with the queue limit raised to
1000 the same request returns 200, proving the 503 comes from the bound and not
from something incidental.

### 1.3 O(1) health, and where it is honestly approximate

The contract requires `/origin/health` to be O(1) and never to block on the request
semaphore. A true rolling p99 is not O(1).

**What is done:** a ring of five 1-second buckets, each holding a fixed 128-bucket
log-spaced latency histogram spanning 0.5 ms – 60 s (growth ratio ≈ 1.096, so about
9.6% resolution). Constant work per observation, constant work per read.

**The reported p99 is the upper edge of the containing bucket** — so it never
understates latency. That direction is chosen deliberately: understating p99 would
flatter the origin exactly when it is struggling, and M1's AIMD controller consumes
this number.

| Alternative | Why rejected |
|---|---|
| Keep a sorted list / deque of durations | O(n log n) per read and unbounded memory under load |
| Reservoir sampling | O(1), but the sample under-represents the tail — wrong for a p99 |
| t-digest / HDRHistogram | Accurate and O(1), but a new dependency outside the contract stack for ~9.6% of extra precision |
| numpy percentile over a ring buffer | Still O(n) per read, and pulls numpy into the origin service |

Three counters are kept apart on purpose:

- **served** — enters the histogram.
- **rejected** — a 503, counted as an error but **excluded from the histogram**. A
  rejection completes in microseconds; folding those in would drag p99 *down*
  precisely when the origin is most overloaded.
- **abandoned** — the client disconnected mid-service. Counted separately and
  treated as neither an error nor a latency sample: the origin did nothing wrong,
  and timed-out clients are an expected feature of this experiment.

`err_rate_1s` reads only **fully elapsed** buckets. A partially-filled current
bucket reads spuriously low, which would tell the controller the origin is healthy
at the exact moment it starts failing. The cost is up to one second of lag, which
is acceptable at a 1 Hz control tick.

`/origin/health` returns **exactly** the three contracted keys. Richer diagnostics
(`waiting`, `served`, `rejected`, `p50_ms`) go onto `ORIGIN_SAMPLE` instead, so
M1's consumer sees a stable shape.

### 1.4 The `ORIGIN_SAMPLE` writer path

The contract gives the event log a single writer — the admission service — but
origin samples originate in M3's process. This was an open question.

**Resolution:** the origin emits through an `EventSink` protocol; the default
implementation writes a **separate** `results/origin-{run_id}.jsonl` that the
analysis joins to M1's log on timestamp.

| Alternative | Why rejected / deferred |
|---|---|
| M1 exposes an internal ingest endpoint | Requires a contract change and work in someone else's package to unblock mine |
| Origin writes directly into M1's event log | Breaks the single-writer guarantee and risks interleaved partial lines |
| Buffer in memory, hand over at run end | Loses everything if a run crashes — and crashes are data |

The known cost is **clock skew between containers**, which is why
`EventLog.clock_skew_warning()` exists: it reports when the two logs do not overlap
as expected rather than silently mis-attributing origin state to the wrong phase of
a run. Swapping to an HTTP ingest sink later replaces one class, not the service.

Writes go through an `asyncio.Queue` drained by a background task using
`asyncio.to_thread`, so no request handler ever blocks on disk I/O. Queue overflow
is **counted, not ignored** — a silently lost sample is a hole in the instrument.

### 1.5 One deliberate contract exception

The contract says "no sleeping in request handlers; everything async." The origin's
entire purpose is to simulate service time, so it does `await asyncio.sleep(...)`.
The rule forbids *blocking the event loop*; `asyncio.sleep` yields. Noted in the
module docstring so it reads as a decision rather than an oversight.

---

## 2. Testbed (`src/aaac/evaluation/testbed/`)

### 2.1 Ingress shaping is mandatory, not optional

**This is the most consequential finding in the package.**

The brief describes egress shaping and calls ingress shaping optional, "if
downstream asymmetry matters."

It matters absolutely. The thing a LOW-class student cannot finish is a ~450 KB
**download**. A `tbf` on the container's egress caps what the client *uploads* and
leaves the download running at full VM speed. Under egress-only shaping the LOW
class is not slow, the completion gap never appears, and **the testbed silently
measures nothing** — while looking like it worked.

`netem.sh` therefore shapes ingress by default, redirecting `eth0` ingress to an
`ifb` device and applying `tbf` + `netem` there:

```sh
ip link add ifb0 type ifb && ip link set ifb0 up
tc qdisc add dev eth0 handle ffff: ingress
tc filter add dev eth0 parent ffff: protocol all u32 match u32 0 0 \
    action mirred egress redirect dev ifb0
tc qdisc add dev ifb0 root handle 1: tbf rate $RATE burst $BURST latency 400ms
tc qdisc add dev ifb0 parent 1:1 handle 10: netem \
    delay $DELAY $JITTER distribution normal loss $LOSS
```

Delay is applied on **one path only**, so measured RTT ≈ configured delay — which
is what the verification gate asserts. (`AAAC_NETEM_DIRECTION=both` halves the
delay across the two paths to preserve that identity.)

If the `ifb` kernel module is unavailable, the script **fails loudly** rather than
falling back to egress-only. A run that cannot shape downstream is not a valid
measurement of the completion gap, and silently producing one is worse than
producing none.

### 2.2 Per-class burst — a deviation from the brief's snippet

The brief's example uses `burst 32kbit` for every class. A token bucket cannot
reach its configured rate when burst is below roughly `rate ÷ HZ`; at 50 Mbit with
HZ = 1000 that floor is about 50 kbit. **A literal `32kbit` caps the HIGH class near
30 Mbit and the verification gate would fail on correctly applied shaping.**

Burst is therefore per class — 256 / 64 / 32 kbit for HIGH / MEDIUM / LOW — and
recorded in `profiles.py` so the report states what was actually applied. There is
a test asserting `burst_kbit ≥ rate_kbps / 1000` for every profile.

### 2.3 The rate gate uses UDP, and why that is not hedging

The brief asks that a timed probe or iperf3 land within 10% of the configured rate.
**Taken with a TCP measurement that gate is unachievable for the LOW profile** — and
not because the shaping failed.

TCP throughput over a lossy, high-latency path is bounded by roughly
`MSS / (RTT · √loss)`. At 250 ms and 3% loss, with MSS 1460, that is ≈270 kbit/s
against a configured 512 kbit/s. A TCP test would report ~53% of target — a 47%
shortfall — on a perfectly shaped link, and fail the gate every time.

So two measurements, with different jobs:

- **UDP downstream throughput gates the shaper.** It bypasses congestion control and
  measures what `tbf` actually admits — the thing being verified. Offered at 1.5×
  the configured rate so the shaper, not the sender, is the binding constraint.
- **TCP downstream goodput is reported alongside as informational**, with the Mathis
  prediction next to it.

The TCP number is not a footnote. That the LOW link delivers roughly half its
nominal capacity to a real transfer is a large part of *why* a 450 KB page does not
fit in a short admission window, and it belongs in the body of the report.

### 2.4 Loss is gated by a confidence interval, not a flat tolerance

A 10% relative check on 0.01% loss would need on the order of millions of packets.
Applying one anyway would be a fake gate that always passes.

Instead the configured loss must fall inside the **95% Wilson score interval** of
the measurement, and a check is only marked *gating* when the interval is narrow
enough to be informative:

```python
in_ci = ci_low <= profile.loss_pct <= ci_high
informative = (ci_high - ci_low) <= max(0.5 * profile.loss_pct, 0.05)
```

For LOW (3%) at 2,000 packets this gates properly. For HIGH (0.01%) it is reported
as informational with the interval printed, which is honest about what the
measurement can and cannot support.

Wilson rather than the normal approximation because the latter is badly behaved for
small `p` and small `n` — it can produce a lower bound below zero.

### 2.5 Single source of truth for the profiles

`profiles.py` holds the three link profiles; `docker-compose.yml` mirrors them into
environment variables because Compose cannot import Python. **A test parses the
compose file and asserts the two agree.** Drift between them would mean the report
describes a testbed that was never run — which is exactly the class of error that
is invisible until someone asks a hard question in a viva.

`iperf3` and `ping` run inside the containers rather than using M2's `/probe`
endpoint, so the testbed is verifiable independently of M2's service.

### 2.6 Known environmental limitation

Docker Desktop on macOS runs containers in a Linux VM, so `tc` shaping is
second-hand and adds an uncontrolled virtual hop. This makes the verification gate
non-optional rather than a nicety, and it is a limitations-section entry. Nothing
in the testbed is Docker-Desktop-specific, so moving to a Linux host later requires
no code change.

**Not yet verified end to end.** Docker Desktop is now available (29.4.3,
linux/arm64), but `make up` + `make verify-testbed` has not yet been run.

---

## 3. Evaluation chain (`src/aaac/evaluation/`)

### 3.1 Population — identical load, enforced rather than promised

The comparison depends entirely on all three modes seeing the same load. Three
mechanisms make that structural:

**Mode is invisible.** `population.py` and `loadgen.py` never receive `mode`, never
read it from config, and never branch on it. **A test parses both modules' ASTs and
asserts the identifier `mode` does not appear** outside docstrings. A guarantee you
cannot violate beats one you have to remember.

**Class counts are apportioned, not sampled.** Largest-remainder apportionment gives
exactly 250 / 400 / 350 from a 1,000-client mix of 0.25 / 0.40 / 0.35, every seed.
Sampling the mix would give each seed slightly different class counts, adding
variance that has nothing to do with the system under test.

**Every population carries a SHA-256 hash** of its client list, recorded on every
run. The harness aborts if two runs of the same seed disagree, and `load()` refuses
a file whose contents no longer match its hash. This turns "all modes replayed the
same load" from an assumption into something the analysis can *prove*.

Arrivals are a mixture: with probability `burst_fraction` (0.8) a Gaussian burst at
`burst_center_s ± burst_sigma_s`, otherwise an exponential tail with mean
`tail_decay_s`. **`burst_fraction` is an addition** — the brief specifies both
components but not their relative weight, and a mixture needs one. It lives in
config so it is a stated parameter rather than a constant buried in code.

Class labels are shuffled independently of arrival times, so class does not
correlate with arrival order — otherwise the burst would hit one class first and
confound class with timing.

### 3.2 Reading the log — the only route from data to number

`events.py` is the sole path from the event log to a metric. It never reads live
process state.

An event type outside the closed vocabulary raises `LogIntegrityError` rather than
being skipped, as does malformed JSON. Skipping would mean computing a metric from
a log that is quietly missing decisions.

`single_mode()` refuses a log containing more than one mode — that would mean runs
were interleaved or logs concatenated, and every aggregate downstream would mix
conditions without saying so.

### 3.3 Metrics — provenance travels with every number

Two rules are enforced by structure, not discipline.

**Everything is disaggregated by `true_class`**, and grouping uses the netem profile
actually configured — never the estimated class. Grouping by the estimate would
measure the classifier instead of the system. There is a test where a LOW client
misclassified as HIGH must still count as LOW.

**Every result carries a `Provenance` record:** which event types it came from, how
many tickets were included, how many excluded and why, how many non-completers were
censored, and what it was disaggregated by. Exclusions are counted by reason and
travel into the report — nothing is discarded silently.

Specific decisions:

- **Time to completion is censored, not filtered.** It is defined over completers
  only, so the number of non-completers is printed beside it every time. A median
  that improves because the slow clients never finished is not an improvement.
- **Goodput includes bytes burned on timed-out attempts.** Those burned bytes are
  the entire cost of the retry loop — the thing that makes LOW diverge. Excluding
  them would make goodput ≈ 1.0 for everyone and delete the most damning number in
  the evaluation.
- **Attempt counts are taken over completed tickets**, per the brief; counting over
  all tickets would mix in clients still queued when the run ended.
- **Optimistic classifier error is `estimate < true`**, not merely LOW→HIGH. Because
  `HIGH=0 < MEDIUM=1 < LOW=2`, that is precisely "judged more capable than it is" —
  the error that sends a slow client a payload it cannot fetch.
- **Origin stability averages per-sample `err_rate_1s`; it does not sum window
  totals.** Consecutive `ORIGIN_SAMPLE` events describe overlapping 5-second
  windows, so summing them would multiply-count by roughly 5×. The 1-second buckets
  are disjoint, so their mean is a legitimate run-level rate.
- **Δ is `None`, not `0.0`, when HIGH or LOW has no joined tickets**, with a warning.
  Zero would read as "perfectly fair"; `None` reads as "no data", which is the truth.
  `quantile([])` returns `None` for the same reason — `0.0` would read as "fast".

Three warnings exist because of what M1's real log does and does not carry (checked
against `origin/thisaru`, pinned by `tests/test_metrics_m1_log_shape.py`). None
changes a number; each says what a number cannot support:

- **Goodput is flagged as an upper bound** when `TIMEOUT` events carry no `bytes`.
  Absent is not zero — the burned bytes are unknown, not nil.
- **Classifier coverage is stated** when some tickets have no `ESTIMATE`. Rejected
  estimates are not logged, so unclassified and rejected cannot be told apart.
- **Completion without an `ok` field is disclosed as an inference.** M1 logs
  `ok=false` as `ABANDON`, so `COMPLETE` implies success — for as long as that
  convention holds.

The estimated class is read from `ESTIMATE` only, never from `JOIN`: M1 logs `JOIN`
with a MEDIUM placeholder before any classification has happened.

### 3.4 Statistics without scipy

The contract's stack is pandas + matplotlib. scipy is not on it, so the required
distributions are implemented directly (~290 lines) rather than adding a dependency
outside the agreed stack.

- **Regularised incomplete beta** via the modified Lentz continued fraction, which
  gives the Student-t CDF as `I_{df/(df+t²)}(df/2, ½)`.
- **`t_ppf`** by bisection on the CDF — 200 iterations, accurate to ~1e-10, which is
  far beyond what five seeds justify.
- **Validated against published critical values**: t₀.₉₇₅ at df = 1, 2, 4, 9, 29, 100
  match to within 1e-3 (12.7062, 4.3027, 2.7764, 2.2622, 2.0452, 1.9840).
- **Percentile bootstrap** as the alternative when differences are visibly skewed,
  seeded so it replays.

| Alternative | Why rejected |
|---|---|
| Add scipy | New runtime dependency outside the contract; needs team agreement for ~50 lines of well-understood numerics |
| Hard-coded t-table for df 1–30 | Works for the planned design but breaks silently on a sensitivity sweep with a different seed count |
| Normal approximation instead of t | Materially wrong at df = 4, which is exactly the planned design |

### 3.5 The Wilcoxon problem

The brief offers "a paired t-test or Wilcoxon signed-rank" with "minimum 5 seeds".
**Those two do not combine.**

The exact two-sided signed-rank test on n = 5 pairs has 2⁵ = 32 equally likely sign
assignments, so the smallest attainable p-value is 2/32 = **0.0625**. Wilcoxon
cannot reject at α = 0.05 with five seeds regardless of effect size.

`wilcoxon_min_achievable_p(n)` makes this explicit, the test attaches the warning to
its own result, and **`report.py` withholds the p-value entirely** rather than
printing one that could never have rejected. The exact test enumerates all 2ⁿ sign
assignments for n ≤ 20 (32 combinations at the planned scale — trivial), falling
back to the normal approximation beyond.

Either the paired t-test is accepted as primary — which is what the paired design
points at anyway — or the run needs ≥ 6 seeds. That is a team decision, raised as Q9.

### 3.6 Falsification as executable code

`falsification.py` is the pre-registered rule in mechanical form. Two properties
matter more than the arithmetic:

**Every condition reports its own verdict.** A single overall boolean would let a
near-miss on origin stability disappear behind a large effect on Δ.

**The NOT SUPPORTED branch is tested against fabricated null data**, as the brief
requires — on data where AAAC does nothing, where it helps but too noisily to call,
where it makes the gap worse, where it sacrifices origin stability, and where it
degrades HIGH-class completion time. Reporting a negative result is therefore
*known* to work rather than hoped to.

The 20% HIGH-class margin is a named module constant, so changing it shows up in a
diff rather than hiding inside a comparison.

The rule also detects the trade-off the brief flags directly: if aggregate
completion falls while Δ narrows, a note is attached saying the gap may have closed
by making other classes worse, and that this belongs in the body of the report.

### 3.7 Fabricated data, quarantined

`synth.py` generates realistic event logs so the analysis chain and the
falsification branch are testable before M1 and M2 exist. Every safeguard is about
one risk: a synthetic number reaching the write-up.

- `synth.write()` **refuses** to write to any filename not prefixed `synthetic-`.
- `.gitignore` excludes `synthetic-*.jsonl`.
- `report.py` opens with a prominent banner when any source log is synthetic.
- `experiment.py --synthetic` prints a warning block before it runs.

Its event shapes follow §3.8 rather than M1's shipped log — for example it puts
`bytes` on `TIMEOUT`, which M1 does not. That is left alone until A2 in
`INTEGRATION-ISSUES.md` settles how failed attempts are logged; the real shapes are
tested separately in `tests/test_metrics_m1_log_shape.py`.

### 3.8 Harness and load generation — failing loudly where blocked

`experiment.py` uses a pluggable `Runner`. `SyntheticRunner` works today.
`ComposeRunner` raises a message naming exactly what is missing — M1's admission
service, M2's SDK and delivery service, wired into compose — rather than producing
something that resembles a measurement.

`loadgen.py` schedules and supervises but **never speaks HTTP**. Each client is one
call to M2's `aaac.client.sdk.run_client`, bound by `load_sdk_runner`, which raises
`SdkUnavailableError` if the SDK cannot be imported instead of substituting a local
client. Two independent client implementations would let baseline and AAAC differ
for reasons neither owner controls, so refusing is the point.

- The estimator settings `run_client` needs are read from M2's `estimator:` config
  section and are **never defaulted** in M3's code; a copy of someone else's numbers
  drifts. `abandon_after_s` comes from the replayed population.
- `poll_jitter_frac` is left at the SDK's default. Spreading the herd would change the
  experiment, which is not the load generator's call.
- `run_client` never raises; it returns a labelled outcome. A client counts as ok
  only on `COMPLETED`. `ADMISSION_UNAVAILABLE`, `TICKET_UNKNOWN`,
  `COMPLETED_UNREPORTED` — and the SDK raising anyway — trigger
  `LoadReport.infrastructure_warning()`: those are failures of the run, not outcomes
  of the system. These tallies are supervision only; no metric is computed from them.

`ClientRecord.launch_lag_s` records how late each client actually started against its
scheduled arrival, and `LoadReport.lag_warning()` fires when p95 lag exceeds a
second. If the machine cannot keep up with 20k clients, that ceiling shows up as a
warning rather than being absorbed into a flatter arrival curve that nobody notices.

### 3.9 Figures

The three-mode palette (`#0072B2` / `#E69F00` / `#009E73`) was run through a
colour-vision-deficiency validator rather than chosen by eye: worst adjacent pair is
ΔE 11.4 under protanopia and 24.2 under normal vision, against a target of ≥ 8. The
amber falls below 3:1 contrast on white, which obligates secondary encoding — so
every bar carries a direct value label and `report.py` prints the same numbers as a
table.

Two layout rules with reasons:

- **No dual-axis charts anywhere.** Figure 4 shows a rate, a rate, a count and a
  latency; forcing them onto two y-scales would make their relative movement
  meaningless. It is drawn as stacked small multiples sharing one time axis.
- **Sequential means one hue, light to dark.** The confusion matrix uses a single
  blue ramp; a rainbow would imply an ordering among classes the counts do not have.

Panels for series M1 has not emitted yet (α, μ̂ from `CONTROL` events) render empty
and **labelled** "not emitted in this run" rather than as a flat line at zero, which
would read as a real measurement.

Line width thins as sample density rises — a legibility choice, applied to the
stroke and never to the data; no smoothing is performed anywhere.

### 3.10 Report

`report.py` emits `results/report-{run_id}.md` so the presentation is assembled from
generated numbers and nothing is hand-typed. It refuses three things:

1. printing an aggregate-only per-class metric;
2. hiding exclusions — every table is followed by its provenance block;
3. presenting a Wilcoxon result it could never have rejected.

---

## 4. Testing strategy

223 tests, none touching the network, Docker or Redis. The suite is weighted toward
the failures that would invalidate results rather than the ones that would crash the
code. Timing-sensitive tests run on a manual clock rather than real sleeps.

| Area | What is actually protected |
|---|---|
| `test_origin_service.py` | Health stays responsive while every slot is occupied — the failure that blinds M1's controller |
| `test_origin_health.py` | Reported p99 never *understates* the true p99 |
| `test_origin_config.py` | `run.yaml`'s M3 sections hold only §3.9 keys, so M1's strict loader accepts the file |
| `test_testbed_profiles.py` | `docker-compose.yml` has not drifted from `profiles.py` |
| `test_population.py` | AST check that load generation cannot see `mode`; tampered population files are refused |
| `test_loadgen.py` | Identity passes to the SDK unchanged; only `COMPLETED` is ok; infrastructure failures are flagged, not absorbed |
| `test_events_metrics.py` | Grouping uses `true_class`; censoring is counted; goodput includes burned bytes |
| `test_metrics_m1_log_shape.py` | The analysis reads M1's real event shapes, and discloses what they cannot support |
| `test_falsification.py` | The NOT SUPPORTED branch fires on five distinct kinds of null result |
| `test_experiment_report.py` | All modes within a seed replay one population; the report banners fabricated data |

Two verifications were done outside the suite because passing tests are not the same
as working software:

- **Mutation check on load shedding** — raising the queue limit to 1000 makes the
  same request return 200, proving the 503 comes from the bound.
- **Live smoke test** of the origin under real uvicorn: p50 65.9 ms against a
  configured 60 ms median, p99 181 ms against a theoretical 192 ms, `ORIGIN_SAMPLE`
  written at 1 Hz.

---

## 5. Local stubs — scheduled for deletion

Both carry a banner at the top of the file.

| File | Stands in for | Delete when |
|---|---|---|
| `origin/config.py` | `common/config.py` | M1 ships the real loader |
| `evaluation/access_class.py` | `common/classes.py` | M1 ships the real enum |

`access_class.py` transcribes the contract's enum **verbatim**. If the real one ever
differs, the contract has been broken — that is a conversation, not an edit to the
stub.

---

## 6. Deviations from the brief, collected

Each of these contradicts something written in `CLAUDE.md`, and each is defended
above rather than applied quietly.

| Deviation | Section |
|---|---|
| `burst 32kbit` replaced with per-class burst | §2.2 |
| Rate gate measures UDP, not TCP | §2.3 |
| Loss gated by confidence interval, not a flat 10% | §2.4 |
| Ingress shaping treated as mandatory, not optional | §2.1 |
| Wilcoxon withheld at 5 seeds | §3.5 |
| `burst_fraction` introduced (0.8) — a code default, not a `run.yaml` key | §3.1 |
| `health_window_s`, `health_buckets`, `sample_interval_s` — code defaults, not `run.yaml` keys | §1.3 |
| `pyyaml` added as a runtime dependency | §0 |

The parameters were first added to `run.yaml`, then moved out: M1's loader rejects any
key outside §3.9, so an M3-owned section is still shared once someone else validates
it. See `INTEGRATION-ISSUES.md` A3.

---

## 7. Not built, and why

| | Blocked on |
|---|---|
| Testbed verified end to end | Nothing — Docker is available; `make up` + `make verify-testbed` not yet run |
| Admission and delivery services in `docker-compose.yml` | Merge of M1's and M2's branches |
| `ComposeRunner` | Merge, plus the compose wiring above |
| Stage 1 — collapse reproduced and measured | Testbed verification, merge |
| Stage 2 — Δ under the access-blind baseline | `INTEGRATION-ISSUES.md` A1 and A2 |
| Retiring the `config.py` / `access_class.py` stubs | Merge |
| `contract-guard`, `result-integrity`, `testbed-verify` skills | Awaiting review of each `SKILL.md`; M2 already has a `contract-guard` skill on its branch |
| Sensitivity sweep (`class_mix` LOW at 0.20/0.35/0.50, `w_base_s` at 10/20/40) | Main result must exist first |

Resolved since the first draft: `CONTROL` carries `alpha` and `mu_hat` under exactly
the names figure 4 reads, and `.gitattributes` now forces LF line endings.

`ComposeRunner` and `load_sdk_runner` each raise a message naming precisely what is
missing, so none of this can be mistaken for working.
