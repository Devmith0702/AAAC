# AAAC — the system, in technical detail

Access-Aware Admission Control: a virtual waiting room that adapts to the
quality of each waiting client's network link, and the apparatus that measures
whether doing so is fair.

This document is the engineering reference for the assembled system. It assumes
you can read Python but knows nothing else about the project. Read
[`PROJECT.md`](./PROJECT.md) first if you want the motivation without the
mechanism; read [`M3-EVALUATION.md`](./M3-EVALUATION.md) for the evaluation
package's design record, and [`INTEGRATION-ISSUES.md`](./INTEGRATION-ISSUES.md)
for what is currently known to be wrong. `CLAUDE.md` §3 is the binding interface
contract and overrides this document wherever the two disagree.

---

## 1. The problem, stated precisely

A national exam results portal on release day. Hundreds of thousands of students
arrive inside a few minutes. The server saturates.

The standard mitigation is a virtual waiting room: a queue in front, admitting
people at a rate the origin survives. It protects the server. The claim under
test is that it does not protect people evenly.

Two students are admitted with the same fixed window:

| | Urban fibre | Congested rural cell |
|---|---|---|
| Bandwidth | 50 Mbit/s | 512 kbit/s |
| RTT | 15 ms | 250 ms |
| Loss | 0.01% | 3% |
| 450 KB page | well under a second | does not finish |

The second student times out, returns to the back of the queue, and retries — at
exactly the same cost, because nothing adapted. The loop diverges.

It is worse than the headline numbers suggest. TCP throughput over a lossy,
high-latency path is bounded by roughly

    BW ≈ MSS / (RTT · √p)

At MSS 1460, RTT 250 ms and p = 3%, that ceiling is **≈ 270 kbit/s** — about
half the link's nominal 512 kbit/s. A "512 kbit/s student" is really a
270 kbit/s student once loss enters. This is why the testbed's rate gate cannot
use TCP (§7.2) and why a smaller payload, not merely a longer window, is the
decisive intervention.

### The headline number

    Δ = completion_rate[HIGH] − completion_rate[LOW]

The fraction of well-connected users who finish minus the fraction of
badly-connected users who finish, both disaggregated by the **true** network
profile the testbed configured — never by the class the system estimated.
Δ = 0 means the queue treated everyone alike. Every component exists either to
shrink Δ (M1, M2) or to measure it honestly enough that the shrinking could be
disproved (M3).

---

## 2. The five components

| | Owner | Idea | Lives in |
|---|---|---|---|
| **C1** | M2 | Estimate link quality from traffic the client already generates | `estimator/` |
| **C2** | M1 | Adaptive admission window — slower links get longer slots | `admission/window.py` |
| **C3** | M2 | Payload adaptation — slower links get a smaller page | `delivery/` |
| **C4** | M1 | Non-regressive re-queue — a timed-out client keeps its place, and its next attempt is cheaper | `admission/requeue.py` |
| **C5** | M1 | Capacity-tracking admission rate (AIMD) so the origin never dies | `admission/controller.py` |

They compose: C1 classifies the link, C2 gives that class more time, C3 gives it
less to download, C4 stops a failure from costing the client its position, and
C5 keeps the total inside what the origin can serve.

C3 does most of the conceptual work. A longer window helps a 270 kbit/s client;
a 6 KB page instead of a 450 KB page changes the arithmetic outright.

### Access classes

```python
class AccessClass(IntEnum):   # ordering matters
    HIGH = 0
    MEDIUM = 1
    LOW = 2
```

`MEDIUM` is the default whenever classification is unavailable or
low-confidence, so a failure of C1 degrades to ordinary behaviour rather than to
a wrong decision.

The ordering is load-bearing. Because `HIGH < MEDIUM < LOW`, an estimate
*numerically lower* than the truth is a client judged **more capable than it
is** — the error that sends a slow student a payload they cannot fetch. That
asymmetry is reported separately from raw accuracy everywhere it appears
(`optimistic_error_rate`).

---

## 3. Topology

Three HTTP services plus Redis on one Docker network.

```
                    ┌──────────────┐
   shaped client ──▶│  admission   │  M1  :8000   queue, window, re-queue, AIMD
   (netem/tbf)      │              │──┐
                    └──────┬───────┘  │ polls /origin/health every control tick
                           │          ▼
                           │   ┌──────────────┐
   shaped client ──▶┌──────────│    origin    │ M3  :8002
                    │ delivery │◀─────────────┘
                    │  :8001   │  M2          overloadable stand-in
                    └──────────┘
```

