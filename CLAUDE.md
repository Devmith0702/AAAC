# CLAUDE.md — AAAC, Work Package 2

**Repository:** https://github.com/Devmith0702/AAAC
**Branch:** `m2-estimation-delivery`
**Owner of this package:** Sachintha
**Components:** C1 (In-Band Link Estimator), C3 (Payload Adaptation)
**Also owns:** the client SDK (`src/aaac/client/`) and the operator dashboard

---

## 1. How to work with me in this repository

These rules override any instinct toward speed. Read them before touching anything.

### 1.1 Never commit without my explicit authority

- Do **not** run `git commit`, `git push`, `git merge`, `git rebase`, `git reset`,
  `git checkout <branch>`, or `git stash` unless I have asked for it in that message.
- Staging with `git add` is also a commit-path action. Ask first.
- When you think a commit is warranted, say so and stop: *"This is a good commit
  point. Suggested message: `...`. Say the word and I'll run it."*
- Never amend, squash, or rewrite history. Never force-push. Never create a tag.
- Never open a pull request or interact with the GitHub remote in any way.

Reading git state is fine and encouraged: `git status`, `git diff`, `git log`.

### 1.2 Explain before you act

For anything beyond a trivial edit, tell me **what** you are about to do and **why**
before doing it. Specifically:

- Creating a new file → say what it is for and where it sits in the architecture
- Changing an existing file → say what breaks if you are wrong
- Installing a package → say why, and check it against §3.1 first
- Deleting anything → ask, always
- Running a long command → say what it does

If a task needs more than about three steps, give me the plan as a short numbered
list and wait for a yes. I would rather approve a plan than unpick a surprise.

### 1.3 Ask instead of guessing

If a requirement is ambiguous, ask one specific question. Do not pick an
interpretation and build on it silently. Half the failure modes on this project
are "someone assumed and nobody noticed for a week."

### 1.4 Stay inside my package

`src/aaac/common/` and `src/aaac/admission/` belong to Thisaru (M1).
`src/aaac/origin/` and `src/aaac/evaluation/` belong to M3.

If an import from `common/` fails because the module does not exist yet, **do not
write it for me**. Create or extend a clearly-marked local stub inside
`src/aaac/estimator/` and tell me it needs deleting once M1 ships. One of my
success criteria is literally "M2 never had to patch `common/`."

### 1.5 Do not soften bad results

If a metric looks bad, tell me it looks bad. Do not adjust a threshold, drop a
class, reshape the synthetic data, or reframe the number to make it read better.
This project has a pre-registered falsification rule; quietly flattering numbers
is the single worst thing that can happen to it.

---

## 2. What the project is

**AAAC — Access-Aware Admission Control.**

The setting: a national exam results portal on release day. Hundreds of thousands
of students hit it in the same few minutes. The server saturates and collapses.

The specific injustice the project targets: when a naive queue is put in front of
that server, it does not fix the problem evenly. A student on urban fibre gets in,
downloads a 450 KB page in under a second, and is done. A student on a congested
rural cell — 512 kbit/s, 250 ms latency, 3% packet loss — cannot finish that same
450 KB download inside their admission window. They time out, get sent to the back
of the queue, and try again. And again. Each retry is as expensive as the last, so
the loop diverges. The people with the worst connections are systematically
excluded by a system that was supposed to be fair.

AAAC's claim is that a queue can close that gap by adapting to the link:

| Component | Owner | Idea |
|---|---|---|
| C1 | **M2 (me)** | Estimate link quality from traffic the client already generates |
| C2 | M1 | Adaptive admission window — slower links get a longer slot |
| C3 | **M2 (me)** | Payload adaptation — slower links get a smaller page |
| C4 | M1 | Non-regressive re-queue — a timed-out client keeps its place and gets a cheaper attempt |
| C5 | M1 | Capacity-tracking admission rate — AIMD control so the origin never dies |

The headline metric is **Δ**, the completion-rate gap between HIGH-class and
LOW-class users. M3 measures it. The hypothesis is that AAAC shrinks Δ without
sacrificing origin stability or materially hurting HIGH-class users.

### 2.1 Team

| Package | Owner | Scope |
|---|---|---|
| M1 | Thisaru Ramanayaka | `common/`, admission queue, controller, window, re-queue |
| **M2** | **Sachintha (me)** | **estimator, delivery, client SDK, dashboard** |
| M3 | Devmith Amarasekara | mock origin, netem testbed, load generation, metrics, plots |

---

