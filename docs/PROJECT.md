# AAAC — Access-Aware Admission Control

A three-person research project. This document explains what the system is, what
problem it attacks, how the three packages fit together, and how the claim will
be tested. It is written for someone who has not seen the code.

> Package-level detail for M3 (evaluation) is in [`M3-EVALUATION.md`](./M3-EVALUATION.md).
> The binding interface contract lives in `CLAUDE.md` §3 and overrides anything
> here if the two ever disagree.

---

## 1. The problem

A national exam results portal on release day. Hundreds of thousands of students
hit it inside the same few minutes. The server saturates and collapses.

The usual fix is a **virtual waiting room**: put a queue in front, admit people at
a rate the origin can survive. This protects the server. The claim this project
investigates is that it does *not* protect people evenly.

Consider two students, both admitted, both given the same fixed admission window:

| | Urban fibre | Congested rural cell |
|---|---|---|
| Bandwidth | 50 Mbit/s | 512 kbit/s |
| RTT | 15 ms | 250 ms |
| Packet loss | 0.01% | 3% |
| 450 KB page | well under a second | does not finish in the window |

The second student times out, is sent to the back of the queue, and tries again.
The retry costs exactly as much as the first attempt, because nothing about the
system has adapted. So the loop does not converge — it diverges. **The people with
the worst connections are systematically excluded by a mechanism that was supposed
to be fair.**

There is a further twist that makes the rural link worse than its headline number
suggests. TCP throughput over a lossy, high-latency path is bounded by roughly
`MSS / (RTT · √loss)`. At 250 ms and 3% loss that ceiling is about **270 kbit/s** —
barely half the 512 kbit/s the link is nominally provisioned at. A "512 kbit/s"
student is really a ~270 kbit/s student the moment loss enters the picture.

### The headline number

**Δ = completion_rate[HIGH] − completion_rate[LOW]**

The fraction of well-connected users who finish, minus the fraction of
badly-connected users who finish. Δ = 0 means the queue treated everyone alike.
A large Δ is the injustice, quantified.

Everything in this project exists either to *cause* Δ to shrink (M1 and M2) or to
*measure it honestly enough that the shrinking could be disproved* (M3).

---

## 2. The proposed system

AAAC's claim: a queue can close that gap by adapting to the link. Five components.

| | Owner | Idea |
|---|---|---|
| **C1** | M2 | Estimate link quality from traffic the client already generates |
| **C2** | M1 | Adaptive admission window — slower links get a longer slot |
| **C3** | M2 | Payload adaptation — slower links get a smaller page |
| **C4** | M1 | Non-regressive re-queue — a timed-out client keeps its place and gets a cheaper attempt |
| **C5** | M1 | Capacity-tracking admission rate — AIMD control so the origin never dies |

The pieces are meant to compose. C1 classifies the link; C2 gives that class more
time; C3 gives it less to download; C4 stops a failure from costing the client its
position; C5 keeps the whole thing inside what the origin can actually serve.

C3 is the one doing most of the work conceptually. Giving a 270 kbit/s client a
longer window helps, but giving it a 40 KB page instead of a 450 KB page changes
the arithmetic outright.

### Access classes

```python
class AccessClass(IntEnum):   # ordering matters: downgrade = HIGH -> MEDIUM -> LOW
    HIGH = 0
    MEDIUM = 1
    LOW = 2
```

`MEDIUM` is the default whenever classification is unavailable or low-confidence —
so a failure of C1 degrades to ordinary behaviour rather than to a wrong decision.

The ordering is not cosmetic. Because `HIGH < MEDIUM < LOW`, a misclassification
where the *estimate is numerically lower than the truth* is a client judged **more
capable than it is** — the error that sends a slow student a payload they cannot
fetch. That asymmetry is reported separately from raw accuracy.

---

## 3. Who owns what

| Package | Owner | Scope |
|---|---|---|
| **M1** | Thisaru Ramanayaka | `common/`, admission queue, controller, window, re-queue |
| **M2** | Sachintha | estimator, delivery, client SDK, dashboard |
| **M3** | Devmith | mock origin, netem testbed, load generation, metrics, plots, report |

### Why the split is drawn there

The boundary that matters most is between M3 and the other two. M3 builds the
apparatus that decides whether the claim is true — so M3's independence from the
implementation is what makes the result credible. If the person measuring the
system is also tuning it, a flattering number is no longer evidence of anything.

Concretely: M3 does not modify M1's or M2's code, and if a number comes out badly
the response is to report it, not to patch the thing being measured.

The reciprocal rule is that M1 and M2 never see `true_class` — the ground-truth
network profile. It travels through their services as an opaque label so M3 can
score the classifier, and if it ever influences a decision inside their code the
whole experiment is silently invalid.

### Repository layout