| Service | Owner | Port | Endpoints |
|---|---|---|---|
| admission | M1 | 8000 | `POST /queue/join`, `POST /queue/estimate`, `GET /queue/status/{tid}`, `POST /queue/complete`, `GET /admin/snapshot`, `GET /admin/stream` |
| delivery | M2 | 8001 | `GET /probe/{n_bytes}`, `GET /result?token=&index=`, `GET /static/{asset}` |
| origin | M3 | 8002 | `GET /origin/result?index=`, `GET /origin/health` |

`/static/*` is not in the §3.5 table. It arrived with the contract change that
made `full` reference real sub-resources, so its cost includes round trips and
not only bytes.

### Environment

| Service | Reads |
|---|---|
| admission | `AAAC_CONFIG_PATH`, `AAAC_RUN_ID`, `AAAC_REDIS_URL` (in-memory store if unset), `AAAC_ORIGIN_URL`, `AAAC_RESULTS_DIR`, `AAAC_TOKEN_SECRET` |
| delivery | `AAAC_CONFIG_PATH`, `AAAC_ORIGIN_BASE`, `AAAC_ORIGIN_TIMEOUT_S`, `AAAC_TOKEN_SECRET` |
| origin | `AAAC_CONFIG`, `AAAC_RUN_ID`, `AAAC_MODE`, `AAAC_SEED`, `AAAC_RESULTS_DIR` |

Note `AAAC_CONFIG` vs `AAAC_CONFIG_PATH`: the same file under two names, because
M1's loader and M3's read different variables. `docker-compose.yml` sets both on
the admission service so neither depends on the other's spelling
(`INTEGRATION-ISSUES.md` A7).

---

## 4. One client, end to end

The sequence every client follows, in every mode. **Mode never changes the code
path** — it changes M1's behaviour behind the same protocol.

1. `POST /queue/join {client_id, true_class}` → `{ticket_id, join_seq, position, eta_s, poll_interval_ms}`.
   The ticket is created `WAITING` with `access_class = MEDIUM` as a placeholder.
   `true_class` is an opaque label for M3's scoring; nothing in M1 or M2 may
   branch on it.
2. **Probe (C1).** One timed `GET /probe/65536` against delivery. Incompressible
   random bytes, `Cache-Control: no-store`, so gzip cannot distort the
   measurement. Throughput is `bytes × 8 / ms`.
3. **Poll.** `GET /queue/status/{tid}` every `poll_interval_ms`. The response is
   tiny, so its round trip approximates RTT — that *is* the RTT mechanism; a
   separate ping would be traffic the client would not otherwise generate.
4. **Estimate (C1), once.** As soon as ≥ `min_rtt_samples` (5) usable RTTs
   exist, the client `POST`s a `LinkEstimate` to `/queue/estimate`. Later
   attempts do not re-estimate: after the first, class comes from M1's downgrade
   policy.
5. **Admission.** The controller admits `n` tickets per tick, stamps
   `expires_at = now + W(class)`, and the next status poll returns state
   `ADMITTED` with an `admit_token` and `window_s`.
6. **Fetch (C3).** `GET /result?token=…&index=…` against delivery, then every
   sub-resource the document references, in parallel (≤ 6 connections, imitating
   a browser). The variant comes from the verified token and nowhere else.
7. `POST /queue/complete {ticket_id, ok, bytes, duration_ms, variant}`.

If the window expires mid-transfer, the fetch aborts and the client reports
failure; the controller's sweep expires the ticket and C4 re-queues it.

### Mode blindness

`none`, `baseline` and `aaac` must run through identical client code, or the
three-way comparison is invalid. Mode appears in nothing the client can read:
`/queue/join` returns no mode, `TicketStatus` has no mode field, and the token
payload carries `tid/cls/att/exp/var`. `tests/client/test_mode_blind.py` walks
the AST of every module in `client/` and fails if the identifier `mode`, or any
mode name as a string literal, appears outside a docstring.

`none` is implemented as an immediately-`ADMITTED` ticket with a large window,
so the client still joins, polls and completes exactly as in the other modes.

---

## 5. M1 — admission

### 5.1 Queue store

Two implementations behind one `Protocol`: `RedisQueueStore` (used when
`AAAC_REDIS_URL` is set) and `InMemoryQueueStore` (everything else, including
unit tests, which may not touch Redis).