## 3. Shared contract — IMMUTABLE

Identical in all three briefs. Changing anything here breaks the other two
packages silently. If a change is genuinely needed, it goes in a PR description as
`CONTRACT CHANGE` and both other owners are notified first. **Never edit this
section unilaterally, and never let me edit it without flagging that it affects
other people.**

### 3.1 Stack

| Concern | Choice | Notes |
|---|---|---|
| Language | Python 3.11 | type hints required, `from __future__ import annotations` |
| Web | FastAPI + uvicorn (async) | one ASGI app per service |
| HTTP client | httpx (async) | no `requests` |
| Queue state | Redis 7 | multi-key updates via Lua for atomicity |
| ML | LightGBM + scikit-learn, joblib export | |
| Analysis | pandas + matplotlib | no seaborn, no plotly |
| Testbed | Docker Compose + Linux `tc` (netem/tbf) | |
| Quality | pytest, ruff, mypy (non-strict) | |

No other runtime dependencies without team agreement. No database beyond Redis.
No Kubernetes.

### 3.2 Repository layout

```
aaac/
├── CLAUDE.md
├── docker-compose.yml
├── Makefile
├── configs/run.yaml              # single source of run parameters
├── src/aaac/
│   ├── common/                   # M1: schemas.py, tokens.py, events.py, config.py, classes.py
│   ├── admission/                # M1: queue, controller, requeue, API
│   ├── estimator/                # M2: features, model, inference, training
│   ├── delivery/                 # M2: payload variants, delivery API
│   ├── client/                   # M2: client SDK (used by M3's load generator)
│   ├── origin/                   # M3: mock origin service
│   └── evaluation/               # M3: testbed, load gen, metrics, plots
├── models/                       # exported classifier + model card
├── results/                      # events-{run_id}.jsonl, figures, tables
└── tests/
```

### 3.3 Access classes

```python
class AccessClass(IntEnum):   # ordering matters: downgrade = HIGH -> MEDIUM -> LOW
    HIGH = 0
    MEDIUM = 1
    LOW = 2
```

`MEDIUM` is the default whenever classification is unavailable or low-confidence.

### 3.4 Run modes

`configs/run.yaml → mode`:

- `none` — no queue, clients hit origin directly (reproduces congestion collapse)
- `baseline` — access-blind queue: fixed window `W_base`, full payload for
  everyone, reset-on-failure re-queue (failed client goes to the tail)
- `aaac` — full proposed system

All three modes run through the **same** code paths and the same event log. Mode
is a config flag, never a separate binary.

### 3.5 HTTP API

Admission service — `http://admission:8000` (M1):

| Method | Path | Body / Params | Returns |
|---|---|---|---|
| POST | `/queue/join` | `{client_id}` | `{ticket_id, join_seq, position, eta_s, poll_interval_ms}` |
| POST | `/queue/estimate` | `LinkEstimate` | `{accepted: bool, access_class}` |
| GET | `/queue/status/{ticket_id}` | — | `TicketStatus` |
| POST | `/queue/complete` | `{ticket_id, ok, bytes, duration_ms, variant}` | `{state}` |
| GET | `/admin/snapshot` | — | live counters |
| GET | `/admin/stream` | — | SSE, same payload at 1 Hz |

Estimator/delivery service — `http://delivery:8001` (**mine**):

| Method | Path | Body / Params | Returns |
|---|---|---|---|
| GET | `/probe/{n_bytes}` | — | `n_bytes` of incompressible payload, `Cache-Control: no-store` |
| GET | `/result` | `?token=<admit_token>&index=<index_no>` | HTML variant selected from token class |

Mock origin — `http://origin:8002` (M3):

| Method | Path | Body / Params | Returns |
|---|---|---|---|
| GET | `/origin/result` | `?index=<index_no>` | JSON result record; 503 past concurrency limit |
| GET | `/origin/health` | — | `{in_flight, p99_ms, err_rate_1s}` |

### 3.6 Shared schemas (`src/aaac/common/schemas.py`, pydantic v2)

```python
class LinkSample(BaseModel):
    ticket_id: str
    probe_bytes: int
    probe_duration_ms: float
    rtt_samples_ms: list[float]
    failed_requests: int
    total_requests: int

class LinkEstimate(BaseModel):
    ticket_id: str
    throughput_kbps: float
    rtt_mean_ms: float
    rtt_jitter_ms: float
    loss_ratio: float
    stability: float            # 0..1
    access_class: AccessClass
    confidence: float           # 0..1
    model_version: str
    fallback: bool              # True if defaulted to MEDIUM

class TicketStatus(BaseModel):
    ticket_id: str
    state: Literal["WAITING", "ADMITTED", "COMPLETED", "EXPIRED", "ABANDONED"]
    position: int               # 0 when admitted
    attempt: int                # 1-based
    access_class: AccessClass
    window_s: float | None
    admit_token: str | None
    expires_at: float | None    # unix seconds
```

