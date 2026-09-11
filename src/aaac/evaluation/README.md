# M3 — Evaluation

Owner: Devmith. Scope: mock origin, netem testbed, load generation, metrics,
figures, report. See `CLAUDE.md` §4.

---

## Pre-registered falsification rule

**Written before the full experiment is executed, as CLAUDE.md §4.7 requires.**
Nothing below may be edited once a full run has been executed. If it has to
change, the change is dated, the reason is recorded here, and the earlier version
stays visible in git history.

### Hypothesis

> AAAC substantially reduces Δ relative to the access-blind baseline while
> keeping origin stability and not materially degrading HIGH-class completion
> time.

Where **Δ = completion_rate[HIGH] − completion_rate[LOW]**, grouped by
`true_class` (the netem profile actually configured), never by the estimated
class.

### Design

Three modes — `none`, `baseline`, `aaac` — over a minimum of **5 seeds**. All
three modes replay the *same* population per seed (`results/population-{seed}.json`),
so the comparison is **paired**. Each run records the SHA-256 of the population it
was driven with, and the harness refuses to compare runs whose hashes differ.

### Decision rule

**SUPPORTED** requires all three of the following:

1. **Effect.** mean Δ(aaac) < mean Δ(baseline), and the paired 95% confidence
   interval of the per-seed difference `Δ(baseline) − Δ(aaac)` **excludes zero**.
   The interval is a t-interval on the paired differences; a bootstrap interval
   is reported alongside when the differences are visibly skewed.
2. **Origin stability.** mean origin 5xx rate under `aaac` ≤ mean under
   `baseline`.
3. **No material HIGH-class regression.** HIGH-class p95 time-to-completion under
   `aaac` is within **20% relative** of `baseline`.

**NOT SUPPORTED** in every other case. `report.py` prints `HYPOTHESIS NOT
SUPPORTED` and names which of the three conditions failed.

The 20% margin in (3) is the pre-registered value. It is stated here rather than
chosen after seeing the data, which is the entire point of writing this down
first.

### Statistical test

Paired t-test on the per-seed differences, because modes share a seed (§4.5).

**The Wilcoxon signed-rank test is not usable at 5 seeds.** The exact two-sided
test on n = 5 pairs has 2⁵ = 32 sign assignments, so its smallest attainable
p-value is 2/32 = 0.0625 — it cannot reject at α = 0.05 no matter how large the
effect. `stats.wilcoxon_signed_rank` attaches that warning to its own result and
`report.py` refuses to present a Wilcoxon p-value it could never have rejected.
If a rank test is wanted as the primary, the run needs ≥ 6 seeds.

### Things that would invalidate a result, and are checked mechanically

- Modes driven by different populations → the harness aborts on a hash mismatch.
- A mode appearing in another mode's event log → `EventLog.single_mode()` raises.
- `true_class` missing from events → per-class metrics are incomplete and say so.
- The testbed unverified → `make experiment` runs `verify-testbed` first and
  stops on failure.
- Fabricated data reaching the report → synthetic logs must be named
  `synthetic-*.jsonl` and `report.py` prints a banner if it sees one.

### What a negative result looks like

The proposal commits in writing to reporting one. Two specific outcomes must go
in the **body** of the report, not a footnote:

- Δ does not narrow, or narrows but the paired CI includes zero.
- **Δ narrows while aggregate completion falls.** §4.7 flags this trade-off
  directly: longer LOW windows consume admission throughput. If the gap closes
  because HIGH got worse rather than LOW got better, that is the finding.

`report.py` computes aggregate completion rate alongside Δ for exactly this
reason.

---

## Layout

| Path | What it is |
|---|---|
| `access_class.py` | **STUB** of §3.3 — delete when M1 ships `common/classes.py` |
| `testbed/profiles.py` | The three link profiles. Single source of truth |
| `testbed/netem.sh` | Applies the qdisc chain; fails loudly if `tc` no-ops |
| `testbed/verify.py` | `make verify-testbed` — the §4.2 gate |
| `population.py` | Replayable client population; cannot see `mode` |
| `loadgen.py` | Drives M2's `run_client` on the population's schedule; cannot see `mode` |
| `events.py` | The only route from event log to number |
| `metrics.py` | Every §4.4 metric, disaggregated by `true_class` |
| `stats.py` | t-distribution, bootstrap, paired tests — no scipy |
| `synth.py` | **FABRICATED DATA** for testing the analysis chain |
| `falsification.py` | The decision rule above, in code |

## Running

```bash
make install
make check                 # lint, types, unit tests
make up                    # start origin + redis + iperf + shaped clients
make verify-testbed        # THE GATE — no experiment runs until this passes
make experiment SEEDS=1,2,3,4,5
make figures report
```

## Known limitations, recorded as they are found

1. **Docker Desktop on macOS.** Containers run in a Linux VM, so `tc` shaping is
   second-hand and adds an uncontrolled virtual hop. `make verify-testbed` is the
   only thing that makes the testbed trustworthy here; run it every time.
2. **Downstream shaping needs the `ifb` kernel module.** If it is unavailable,
   `netem.sh` refuses to fall back silently — downstream is the direction the
   450 KB page travels, and an egress-only run does not measure the completion
   gap at all.
3. **TCP cannot reach the LOW profile's nominal rate.** At 250 ms and 3% loss the
   Mathis ceiling is ≈270 kbit/s against a configured 512 kbit/s. The rate gate
   therefore measures the shaper with UDP; TCP goodput is reported separately,
   and its shortfall is part of *why* LOW clients fail.
4. **Origin service time is deterministic per index number.** Replay requires it
   (§3.10 rule 5), but it means a retry for the same index draws the same service
   time — service time is correlated across attempts rather than independent.
5. **`ORIGIN_SAMPLE` is joined on timestamp across containers.** Clock skew is a
   real risk; `EventLog.clock_skew_warning()` reports when the two logs do not
   overlap as expected.
6. **Goodput from M1's log is an upper bound.** `TIMEOUT` events carry no `bytes`,
   so bytes burned on timed-out attempts are missing from the denominator. The
   provenance block says so whenever it applies. See `docs/INTEGRATION-ISSUES.md` A4.
7. **Classifier coverage is incomplete.** Rejected estimates are not logged, so a
   ticket with no `ESTIMATE` may be unclassified or rejected. The provenance block
   states how many tickets classifier accuracy covers.