State per run:

    aaac:{run}:seq                INCR, the monotonic join sequence
    aaac:{run}:waiting            ZSET, score = join_seq
    aaac:{run}:inflight           ZSET, score = expires_at
    aaac:{run}:ticket:{tid}       HASH: state, attempt, class, true_class, join_seq, expires_at
    aaac:{run}:counters           HASH: waiting:{c}, completed:{c}, timed_out:{c}

Admission is a **Lua script**, because it must be atomic across keys: `ZPOPMIN`
n tickets from `waiting`, and for each one read its class, compute
`expires_at = now + window[class]`, set state `ADMITTED`, add it to `inflight`
at that score, and decrement the per-class waiting counter. Doing this in
round-trips would let a concurrent sweep see a ticket in neither set.

Expiry is likewise a script: `ZRANGEBYSCORE inflight -inf now` followed by
`ZREMRANGEBYSCORE`, so a ticket cannot be expired twice.

### 5.2 C2 — the adaptive window

```python
W(c) = min(w_base_s · kappa[c], w_max_s)     # aaac
W(c) = w_base_s                              # baseline, none
```

With the shipped config: `w_base_s = 20 s`, `kappa = {HIGH 1.0, MEDIUM 1.5,
LOW 2.5}`, `w_max_s = 60 s` — so 20 / 30 / 50 seconds. The window is enforced
in two independent places: as `exp` inside the HMAC token, which delivery
refuses once stale, and as the score in the `inflight` ZSET, which the sweep
reads. Neither alone is sufficient — the token stops a late fetch, the ZSET
frees the slot.

### 5.3 C4 — non-regressive re-queue

On expiry, `handle_timeout` emits `TIMEOUT`, then:

- **`baseline`** — reset-on-failure: `score = next_seq()`, i.e. the tail of the
  queue, class unchanged. This is the divergent loop the project is about, and
  it is the honest comparator.
- **`aaac`** — non-regressive: the score (`join_seq`) is **preserved**, and the
  class is downgraded one step (`HIGH → MEDIUM → LOW`, `LOW` is the floor). At
  `attempt ≥ max_attempts` the class is forced straight to `LOW` and the
  `DOWNGRADE` event carries `forced_floor=true`.

Then `REQUEUE` is emitted with the new position.

**Starvation.** Preserving `join_seq` means a repeatedly-failing client is
re-served ahead of later arrivals. That is deliberate and bounded: each attempt
is strictly cheaper than the last (smaller payload, longer window), so the
expected number of re-serves per ticket is small and decreasing.

`TIMEOUT` deliberately carries **no `bytes`**. Delivery never reported to the
admission service, so the bytes burned are unknown to it, and `bytes: 0` would
be a zero that reads as data. The consequence for goodput is disclosed in §9.

### 5.4 C5 — the AIMD controller

One tick per `control_tick_s` (1 s):

1. **Capacity estimate.** `mu_hat ← 0.3·rate + 0.7·mu_hat`, where `rate` is
   completions observed this tick — an EWMA of service rate.
2. **Rate control.** Poll `GET /origin/health`. Healthy means
   `p99 < target_origin_p95_ms` **and** `err_rate_1s < target_origin_err_rate`.
   Healthy → `alpha += alpha_increase` (additive increase);
   unhealthy → `alpha *= alpha_decrease` (multiplicative decrease);
   clamped to `[alpha_min, alpha_max]`. A failed or non-200 health poll is
   treated as maximally unhealthy (`p99 = inf`, `err_rate = 1.0`), so blindness
   backs the controller off rather than letting it run open-loop.
3. **Concurrency cap.** `C_max = ceil(mu_hat · W_mean)` — Little's law, with
   `W_mean` the load-weighted mean window over waiting clients. Until the first
   real completion, `C_max` is held unconstrained: gating on `mu_hat > 0` was a
   bug, because the EWMA decays geometrically and never returns to exactly zero,
   so the bypass never re-engaged and `C_max` pinned at 1.
4. **Admit.** `n = min(alpha · tick, C_max − in_flight, waiting)`, then one
   `admit_n` call, one `ADMIT` event per ticket, and a `CONTROL` event carrying
   `alpha`, `mu_hat`, `in_flight`, `C_max` and `origin_p99`.

`W_mean` is currently approximated from the **configured** class mix rather than
the live per-class counters, which is a known issue: the controller is handed
the population's ground truth, which a real portal would not have
(`INTEGRATION-ISSUES.md` A5).