### 3.7 Admit token (M1 writes, **I verify**)

Compact HMAC-SHA256: `b64url(payload_json) + "." + b64url(sig)`.
Payload: `{"tid": str, "cls": int, "att": int, "exp": float, "var": "full"|"reduced"|"essential"}`.
Shared secret from `AAAC_TOKEN_SECRET`. `verify_token()` raises on bad signature or
past `exp`.

**I must never infer access class from anything but the verified token.** Not a
query parameter, not a header, not a user agent. That is a security property, not
a style preference.

### 3.8 Event log

Append-only JSONL at `results/events-{run_id}.jsonl`. Single writer: the admission
service. Vocabulary is closed:
`JOIN`, `ESTIMATE`, `ADMIT`, `COMPLETE`, `TIMEOUT`, `REQUEUE`, `DOWNGRADE`,
`ABANDON`, `ORIGIN_SAMPLE`, `CONTROL`.

`true_class` is the netem class M3 configured for a client. It is an opaque
passthrough so M3 can score classifier accuracy. **It must never influence any
decision I make.** If you find yourself reading `true_class` anywhere in
`estimator/` or `delivery/` outside a test, that is a bug.

### 3.9 Relevant config (`configs/run.yaml`)

```yaml
estimator:
  probe_bytes: 65536
  min_rtt_samples: 5
  confidence_threshold: 0.60
  model_path: models/link_classifier.joblib

delivery:
  budgets_bytes: {full: 460800, reduced: 61440, essential: 6144}
```

### 3.10 Rules for every session

1. Never break a Section 3 interface without a `CONTRACT CHANGE` note.
2. Every control decision must emit an event. If it isn't logged, M3 can't measure it.
3. No sleeping in request handlers; everything async.
4. Unit tests must not require Redis or the network.
5. Deterministic given `seed`. Seed every RNG from `config.seed`.

---

## 4. My package in detail

### 4.1 C1 — In-band link estimator

Work out how good a waiting client's connection is, using only traffic the client
is already generating. No speed-test screen, no user question, no IP heuristics,
no personal data. That constraint is a claim the proposal makes explicitly and it
belongs in a docstring.

**Four raw signals**, collected while the client sits in the queue:

| Signal | Source |
|---|---|
| Throughput | one timed `GET /probe/65536`; `kbps = bytes × 8 / ms`. Incompressible random bytes so gzip cannot distort it. Re-run once if the wait exceeds 60 s. |
| RTT | time each `/queue/status` poll — tiny response, so it approximates RTT. Need ≥ `min_rtt_samples` (5). |
| Loss proxy | `failed_requests / total_requests`, where a failure is a timeout, reset, or 5xx |
| Stability | `1 − clamp(stdev(rtt)/mean(rtt), 0, 1)` — coefficient-of-variation based |

**Seven features, fixed order.** The order is a contract; the model learned
position-by-position. Any change forces a `model_version` bump and a model card
update.

```
log10(throughput_kbps), rtt_mean_ms, rtt_p95_ms, rtt_jitter_ms,
fail_ratio, stability, n_rtt_samples
```

The brief specified eight, with `loss_ratio` alongside `fail_ratio`. They were
the same number — `LinkSample` carries only `failed_requests / total_requests`,
which is the brief's own definition of `loss_ratio`. `loss_ratio` was dropped as
a C1-owner decision (open question 1, closed 2026-09-06); see the DECISION note
in `features.py`. **`LinkEstimate.loss_ratio` (§3.6) is untouched** and is still
reported on every estimate — the shared contract was not involved.

**Model:** LightGBM, ~100 trees, `max_depth=4`, joblib export.
Targets: file < 200 KB, inference < 2 ms.

**Cost asymmetry — the core design idea.** The two error directions are not equal:

- *Slow link called HIGH* → short window plus 450 KB down a 512 kbit pipe →
  guaranteed timeout. This is precisely the exclusion the project exists to
  remove. **Expensive.**
- *Fast link called LOW* → plain 6 KB page, generous window. Finishes instantly,
  page is less pretty. **Cheap.**