```
aaac/
├── CLAUDE.md                     # the binding contract (§3) + per-owner briefs
├── docker-compose.yml            # M3: the whole testbed environment
├── Makefile                      # M3
├── configs/run.yaml              # single source of run parameters
├── docs/                         # this file and M3-EVALUATION.md
├── src/aaac/
│   ├── common/                   # M1: schemas, tokens, events, config, classes
│   ├── admission/                # M1: queue, controller, requeue, API
│   ├── estimator/                # M2: features, model, inference, training
│   ├── delivery/                 # M2: payload variants, delivery API
│   ├── client/                   # M2: client SDK (driven by M3's load generator)
│   ├── origin/                   # M3: mock origin service
│   └── evaluation/               # M3: testbed, load gen, metrics, plots, report
├── models/                       # exported classifier + model card
├── results/                      # event logs, figures, tables
└── tests/
```

---

## 4. How the services talk

Three HTTP services plus Redis, on one Docker network.

```
                    ┌──────────────┐
   shaped client ──▶│  admission   │  M1   :8000   queue, window, re-queue, AIMD
   (netem/tbf)      │              │
                    └──────┬───────┘
                           │ polls /origin/health every 1 s
                           ▼
   shaped client ──▶┌──────────────┐      ┌──────────────┐
                    │   delivery   │ M2   │    origin    │ M3  :8002
                    │    :8001     │─────▶│              │
                    └──────────────┘      └──────────────┘
                     payload variants      overloadable stand-in
```

### Admission service — `http://admission:8000` (M1)

| Method | Path | Body / Params | Returns |
|---|---|---|---|
| POST | `/queue/join` | `{client_id}` | `{ticket_id, join_seq, position, eta_s, poll_interval_ms}` |
| POST | `/queue/estimate` | `LinkEstimate` | `{accepted, access_class}` |
| GET | `/queue/status/{ticket_id}` | — | `TicketStatus` |
| POST | `/queue/complete` | `{ticket_id, ok, bytes, duration_ms, variant}` | `{state}` |
| GET | `/admin/snapshot` | — | live counters |
| GET | `/admin/stream` | — | SSE at 1 Hz |

### Estimator / delivery — `http://delivery:8001` (M2)

| Method | Path | Params | Returns |
|---|---|---|---|
| GET | `/probe/{n_bytes}` | — | incompressible payload, `Cache-Control: no-store` |
| GET | `/result` | `?token=…&index=…` | HTML variant selected from the token's class |

### Mock origin — `http://origin:8002` (M3)

| Method | Path | Params | Returns |
|---|---|---|---|
| GET | `/origin/result` | `?index=…` | JSON record; 503 past the concurrency limit |
| GET | `/origin/health` | — | `{in_flight, p99_ms, err_rate_1s}` |

`/origin/health` is polled by M1's controller **every control tick (1 s)**. It must
be O(1) and must never block on the request semaphore — if health checks queue
behind real traffic, the controller goes blind exactly when it matters.

### The admit token

Compact HMAC-SHA256, `b64url(payload) + "." + b64url(sig)`, payload
`{"tid", "cls", "att", "exp", "var"}` with `var ∈ {full, reduced, essential}`.
M1 writes it, M2 verifies it, M3 treats it as an opaque string and never parses one.

---

## 5. The three run modes

`configs/run.yaml → mode`:

- **`none`** — no queue, clients hit the origin directly. Reproduces congestion collapse.
- **`baseline`** — access-blind queue: fixed window `W_base`, full payload for everyone,
  reset-on-failure re-queue (a failed client goes to the tail).
- **`aaac`** — the full proposed system.

**All three run through the same code paths and the same event log. Mode is a
config flag, never a separate binary.** If a mode diverges into its own code path,
the comparison is no longer controlled and the results are worthless.

`baseline` is the honest comparator — a conventional virtual waiting room, the
thing a real portal would actually deploy. Beating `none` proves only that a queue
is better than no queue, which nobody doubts.

---

## 6. The event log

Append-only JSONL at `results/events-{run_id}.jsonl`, **single writer** (the
admission service). The vocabulary is closed:

`JOIN` · `ESTIMATE` · `ADMIT` · `COMPLETE` · `TIMEOUT` · `REQUEUE` · `DOWNGRADE` ·
`ABANDON` · `ORIGIN_SAMPLE` · `CONTROL`

```json
{"ts": 1756900000.123, "run_id": "r07", "mode": "aaac", "ticket_id": "…",
 "event": "ADMIT", "access_class": 2, "true_class": 2, "attempt": 2,
 "bytes": 0, "duration_ms": null, "variant": "essential", "position": 0}
```

Two consequences that shape the whole project:

1. **If a decision is not logged, it cannot be measured.** Every control decision
   must emit an event; any gap in the stream is a hole in the evaluation.
2. **No number is ever read from live process state.** No scraping
   `/admin/snapshot` into a result, no instrumenting M1's internals. The log is the
   interface, which is what makes the analysis reproducible from an archived file
   months later.

---

## 7. How the claim gets tested

### The testbed