### 5.5 The admit token

Compact HMAC-SHA256: `b64url(payload) + "." + b64url(sig)`, payload
`{"tid", "cls", "att", "exp", "var"}` with `var ∈ {full, reduced, essential}`.
M1 signs, M2 verifies, M3 never parses one.

This is the trust boundary of C3. The variant is derived from the class **at
signing time** and travels inside the signature, so a client cannot request a
bigger payload by editing a URL. Delivery rejects a bad signature with 401 and
serves nothing — no fallback to a default variant, since an unverifiable token
gets nothing. Verification also rejects `exp` in the past, which is what stops a
late fetch from consuming origin capacity after the window closed.

---

## 6. M2 — estimator, delivery, client

### 6.1 C1 — in-band link estimation

Four raw signals, all from traffic the client generates anyway:

| Signal | Source |
|---|---|
| Throughput | one timed `GET /probe/65536`; `kbps = bytes × 8 / ms` |
| RTT | the `/queue/status` polls themselves; need ≥ `min_rtt_samples` |
| Loss proxy | `failed_requests / total_requests` |
| Stability | `1 − clamp(stdev(rtt)/mean(rtt), 0, 1)` |

These become a **7-feature vector whose order is a contract**, because the model
learned position by position:

```
log10_throughput_kbps, rtt_mean_ms, rtt_p95_ms, rtt_jitter_ms,
fail_ratio, stability, n_rtt_samples
```

`log10(tput + 1)` keeps the value finite when the probe failed outright.
`loss_ratio` was dropped as a separate feature: `LinkSample` carries only
`failed_requests/total_requests`, which was the brief's own definition of
`loss_ratio`, so the two columns held identical numbers and split a tree model's
feature importance between duplicates.

The model is LightGBM (100 trees, depth 4, 15 leaves), trained on synthetic link
samples, exported with joblib. Decisions use an **expected-cost rule**, not
argmax:

    expected_cost[k] = Σ_j P(true = j) · cost[j][k]      choose argmin

The cost matrix penalises "judged more capable than it is" heavily, so HIGH must
clear a much higher bar than LOW. The matrix ships **inside the bundle**, so the
rule at serve time is exactly the rule the model was evaluated under.

**The fallback ladder.** `classify()` never raises; every failure returns MEDIUM
with `fallback=True`, in this order:

1. the bundle is missing, unreadable or undeserialisable;
2. fewer than `min_rtt_samples` usable RTTs;
3. top-class probability below `confidence_threshold`;
4. the bundle fails validation — `feature_names` drifted from `FEATURE_NAMES`,
   or no `cost_matrix`/`feature_ranges`;
5. **any feature outside the range the model was trained on.**

Condition 5 is the expensive lesson. On first contact with a real network the
probe finished too fast to time, reported 524,288,000 kbps, and the model
returned HIGH at 0.967 confidence with `fallback=False`. The model was not
wrong — it was asked about a point decades outside its training set, and a tree
ensemble answers there confidently from the nearest leaf. **Confidence guards
the model's uncertainty, not the input's validity.** Condition 5 is the only
check that interrogates the question before trusting the answer, and it uses the
recorded min/max with no margin, because there is no principled margin for a
model that does not degrade gracefully outside its splits.

### 6.2 C3 — payload adaptation

Three renderings of the *same information* — index number, name, every subject
and grade, the outcome. What is dropped is presentation, never content.

| variant | document | total transferred | requests | budget (§3.9) |
|---|---|---|---|---|
| `full` | 6,694 B | **411,655 B** | 6 | 460,800 B |
| `reduced` | 4,969 B | **4,969 B** | 1 | 61,440 B |
| `essential` | 1,909 B | **1,909 B** | 1 | 6,144 B |

(Measured from a real origin record through the real renderer.)

Cost has two components and both matter: **bytes**, and **round trips**.
`essential` is one request by construction. `full` references real
sub-resources — stylesheet, two fonts, script, crest — so on a 250 ms link it
pays latency on top of weight. `variant_cost()` accounts for both using the same
`discover()` the client SDK uses to fetch them, so the budgeted set and the
measured set cannot drift apart.

Templates render under Jinja's `StrictUndefined`: a record missing a field the
template names is a loud 500, not a blank space on a student's result page.
That strictness is also what surfaced the schema mismatch in A9 (§10).