So the model is deliberately biased pessimistic, in two places: class weights
during training, and an expected-cost decision rule at inference instead of plain
argmax. A model at 88% accuracy with almost no optimistic errors beats one at 92%
that makes them.

**Fallback — a headline design property, not defensive boilerplate.**
`classify()` returns `access_class=MEDIUM, confidence=0.0, fallback=True` when:

1. the model file is missing or fails to load,
2. fewer than `min_rtt_samples` RTT samples were collected,
3. the top class probability is below `confidence_threshold`,
4. the loaded bundle fails validation — its `feature_names` have drifted from
   `FEATURE_NAMES`, or it carries no `cost_matrix` or `feature_ranges`,
5. **any feature falls outside the range the model was trained on.** The bundle
   carries per-feature min/max from the training split; `classify()` refuses to
   answer outside them and names the offending feature in the log.

**The two ends of the throughput range are handled differently on purpose. Do
not "fix" the inconsistency.** The client (`client/probe.py`) clamps the slow
end and leaves the fast end raw:

| End | If clamped, the model is told | Consequence |
|---|---|---|
| Slow — below training support | "the slowest link I know" → **LOW** | Correct and safe. Falling back to MEDIUM instead would be an **upgrade** — the optimistic error, handed to the client least able to absorb it. |
| Fast — above training support | "the fastest link I know" → **HIGH** | **The optimistic error itself**, delivered with confidence, from a measurement that carried no information. Left raw so condition 5 catches it and lands on MEDIUM. |

This is the cost asymmetry of §4.1 applied one layer below where the brief
specifies it: at the measurement, not the decision. The two ends are not
symmetric because their errors are not symmetric.

Condition 5 is the one that cost us something to learn — see §5.1. It is the
only check that interrogates the *question* rather than the answer.

Condition 4 is not defensive boilerplate either; it is the worst failure mode
available, because nothing raises. A bundle whose feature order has drifted is
fed the right number of floats in the wrong positions and returns confident
nonsense indefinitely. And the decision rule is part of the model: a bundle
without its `cost_matrix` cannot be served, because substituting a default
would silently apply a different policy than the one that was measured. **Do
not simplify this back to three conditions.**

`confidence` is the **top class probability** — how certain the model is about
its read of the link. `access_class` is the policy decision layered on top, and
the expected-cost rule may deliberately return a class other than argmax, so
the two can disagree. Reporting the returned class's own probability instead
would make a deliberate override look like uncertainty, and would break the
comparability of `confidence` with `confidence_threshold`, which sit beside
each other in §3.6.

The acceptance test: delete `models/link_classifier.joblib`, run the whole `aaac`
pipeline, and it must complete, degrading to baseline-like behaviour with
`fallback: true` on every `ESTIMATE`. The proposal claims no component depends on
classifier correctness for safety or liveness — this test is that claim.

### 4.2 C3 — Payload adaptation

Three renderings of the same exam result. `essential` carries the same
*information*; only presentation is dropped.

| Variant | Budget | Contents |
|---|---|---|
| `full` | ≤ 450 KB | styled page, web font, crest image, client-side JS, CSS framework |
| `reduced` | ≤ 60 KB | one inline `<style>` block, no font, no JS, no images |
| `essential` | ≤ 6 KB | server-rendered HTML: index number, name, subject/grade table. No `<link>`, no `<script>`, no favicon, no external request of any kind |

Hard requirements:

- `essential` must be **one HTTP request**. Any sub-resource defeats the point on
  a high-RTT link.
- A build test asserts `size(full) / size(essential) >= 10`. The proposal claims an
  order of magnitude, so it must be true and measurable.
- **State the claim honestly in the write-up.** Not "we achieved 10×" — that is
  circular, because we chose both numbers. The defensible claim is: *a realistic
  styled results page costs roughly 90× what the information itself weighs, and
  we can serve the information alone.* The ratio test is a regression guard
  against `essential` creeping upward, not the headline result.
- Accurate `Content-Length` on every response — M3 computes goodput from it.
  With sub-resources this means goodput is the **sum** across the document and
  every sub-resource it pulls, not the document's header alone.
- **Disclosure requirement.** `full` and `reduced` use a realistic sub-resource
  structure. Part of their cost on high-RTT links is round trips rather than
  bytes. This is deliberate — it is one of the two mechanisms payload adaptation
  addresses — and must be stated explicitly in the evaluation write-up.

  It cuts in our favour: sub-resources make `full` more expensive, which makes
  baseline worse, which makes AAAC's improvement look larger. That is the
  realistic structure and we stand by it, but it must be stated rather than left
  for an examiner to discover.
