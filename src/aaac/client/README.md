# Running the client harness

One client, real services, narrated end to end. Use it to check that a
half-finished system is wired up before running load through it.

```powershell
.\.venv\Scripts\Activate.ps1
$env:PYTHONPATH="src"
$env:AAAC_TOKEN_SECRET="pick-something"

# terminal 1 - the delivery service
$env:AAAC_ORIGIN_BASE="http://127.0.0.1:8002"     # see trap 1 below
python -m uvicorn aaac.delivery.app:app --port 8001

# terminal 2 - one client
python -m aaac.client.harness --stub-origin
```

Useful flags: `--delivery-only` (skip the admission service entirely),
`--variant-class HIGH|MEDIUM|LOW` (which token to mint in delivery-only mode),
`--stub-origin` (run a throwaway origin so `/result` can render before M3's
service exists), `--admission` / `--delivery` / `--origin` to point elsewhere.

Exit codes: `0` completed, `1` the client failed, `2` infrastructure was
missing or broken.

---

## Three traps that will each cost you an hour

These were all hit on the first real run. None of them shows up against a mock.

### 1. A green preflight against an address the service never uses

**Symptom.** Preflight prints `[OK] origin HTTP 200`, and then `/result`
returns **502** after a ~3 second pause.

**Cause.** The delivery service resolves the origin from `AAAC_ORIGIN_BASE`,
which defaults to `http://origin:8002` — a **Docker Compose hostname**. Outside
Docker that name does not resolve. Preflight checks `127.0.0.1:8002`, so it is
testing a different address than the service actually calls. The pause is DNS
failing.

**Fix.** Start the delivery service with the address it should really use:

```powershell
$env:AAAC_ORIGIN_BASE="http://127.0.0.1:8002"
```

**Worth remembering generally:** a health check that probes a different address
than the service uses will confirm your assumption rather than test it.

### 2. A stale uvicorn silently serving the old configuration

**Symptom.** You change an environment variable, restart the service, and
nothing changes. The old behaviour persists exactly.

**Cause.** The new process failed to bind and exited, while the old one kept
serving:

```
ERROR: [Errno 10048] error while attempting to bind on address
('127.0.0.1', 8001): only one usage of each socket address ... is permitted
```

If uvicorn's output is redirected to a log you are not reading, this is silent.
`pkill` does not reliably kill it on Windows.

**Fix.** Find the listener and kill it by PID:

```powershell
netstat -ano | Select-String ':8001.*LISTENING'
taskkill /PID <pid> /F
```

Then confirm the new process actually started before trusting a result.

### 3. On loopback the probe is unmeasurably fast, and that used to mean "HIGH"

**Symptom.** The harness prints a probe duration of `0.0 ms` and a throughput in
the hundreds of Gbit/s, with a `WARNING` block underneath.

**Cause.** All 64 KB arrives in a single read, so first-byte and last-byte are
the same instant. There is no transfer window to divide by. This is an
**absence of timing information**, not bandwidth.

**Why it matters far beyond loopback.** Before this was caught, that reading
produced `access_class=HIGH, confidence=0.967, fallback=False` — a confident
answer from a feature value nearly three decades outside anything in the
training set. Two guards now exist: the reading is clamped to the top of the
training distribution (`TRAINING_CEILING_KBPS`), and `classify()` falls back to
MEDIUM whenever **any** feature falls outside its trained range (fallback
condition 5, `models/README.md`).

**Fix.** Nothing to fix — expected on loopback. It should not occur against a
netem-shaped link, where 64 KB takes at least ~10 ms even at 50 Mbit. **If you
see the warning on a shaped link, the shaping is not applied.** That is the
useful signal here.

Note the same applies to RTT: loopback measures ~3.0 ms against a trained
minimum of 3.018 ms, so a loopback run may fall back on RTT alone.

---

## What the harness does not do

It runs **one** client. It is a wiring check and a demo, not a load generator —
load generation is M3's (§4.7). For many clients, drive
`aaac.client.sdk.run_client` directly: one coroutine per client, each owning its
own connection pool, returning a `ClientOutcome` and never raising.

The delivery-only path **mints its own admit token**. That is a harness-only
capability for demonstrating C3 without an admission service; it needs
`AAAC_TOKEN_SECRET`. The SDK never mints a token and never infers a class from
anything but a verified one (§3.7).

`--stub-origin` runs a throwaway origin. That is M3's component (§1.4); the stub
exists only so the C3 path is demonstrable before it ships, implements no load
shedding, and should be deleted once M3's origin exists.
