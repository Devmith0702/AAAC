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

**Eight features, fixed order.** The order is a contract; the model learned
position-by-position. Any change forces a `model_version` bump and a model card
update.

```
log10(throughput_kbps), rtt_mean_ms, rtt_p95_ms, rtt_jitter_ms,
loss_ratio, fail_ratio, stability, n_rtt_samples
```

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
3. the top class probability is below `confidence_threshold`.

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
- Accurate `Content-Length` on every response — M3 computes goodput from it.
- Variant comes **only** from the verified token's `var` field.

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

As of the last session:

**Done, in `src/aaac/estimator/`:**

- `features.py` — the eight features in fixed order, plus `RawSample` (a local
  stand-in for `LinkSample`). Both training and inference call
  `extract_features()`, which is what prevents train/serve skew.
- `synthdata.py` — synthetic labelled generator. Simulates *behaviour* (a probe
  that took 1,100 ms, polls at 190/450/260 ms, two failures) and derives features
  from it, rather than fabricating feature values directly.
- `train.py` — LightGBM training, cost-sensitive decision rule, metrics, joblib
  export to `models/link_classifier.joblib`.

**Not started:** `infer.py`, the probe routine, delivery variants, the SDK, the
dashboard, all tests.

**Blocked:** `src/aaac/common/` does not exist. Thisaru has not pushed it. The SDK
cannot be written until `schemas.py` and `classes.py` land.

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
Current figures: 4.67% under naive argmax, 1.13% under the cost-sensitive rule.

**The cost matrix is provisional.** Current settings buy that 4× reduction in
optimistic errors at the price of HIGH-class recall falling to ~0.58 — roughly
four in ten fast users get a plainer page than they needed. The brief calls that
cheap. Whether it is *that* cheap is my call, and the chosen values plus the
reasoning go in `MODEL_CARD.md`.

### 5.2 Open questions — resolve before the shipping model is trained

1. **`loss_ratio` vs `fail_ratio` are identical as specified.** `LinkSample` only
   carries `failed_requests / total_requests`, which is the brief's definition of
   `loss_ratio`. As written the two features are the same number and one is dead
   weight. Either drop one (down to 7 features) or define them distinctly. Feature
   order goes in the model card, so this must be settled first. Marked with a
   `NOTE` in `features.py`.

2. **The netem profiles may make classification trivially easy.** M3's testbed
   defines HIGH as 50 Mbit / 15 ms / 0.01% loss and LOW as 512 kbit / 250 ms / 3%
   loss. Measured accuracy on that data will likely be 98–99%, and an examiner
   will rightly ask why a model is needed at all. Two honest responses: ask M3 for
   intermediate or time-varying profiles, or state plainly in the report that the
   testbed makes classification easy by construction, that real links are far
   messier, and that the confidence threshold plus fallback is what makes the
   design safe when it isn't easy. Raise with M3 early, while the testbed is still
   being built.

3. **Local stubs need removing.** `AccessClass` and `RawSample` are temporary
   copies inside `estimator/`. Field names match M1's schema exactly so the swap
   is a two-line import change. Delete them the day `common/` lands — do not let
   them become permanent.

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