### 6.3 The client SDK

One coroutine per emulated client. `run_client()` **never raises** except on
cancellation — a load generator drives 20,000 of these, and one raising client
must not take down a gather of the rest. Every failure path returns a labelled
outcome instead:

`COMPLETED` · `TIMED_OUT` · `ABANDONED` · `EXPIRED` · `ADMISSION_UNAVAILABLE` ·
`ORIGIN_UNAVAILABLE` · `COMPLETED_UNREPORTED` · `TICKET_UNKNOWN`

The distinctions are not pedantry. `ADMISSION_UNAVAILABLE` means the
infrastructure broke, and must never be counted as a client that failed to
finish — if M1 is down there is no event log at all, so this return value is the
only place that distinction can live. `TICKET_UNKNOWN` (a 404 on status) means
ticket state was lost, which invalidates the run it appears in.

**Determinism, honestly:** every client-side random choice comes from an RNG
seeded on `(seed, client_id)`, so decisions replay. Outcomes do not — wall-clock
timing, RTT, sub-resource completion order and whether a transfer beats
`expires_at` are all real.

---

## 7. M3 — origin, testbed, evaluation

### 7.1 The mock origin

A deliberately overloadable stand-in.

**Deterministic service time.** Times drawn from one shared RNG in arrival order
are *not* reproducible in a concurrent server, because arrival order itself
varies. So each service time is derived from a hash of `(seed, index)`:

```python
z = NormalDist().inv_cdf(uniform01("svc", seed, index))
return cfg.median_ms * math.exp(cfg.sigma * z)
```

`blake2b` rather than Python's `hash()` (salted per process); inverse-CDF rather
than `random.lognormvariate` so one uniform maps to one draw with no hidden
state. The cost, which belongs in the report: a retry for the same index draws
the *same* service time, so service time is correlated across attempts.

**Load shedding** is what makes `mode: none` collapse:

```python
if state.waiting >= state.config.origin.queue_limit:
    state.tracker.record_rejected()
    return JSONResponse({"detail": "origin queue full"}, status_code=503)
async with _serving_slot(state):    # semaphore(concurrency_limit)
```

The check-then-increment has **no `await` between the two lines**, which makes
it atomic under asyncio's single-threaded scheduler, so the bound cannot be
overshot by simultaneous arrivals.

**O(1) health.** A true rolling p99 is not O(1), and M1's controller polls this
every tick and must never queue behind real traffic. So: a ring of five 1-second
buckets, each with a fixed 128-bucket log-spaced latency histogram spanning
0.5 ms – 60 s (≈9.6% per bucket). Constant work per observation and per read.
The reported p99 is the **upper edge** of the containing bucket, so it never
understates latency — understating it would flatter the origin exactly when it
is struggling.

Three counters are kept apart deliberately: **served** enters the histogram;
**rejected** (a 503) counts as an error but is *excluded* from the histogram,
because a rejection completes in microseconds and would drag p99 down when the
origin is most overloaded; **abandoned** is neither error nor latency sample.
`err_rate_1s` reads only fully elapsed buckets, so a partially-filled current
bucket cannot read spuriously low and tell the controller everything is fine at
the moment it starts failing.

Origin samples are written to a **separate** `results/origin-{run_id}.jsonl`
through an `EventSink`, preserving the event log's single-writer rule; the
analysis joins them on timestamp, and `clock_skew_warning()` reports when the
two logs do not overlap as expected.

### 7.2 The testbed

One shaped container per class, `tc` applied at startup, `NET_ADMIN` required.

| Class | Rate | Delay ± jitter | Loss | Burst |
|---|---|---|---|---|
| HIGH | 50 Mbit | 15 ms ± 3 ms | 0.01% | 256 kbit |
| MEDIUM | 5 Mbit | 60 ms ± 20 ms | 0.5% | 64 kbit |
| LOW | 512 kbit | 250 ms ± 120 ms | 3% | 32 kbit |

Three findings are baked into these numbers:

- **Ingress shaping is mandatory.** The thing a LOW client cannot finish is a
  *download*. A `tbf` on egress caps uploads and leaves the download at full VM
  speed — the completion gap never appears and the testbed silently measures
  nothing. `netem.sh` redirects `eth0` ingress to an `ifb` device and shapes
  there, and **fails loudly** if `ifb` is unavailable.