- **The font is discovered via `<link rel="preload">`, not via `@font-face`
  alone, and this biases against us.** A real browser finds a font only after
  parsing the CSS that declares it — HTML, then CSS, then font, three *serial*
  round trips. `preload` collapses that into the parallel batch, so `full` is
  slightly **cheaper** here than a naive real page would be. The simplification
  therefore understates `full`'s cost rather than inflating it, which is the
  safe direction. It is not adopted because it needs less machinery.
- **Sub-resources are fetched in parallel, capped at 6**, as a browser does.
  Sequential fetching would charge `full` five serial round trips instead of
  about two — on a 250 ms link, ~750 ms of invented latency landing hardest on
  LOW clients in `baseline` mode, the exact case whose failure flatters AAAC.
- Variant comes **only** from the verified token's `var` field.

**Measured, 2026-09-07** (`sample_record()`, nine subjects, totals include
sub-resources):

| Variant | Document | Sub-resources | Total | Budget | Requests |
|---|---|---|---|---|---|
| `full` | 6,697 | 404,961 | **411,658** | 460,800 | 6 |
| `reduced` | 4,972 | 0 | **4,972** | 61,440 | 1 |
| `essential` | 1,912 | 0 | **1,912** | 6,144 | 1 |

**Lead with this: 98.4% of `full`'s weight is its sub-resources.** That is the
structural fact, it is insensitive to how we sized anything, and it explains
*why* payload adaptation works rather than merely asserting that it does. The
`full`/`essential` multiplier is 215×, but quote it only as a supporting detail
and attach the caveat — we chose both endpoints, and it scales with how many
subjects a record carries.

**Payload adaptation is close to binary on the byte axis.** Because
sub-resources dominate so completely, dropping them removes almost the entire
cost, and there is little left for a third tier to remove. `reduced` → `essential`
saves **3,060 bytes and zero round trips** — about **48 ms** on a 512 kbit link.

This is a result, not a defect, and it was found by building the variants
honestly and measuring them. **Do not raise `reduced`'s weight to manufacture a
three-rung ladder.** The consequence is that the middle tier's value has to come
from **window scaling**, not payload weight — and if it cannot be justified
there, a two-tier design ("has sub-resources" / "does not") would capture nearly
all of the benefit. Raised with M1 as a question, since `AccessClass` is in the
shared contract and the downgrade policy is his.

Render from Jinja2 templates in `delivery/templates/`. Content from
`GET /origin/result?index=…`. On origin 503, return 503 and do not count it as a
completion.

### 4.3 Client SDK

The stand-in browser that M3's load generator drives. Sequence:

1. `POST /queue/join` with `client_id` and `true_class` (opaque passthrough)
2. While `WAITING`: run the probe, poll `/queue/status/{tid}` every `poll_interval_ms`
3. Submit `LinkEstimate` **once**, as soon as the probe has enough RTTs. Later
   attempts do not re-estimate — after that the class comes from M1's downgrade policy
4. On `ADMITTED`: `GET /result?token=…&index=…`, timing the transfer, counting bytes
5. `POST /queue/complete` with `ok`, `bytes`, `duration_ms`, `variant`
6. If the transfer misses `expires_at`, abort, report `ok: false`, resume polling
7. Abandon after `abandon_after_s`

### 4.4 Dashboard

One static HTML file plus one `<script>`, consuming `GET /admin/stream`. No React,
no build step. Per access class: waiting, completed, timed out, and the live Δ.
Must accept the admission service URL as a query parameter so two panes can run
side by side (baseline vs AAAC) — that is the demo. Design for a projector: large
numerals, three clearly distinguished rows, a Δ readout that visibly stalls in
baseline and converges in AAAC.

### 4.5 Tests

- Probe math: synthetic transfer of known size and duration → throughput correct within 1%
- Variant sizes inside budget; the 10× ratio holds (fails the build if a template grows)
- `essential` HTML contains zero `<script>`, `<link>`, `<img>` — assert by parsing, not regex
- **Use sentinel substitution wherever the question is "did this value reach the
  output".** Render the field with a unique marker and look for the marker; do
  not search for the real value. A substring check on real data passes for the
  wrong reason far too easily, and it looks identical to a good assertion — two
  such were found in one session: `"Kandy" in text` matched inside the centre
  name *Central College, Kandy*, and `grade in html` matched the letter "A" in
  arbitrary markup. Neither could ever have failed. Structural linting cannot
  catch this class; only substitution can.