Docker Compose with one shaped client container per access class. Each gets a `tc`
qdisc chain (`tbf` for bandwidth, `netem` for latency/jitter/loss) applied at
startup, requiring `NET_ADMIN`.

| Class | Rate | Delay ± jitter | Loss | Represents |
|---|---|---|---|---|
| HIGH | 50 Mbit | 15 ms ± 3 ms | 0.01% | urban fibre / good LTE |
| MEDIUM | 5 Mbit | 60 ms ± 20 ms | 0.5% | typical mobile broadband |
| LOW | 512 kbit | 250 ms ± 120 ms | 3% | congested rural cell |

**No experiment runs until the testbed verifies.** A `tc` command that silently
no-ops looks identical to one that worked, so achieved rate, RTT and loss are
measured from inside each container and asserted within 10% of configuration, at
the start of every run rather than once at the start of the project.

### The load

20,000 emulated clients standing in for ~200,000 real ones, with the origin's
concurrency limit scaled by the same factor. Arrivals are a flash crowd — a
Gaussian burst plus an exponential tail — not steady state.

The population is generated **once per seed** and replayed across all three modes,
so any difference between conditions comes from the system rather than the load.

### The metrics

Every metric is computed from the event log alone and reported **disaggregated by
`true_class`**, because an aggregate conceals exactly the effect under study.

| Metric | Definition |
|---|---|
| Completion rate | `COMPLETE(ok=true)` / joined, per class |
| Time to completion | last `COMPLETE.ts` − `JOIN.ts`; median and p95, censoring stated |
| Attempt count | `ADMIT` per completed ticket; mean, p95, max, full CCDF |
| Goodput | successful bytes / total bytes, **including bytes burned on timeouts** |
| **Completion gap Δ** | `completion_rate[HIGH] − completion_rate[LOW]` |
| Jain's index | `J = (Σxᵢ)² / (n · Σxᵢ²)` over the three per-class rates |
| Origin stability | 5xx rate and p99 from `ORIGIN_SAMPLE` |
| Classifier accuracy | `ESTIMATE.access_class` vs `true_class`; optimistic error called out |

### The statistics

Minimum 5 seeds × 3 modes. Because modes share a seed, the comparison is
**paired**: the per-seed difference in Δ between `baseline` and `aaac`, tested with
a paired t-test. Paired comparison is far more powerful here than comparing two
independent means, and it is the correct test for the design.

---

## 8. The pre-registered falsification rule

Written down **before** the full experiment runs, in
`src/aaac/evaluation/README.md`.

> **Hypothesis.** AAAC substantially reduces Δ relative to the access-blind
> baseline while keeping origin stability and not materially degrading HIGH-class
> completion time.

**SUPPORTED** requires all three:

1. mean Δ(aaac) < mean Δ(baseline), and the paired 95% CI of the per-seed
   difference **excludes zero**;
2. mean origin 5xx rate under `aaac` ≤ under `baseline`;
3. HIGH-class p95 time-to-completion under `aaac` within **20% relative** of
   baseline.

**NOT SUPPORTED** otherwise, and `report.py` prints `HYPOTHESIS NOT SUPPORTED`
naming which condition failed. That branch is tested against fabricated null data,
so reporting a negative result is known to work rather than hoped to.

### The trade-off to watch for

Longer LOW windows consume admission throughput. **If aggregate completion falls
while Δ narrows, that is a real finding and belongs in the body of the report, not
a footnote** — the gap may have closed because HIGH got worse rather than because
LOW got better. `report.py` computes aggregate completion alongside Δ for exactly
this reason.

---

## 9. Why the negative result still counts

Stage 2 — quantifying Δ under a conventional access-blind queue — produces
evidence that **the problem exists**, and that is independent of whether AAAC
works. If the proposed system underperforms, a rigorous measurement of the
completion gap under a standard virtual waiting room is still a real,
defensible contribution. It is the empirical claim the whole proposal rests on.

That is why Stage 2 is prioritised over everything downstream of it, and why the
falsification rule is written before the run rather than after.

---

## 10. Status

| Package | State |
|---|---|
| M1 — admission, controller, re-queue | built on branch `thisaru`; not yet merged |
| M2 — estimator, delivery, client SDK | built on branch `sachintha`; not yet merged |
| M3 — origin, testbed, analysis chain | built and tested on branch `devmith`; see [`M3-EVALUATION.md`](./M3-EVALUATION.md) |

The three branches are being merged into a shared development branch.

Several early questions are now answered by the code. `true_class` travels on the
`/queue/join` body; `mode: none` is an immediately-ADMITTED ticket that fetches the
full page through delivery; `COMPLETE` carries no `ok` field, with failures logged as
`ABANDON` instead.

Issues found reading M1's and M2's branches against the contract are tracked in
[`INTEGRATION-ISSUES.md`](./INTEGRATION-ISSUES.md). Two of them — `baseline` applying
payload adaptation (A1), and failed transfers ending tickets rather than re-queueing
them (A2) — block Stage 2.