- **Burst is per class.** A token bucket cannot reach its rate when burst is
  below ~`rate ÷ HZ`; at 50 Mbit and HZ=1000 that floor is ~50 kbit, so the
  brief's literal `32kbit` would cap HIGH near 30 Mbit and fail the gate on
  correctly applied shaping.
- **The rate gate uses UDP.** By Mathis, a TCP measurement of the LOW profile
  tops out near 53% of its configured rate on a perfectly shaped link. UDP
  measures what `tbf` admits; TCP goodput is reported alongside as
  informational, with the Mathis prediction next to it.

Loss is gated by a **95% Wilson score interval** rather than a flat tolerance,
and a check is only marked gating when the interval is narrow enough to be
informative — a 10% relative check on 0.01% loss would need millions of packets
and would be a fake gate that always passes.

`profiles.py` is the source of truth; `docker-compose.yml` mirrors it, and a
test parses the compose file and asserts they agree.

**No experiment runs until the testbed verifies** achieved rate, RTT and loss
from inside each container, at the start of every run.

### 7.3 Load generation

20,000 emulated clients standing in for ~200,000 real ones, with the origin's
concurrency scaled by the same factor. Arrivals are a flash crowd: with
probability `burst_fraction` (0.8) a Gaussian burst at `burst_center_s ±
burst_sigma_s`, otherwise an exponential tail with mean `tail_decay_s`.

Three mechanisms make "all modes saw the same load" structural rather than
promised:

- **Mode is invisible.** `population.py` and `loadgen.py` never receive it, and
  a test parses both modules' ASTs to prove the identifier never appears.
- **Class counts are apportioned, not sampled** (largest remainder), so a 0.25 /
  0.40 / 0.35 mix of 20,000 is exactly 5,000 / 8,000 / 7,000 every seed.
- **Every population carries a SHA-256** of its client list. The harness aborts
  if two runs of the same seed disagree, and `load()` refuses a file whose
  contents no longer match its hash.

The load generator schedules and supervises but **never speaks HTTP** — each
client is one call into M2's `run_client`, because two client implementations
would let baseline and AAAC differ for reasons neither owner controls.

---

## 8. The event log

Append-only JSONL at `results/events-{run_id}.jsonl`, written by the admission
service alone. The vocabulary is closed:

`JOIN` · `ESTIMATE` · `ADMIT` · `COMPLETE` · `TIMEOUT` · `REQUEUE` ·
`DOWNGRADE` · `ABANDON` · `ORIGIN_SAMPLE` · `CONTROL`

```json
{"ts": 1756900000.123, "run_id": "r07", "mode": "aaac", "ticket_id": "…",
 "event": "ADMIT", "access_class": 2, "true_class": 2, "attempt": 2,
 "bytes": 0, "duration_ms": null, "variant": "essential", "position": 0}
```

`EventLogger.log` stamps `ts`, `run_id`, `mode` and the event name, buffers up
to 100 lines or 500 ms, and writes through `asyncio.to_thread` so no handler
blocks on disk. An event name outside the vocabulary raises.

Two consequences shape everything downstream:

1. **If a decision is not logged, it cannot be measured.** Every control
   decision must emit an event; a gap is a hole in the instrument.
2. **No number is ever read from live process state.** No scraping
   `/admin/snapshot` into a result. The log is the interface, which is what
   makes the analysis reproducible from an archived file months later.

The reader enforces this: an event outside the vocabulary or a malformed line
raises `LogIntegrityError` rather than being skipped, and `single_mode()`
refuses a log containing more than one mode.

---

## 9. From log to number

Every metric is computed from the log alone and reported **disaggregated by
`true_class`**, because an aggregate conceals exactly the effect under study.

| Metric | Definition |
|---|---|
| Completion rate | `COMPLETE` / joined, per class |
| Time to completion | last `COMPLETE.ts` − `JOIN.ts`; median and p95, censoring stated |
| Attempt count | `ADMIT` per completed ticket; mean, p95, max, full CCDF |
| Goodput | successful bytes / total bytes, including bytes burned on timeouts |
| **Δ** | `completion_rate[HIGH] − completion_rate[LOW]` |
| Jain's index | `J = (Σxᵢ)² / (n · Σxᵢ²)` over the three per-class rates |
| Origin stability | 5xx rate and p99 from `ORIGIN_SAMPLE` |
| Classifier accuracy | `ESTIMATE.access_class` vs `true_class`, optimistic error called out |

Decisions that change what the numbers mean:

- **Grouping uses the configured netem profile, never the estimate.** Grouping
  by the estimate would measure the classifier instead of the system.
- **Time to completion is censored, not filtered** — it is defined over
  completers only, so the number of non-completers is printed beside it. A
  median that improves because slow clients never finished is not an
  improvement.
- **Goodput includes bytes burned on timed-out attempts.** Those burned bytes
  are the entire cost of the retry loop; excluding them would make goodput ≈ 1.0
  for everyone and delete the most damning number in the evaluation.
- **Origin stability averages per-sample `err_rate_1s`**; it does not sum window
  totals, because consecutive `ORIGIN_SAMPLE` events describe overlapping
  5-second windows and summing would multiply-count roughly 5×.
- **Δ is `None`, not `0.0`, when HIGH or LOW has no joined tickets.** Zero would
  read as "perfectly fair"; `None` reads as "no data", which is the truth.

Every result carries a `Provenance` record — which event types it came from, how
many tickets were included, how many excluded and why, how many were censored —
and every warning the log's shape forces. For example, a live run of the merged
system emits:

> 12 COMPLETE event(s) carried no `ok` field and were counted as successful.
> §4.4 defines completion as COMPLETE(ok=true); this inference holds only while
> the admission service logs a failed completion as ABANDON rather than
> COMPLETE(ok=false).

### Statistics, without scipy

The contract's stack is pandas + matplotlib, so the required distributions are
implemented directly: the regularised incomplete beta via a modified Lentz
continued fraction gives the Student-t CDF as `I_{df/(df+t²)}(df/2, ½)`, and
`t_ppf` by bisection. Validated against published critical values at df = 1, 2,
4, 9, 29, 100 to within 1e-3.

Minimum 5 seeds × 3 modes, and because modes share a seed the comparison is
**paired**: the per-seed difference in Δ between `baseline` and `aaac`, tested
with a paired t-test.

**The Wilcoxon problem.** The exact two-sided signed-rank test on n = 5 has
2⁵ = 32 equally likely sign assignments, so its smallest attainable p-value is
2/32 = **0.0625**. It cannot reject at α = 0.05 with five seeds regardless of
effect size, so `report.py` withholds the p-value entirely rather than printing
one that could never have rejected.

### The pre-registered falsification rule

Written before the experiment runs. **SUPPORTED** requires all three:

1. mean Δ(aaac) < mean Δ(baseline) with the paired 95% CI excluding zero;
2. mean origin 5xx rate under `aaac` ≤ under `baseline`;
3. HIGH-class p95 time-to-completion under `aaac` within 20% of baseline.

Otherwise `report.py` prints `HYPOTHESIS NOT SUPPORTED` naming which condition
failed. That branch is tested against fabricated null data — five distinct kinds
of null result — so reporting a negative result is known to work rather than
hoped to. If aggregate completion falls while Δ narrows, the rule attaches a
note: the gap may have closed because HIGH got worse, not because LOW got
better.

Fabricated data is quarantined: `synth.write()` refuses any filename not
prefixed `synthetic-`, `.gitignore` excludes them, and `report.py` opens with a
banner when any source log is synthetic.

---

## 10. Running it

```bash
make install          # venv on Python 3.11 + the package and dev tooling
make check            # ruff, mypy, pytest
make up               # the testbed (needs .env with AAAC_TOKEN_SECRET)
make verify-testbed   # THE GATE: measure achieved rate/RTT/loss per container
make population       # replayable client populations for SEEDS
make experiment       # all modes × SEEDS, verifies the testbed first
make figures report
```

Unit tests never touch Redis, Docker or the network. For an end-to-end check
without containers, all three services can be run in one process over
`127.0.0.1` and driven through the real SDK — that is how the schema mismatch in
§11 was found.

### Configuration

`configs/run.yaml` is the single source of run parameters, sectioned by owner:
`admission:` (M1), `estimator:` and `delivery:` (M2), `origin:` and `load:`
(M3). M1's loader **rejects any key outside §3.9**, so M3-only parameters (the
health window and bucket count, the sample interval, `burst_fraction`, the
results directory) live as named defaults in `src/aaac/origin/config.py`
instead, which layers over M1's loader so that a file M3 accepts is a file the
admission service accepts.

`mode` is read from the file by M1 and honours no environment override, so the
committed value — not `make run MODE=…` — is what the admission service runs as
(A8).

---

## 11. Verified state, and what is known to be wrong