- Tampered token → 401; expired token → 401; valid `cls=2` → `essential` served
- Missing model file → pipeline still completes, all estimates marked `fallback`
- Classifier: accuracy, per-class recall, and the optimistic-error rate specifically

### 4.6 Definition of done

- LOW-class clients complete inside a normal window because the payload shrank,
  and I can point at the byte counts that prove it
- Deleting the model degrades the system gracefully instead of breaking it
- The dashboard is legible from the back of a room
- Zero contract drift: I never had to patch `common/`

### 4.7 Do not build

Queue state, admission rate control, window assignment, re-queue policy, netem,
load generation, metric computation. **If a decision is about *when* a client is
admitted, it belongs to M1. If it is about measuring the outcome, it belongs to M3.**

---

## 5. Current state of the repository

As of 2026-09-04, verified against the working tree:

**Done — `features.py`, `synthdata.py`, `train.py`:**

- `features.py` — `FEATURE_NAMES`, the eight features in fixed order, plus
  `RawSample` (a local stand-in for `LinkSample`). Both training and inference
  call `extract_features()`, which is what prevents train/serve skew. The
  `loss_ratio == fail_ratio` duplication of open question 1 is live in the code
  and carries a NOTE at the assignment.
- `synthdata.py` — synthetic labelled generator. Simulates *behaviour* (a probe
  that took 1,100 ms, polls at 190/450/260 ms, two failures) and derives features
  from it, rather than fabricating feature values directly.
- `train.py` — LightGBM training, cost-sensitive decision rule, metrics, joblib
  export to `models/link_classifier.joblib`.

**Not started:** the probe routine, delivery variants, the SDK, the dashboard.
`models/` does not exist in the repository — no classifier bundle has been
exported here, so any code that loads one currently takes the fallback path by
definition. A fallback test that passes against this tree may be passing for that
trivial reason; make such a test build its own bundle.

**`common/` is available, not blocked.** Thisaru (M1) has pushed
`src/aaac/common/` to `origin/thisaru`: `classes.py`, `schemas.py`, `tokens.py`,
`events.py`, `config.py`, with passing tests. The schemas match §3.6
field-for-field. Branch naming is per-person, not per-package, so M1's code
living on `thisaru` is correct and expected.

Merging it also brings M1's in-progress `admission/store.py` and `window.py`.
Those are M1's and are not to be edited here (§1.4). Merging is my call, not
Claude's (§1.1).

### 5.1 Decisions already made — do not silently revisit these

**Synthetic data must be genuinely hard.** A first version of the generator scored
98% accuracy. That was a failure, not a success: every LOW client was uniformly
bad — slow *and* laggy *and* jittery *and* failing — so all eight features pointed
the same way and the model separated them trivially. The number measured the
generator, not the model.

The fix was `MIXED_CHARACTER_FRAC = 0.30`: 30% of clients draw their latency
character from a different class than their bandwidth. That is realistic —
satellite is fast but laggy, throttled fibre is slow but steady, a good rural cell
is fine until the tower congests. Accuracy fell to ~87%, which is believable.
**Do not raise `CLASS_SEP_DECADES` or lower `MIXED_CHARACTER_FRAC` to improve a
metric.**

**LOW→HIGH is the wrong headline metric.** The brief names it, but it sits at zero
by construction: LOW and HIGH are over a decade apart in throughput and are never
confused. Reporting it alone would look like a triumph and mean nothing. The error
that actually occurs is MEDIUM→HIGH. Track `optimistic_error_rate` — any client
judged *more capable than it is* — which captures the whole dangerous family.
Current figures (`v2-synthetic-7f`, n=12000, seed=1): **4.60%** under naive
argmax, **1.07%** under the cost-sensitive rule. The 8-feature `v1-synthetic`
model scored 4.67% / 1.13%; dropping the duplicate column moved both slightly in
the right direction.

**The cost matrix is provisional.** Current settings buy that 4× reduction in
optimistic errors at the price of HIGH-class recall falling to ~0.58 (0.580) — roughly
four in ten fast users get a plainer page than they needed. The brief calls that
cheap. Whether it is *that* cheap is my call, and the chosen values plus the
reasoning go in `MODEL_CARD.md`.

**Confidence guards the model's uncertainty, not the input's validity.**

This is the most important result of the project so far, and it was found on
first contact with a real network rather than by any test.

The entire cost-asymmetry design exists to stop a slow client being called
fast — class weights during training, an expected-cost decision rule instead of
argmax, four fallback conditions. On the first harness run against real
services, `classify()` returned **HIGH at 0.967 confidence with
`fallback=False`**. The probe had completed too fast to time on loopback and
reported 524,288,000 kbps: `log10` **8.72**, against a training range of
**1.262 – 5.905**. Nearly three decades outside support.

**The model was not wrong.** It was asked about a region it had never seen, and
a tree ensemble answers such questions confidently, from whichever leaf sits at
the boundary. A model expresses doubt only about regions it was *trained* on;
input from outside produces confident nonsense, and neither the class weights
nor the expected-cost rule can help, because both operate on probabilities the
model had no basis to produce. Nothing downstream noticed, because nothing
downstream was looking at the input.

Fallback condition 5 (§4.1) is the fix, and it generalises: the bundle now
carries per-feature training ranges and `classify()` declines to answer outside
them. The throughput ceiling clamp closes one instance; the condition closes the
class. It is not hypothetical for other features either — loopback RTT measures
~3.0 ms against a trained minimum of 3.018 ms.

**For the write-up:** cost-sensitivity and confidence thresholds are guards on
the model's *answer*. They are silent about whether the *question* was one the
model can answer at all. Any deployed classifier needs a support check as well,
and ours only exists because we ran the thing for real.

**Corollary, and the more transferable lesson: do not sanitise an input upstream
of the check that validates it.**

The first fix for the above clamped throughput at both ends of the training
range. It did not merely fail to help — it *removed the evidence the safety
check needed*. By making 524,288,000 kbps look like a reasonable
top-of-range value before `classify()` ever saw it, the clamp converted a
detectable anomaly into an undetectable one. Condition 5 then found nothing
wrong, `classify()` returned HIGH with `fallback=False` exactly as before, and
**the test suite went green over a defect that was still fully present.** Only
running the harness against real services showed it.

A sanitiser placed upstream of a validity check does not protect the check; it
blinds it. If a value must be both bounded and validated, validate first, or
bound in a way the validator can still see. Generalise this beyond the
estimator — it applies anywhere a guard reads a value something else has already
normalised.

**Determinism: reproducible decisions, not reproducible outcomes.**

§3.10 rule 5 asks for deterministic behaviour given `seed`, and the client loop
cannot fully deliver that — so the write-up must not claim it does.

*Reproducible:* every client-side random choice comes from an RNG seeded on
`(seed, client_id)`. There is no unseeded randomness anywhere in `client/`. The
same seed makes the same decisions given the same inputs.

*Not reproducible:* wall-clock timing, network RTT, the completion order of
parallel sub-resource fetches, whether a transfer beats `expires_at`, and
asyncio scheduling order. Two runs at the same seed will make identical choices
and can still finish differently.

State it in those words. The training pipeline (`synthdata` → `train`) **is**
fully deterministic given a seed and reproduces byte-identical metrics; the
live client is not, and conflating the two would overclaim.

**Known measurement property: not every client gets classified, and the ones
that miss out are not a random sample.**

A client needs `min_rtt_samples` (5) status polls before it can be classified,
and `poll_interval_ms` is 2000 — so roughly **8–10 seconds of queueing** before
an estimate is possible at all. A client admitted faster than that never
qualifies, never submits, and takes M1's MEDIUM default.

That population is systematically skewed. Short waits happen during ramp-up and
in the tail, and they happen disproportionately to clients the queue could
serve quickly. The classifier therefore engages most where the queue is
longest — which is where it matters — but it means **a headline accuracy figure
averaged over all clients would be computed partly over clients that were never
classified.** That number would be meaningless, and it would look fine.

M3 can separate the three populations from the event log as it stands, with no
contract change:

| Population | How to identify it |
|---|---|
| Classified | `ESTIMATE` with `fallback == false` |
| Estimated but low-confidence | `ESTIMATE` with `fallback == true` |
| Never classified | a `JOIN` with no `ESTIMATE` for that `ticket_id` |

**Report classifier accuracy over the classified subset only, and report the
never-classified fraction beside it as its own number.** Do not blend them.

### 5.2 Open questions — resolve before the shipping model is trained