**Verified.** 411 tests pass; `ruff` and `mypy` are clean across the whole
repository.

*In one process, over loopback.* 12 clients per mode complete end to end through
the real M1 + M2 + M3: `none` serves `full` (12 × 411 KB ≈ 4.94 MB), `aaac` and
`baseline` serve `reduced`. M1 writes the event log and M3's chain reads it.

*As built containers.* `docker compose up` brings up redis, origin, delivery and
admission; 12 clients complete against the images through **real Redis** and the
Docker network. The admission container's log reads back single-mode with
`JOIN 12 · ADMIT 12 · COMPLETE 12`, 4/4 completions in every class, Δ = 0.0 and
59,655 bytes transferred. (Read the log *after* the flush window: `EventLogger`
buffers up to 100 lines or 500 ms, so a read the instant the last client returns
sees the JOINs and ADMITs but not the COMPLETEs.)

*At scale, on generated data.* 20,000 clients × 3 modes × 5 seeds through
metrics, statistics, five figures and a report.

**Not a measurement.** None of the above is a measurement of the system under
load, because **no run has gone through the shaped containers**. All three
client containers exit 1 at startup on this machine:

    netem.sh: FATAL: could not create ifb0. The ifb kernel module is
        unavailable in this kernel (common under Docker Desktop).

That is §7.2's loud failure working: a container that cannot shape downstream
never carries load. `make verify-testbed` consequently reports `service
"client-high" is not running`. Until the testbed runs on a kernel with `ifb` —
a Linux host — there is no Δ worth reporting, because every client is on the
same unshaped link. `ComposeRunner` is also still unwritten.

One consequence visible already: on the Docker bridge the probe measures around
1 Gbit/s, and C1's **fallback condition 5 fires on every client** —
`log10_throughput_kbps=6.03 outside [0, 5.75]` — so each one correctly falls
back to MEDIUM rather than being confidently misread. The guard described in
§6.1 is doing exactly what it was built for, and it is also why every client in
these runs received `reduced`.

**Known issues** (detail in [`INTEGRATION-ISSUES.md`](./INTEGRATION-ISSUES.md)):

| # | Issue | Effect |
|---|---|---|
| A1 | `baseline` applies payload adaptation | The control condition contains C1+C3, which understates the problem Stage 2 exists to measure. **Confirmed in a live run**: baseline served `reduced`, not the contracted full payload. |
| A2 | A failed transfer ends the ticket instead of re-queueing it | The retry loop the proposal is about is largely short-circuited. |
| A4 | Gaps in the event log | `TIMEOUT` carries no bytes (goodput is an upper bound); rejected estimates are unlogged; `COMPLETE` has no `ok`. |
| A5 | The controller reads the configured class mix | It is handed the ground truth a real portal would not have. |
| A8 | `mode` cannot be set per run | The §4.5 mode matrix cannot be driven from the environment. |
| A9 | Origin record ↔ delivery template schema | **Fixed.** See below. |
| A10 | Compose let the origin and admission run different modes | **Fixed.** The origin honoured `AAAC_MODE` while M1 read `mode` from the file, so a default `up` ran the origin in `none` and admission in `aaac`. Caught by `EventLog.single_mode()` refusing the mixed log, not by any service. `run.yaml` now governs both. |

### A9, because it is instructive

The first end-to-end run of the merged system failed completely — every client
abandoned, zero bytes transferred, delivery 500 on every request:

    jinja2.exceptions.UndefinedError: 'dict object' has no attribute 'exam'

M3's origin emitted the three fields §4.1 names. M2's templates render fourteen
and iterate `r.subjects`, where M3 emitted `results`. Both test suites were
green: M2 rendered its own `sample_record()`, and M3 asserted its own record's
shape and never rendered anything. The two records never met until a real client
fetched a real page.

The origin now emits the full record the page renders — still deterministic from
`(seed, index)`, still size-stable — and `tests/test_origin_records.py` renders
every variant from an origin record, so the two sides are now pinned together by
a test rather than by hope.

One more observation from those runs: in-process over loopback, **no `ESTIMATE`
events appeared at all** — the controller admits every client within a tick or
two, long before it accumulates the RTT samples C1 needs, so each ticket keeps
its MEDIUM join placeholder. Against the containers the estimates *were*
submitted (the extra network hop supplies the RTT samples), and then fell back
on condition 5 as described above. Either way C1 cannot be meaningfully
exercised without the shaped testbed — which is the point of the testbed.