1. ~~**`loss_ratio` vs `fail_ratio` are identical as specified.**~~ **Closed
   2026-09-06.** Dropped `loss_ratio`, down to 7 features, keeping `fail_ratio`.
   Two identical columns give a tree model nothing and split feature importance
   between duplicates, understating how much loss matters; and no honest
   packet-loss proxy can be derived from `LinkSample`'s fields, so a removal is
   easier to defend than a fabricated derivation. This was a C1-owner call, not
   a team decision: the shared contract owns `LinkEstimate.loss_ratio` as a
   reported field, not the internal feature vector. Model retrained as
   `v2-synthetic-7f`. Reasoning recorded in `features.py`, not deleted.

2. **The netem profiles may make classification trivially easy.** M3's testbed
   defines HIGH as 50 Mbit / 15 ms / 0.01% loss and LOW as 512 kbit / 250 ms / 3%
   loss. Measured accuracy on that data will likely be 98–99%, and an examiner
   will rightly ask why a model is needed at all. Two honest responses: ask M3 for
   intermediate or time-varying profiles, or state plainly in the report that the
   testbed makes classification easy by construction, that real links are far
   messier, and that the confidence threshold plus fallback is what makes the
   design safe when it isn't easy. Raise with M3 early, while the testbed is still
   being built.

3. ~~**Local stubs need removing.**~~ **Resolved 2026-09-04.** Both stand-ins
   are gone: `RawSample` in `features.py` is now `LinkSample` from
   `aaac.common.schemas`, and the local `AccessClass` in `synthdata.py` is now
   `aaac.common.classes.AccessClass` (`train.py` imports it from there too).
   The swap was verified behaviour-neutral: `models/metrics.json` from
   `--n 12000 --seed 1` is byte-identical before and after. No stubs remain in
   `estimator/`.

### 5.3 Build order

Per the brief, C3 before C1: a 10× byte reduction helps LOW-class users more than
any classifier accuracy does, and it works even when the model is mediocre. It is
the bigger lever *and* the easier one.

| Week | Deliverable | Done when |
|---|---|---|
| 1 | Client SDK, estimator stubbed to MEDIUM | M3 can generate load |
| 2 | Payload variants + `/result` + token verification | 10× ratio test green |
| 3 | Probe routine and feature extraction | real `LinkEstimate` values reaching M1's queue |
| 3–4 | Classifier on synthetic, then on M3's measured traces | confusion matrix reported, optimistic errors minimised |
| 4 | Fallback verified | model deleted, run still completes |
| 5 | Dashboard | side-by-side demo runs unattended for 10 minutes |
| 6 | Model card + support for full evaluation runs | — |

---

## 6. Environment

Windows, PowerShell. Virtualenv at `.venv`, Python 3.11.

```powershell
.\.venv\Scripts\Activate.ps1
$env:PYTHONPATH="src"
python -m aaac.estimator.train --n 12000 --seed 1
```

Note for command generation: this is **PowerShell, not bash**. No `source`, no
`touch`, no `mkdir -p`, no `export`. Use `.\.venv\Scripts\Activate.ps1`,
`New-Item -ItemType File`, `mkdir a\b, c`, `$env:VAR="x"`, and `python -m pip`
rather than bare `pip`.

---

## 7. Skills to create

Create these under `.claude/skills/`. **Show me each SKILL.md before writing it.**

### 7.1 `contract-guard`

Triggers whenever a change touches `src/aaac/common/`, the schemas, the token
format, the event vocabulary, or any endpoint path or payload shape.

Behaviour: stop, quote the exact contract clause from §3 of this file, explain
what would break in M1's or M3's package, and ask whether to proceed as a
`CONTRACT CHANGE` requiring notification of both other owners. Never make the
change silently.

### 7.2 `model-card`

Triggers on requests to write, refresh, or check `models/MODEL_CARD.md`.

Generates the card from `models/metrics.json` and the exported bundle rather than
from memory, and requires every one of: feature order (verbatim from
`FEATURE_NAMES`), training data source and size, hyperparameters, measured
accuracy, full confusion matrix, per-class recall, `optimistic_error_rate`, the
cost matrix with its justification, and the documented fallback behaviour. Refuses
to emit a card with any section missing, and states clearly whether the numbers
came from synthetic or measured data.

### 7.3 `payload-budget`

Triggers on any edit to `delivery/templates/` or `delivery/variants.py`.

After the edit, renders each variant, measures the byte size, asserts each is
inside its budget from `configs/run.yaml`, asserts `size(full)/size(essential) >= 10`,
and parses the `essential` output to confirm it contains zero `<script>`, `<link>`,
`<img>` or any other sub-resource reference. Reports actual byte counts every time,
because those counts are the evidence for the C3 claim in the write-up.
