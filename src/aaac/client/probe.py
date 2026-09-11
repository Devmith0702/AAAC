"""In-band link measurement (C1, section 4.1): probe, RTT collection, estimate.

Everything here is derived from traffic the client generates anyway while it
waits in the queue. One timed download, and the timing of the ordinary status
polls. No speed-test screen, no question to the user, no IP heuristic, no
personal data. That constraint is a claim the proposal makes explicitly.

THREE THINGS WORTH READING BEFORE CHANGING ANY OF THIS
-----------------------------------------------------

1. `probe_duration_ms` is FIRST BYTE TO LAST BYTE. Not wall clock.

   synthdata.py builds its training label as

       probe_duration_ms = (PROBE_BYTES * 8.0) / tput_kbps

   which is pure serialisation delay with no latency component at all. If the
   live probe reported wall clock instead, it would fold in the handshake and
   the time-to-first-byte, and the error would be worst exactly where it hurts:
   a HIGH client's 64 KB transfer takes ~26 ms, so ~22 ms of RTT would nearly
   halve its apparent throughput and push it toward MEDIUM. That is train/serve
   skew one layer below the features. Measuring the transfer window only keeps
   the live number and the trained number talking about the same quantity.

   It also keeps the features honest: RTT already has three features of its
   own, so folding latency into throughput would merely correlate them.

   Known and accepted: TCP slow-start falls inside a 64 KB window, so this
   slightly understates steady-state bandwidth. It does so uniformly across
   classes, so it shifts no decision boundary.

2. A FAILED PROBE IS A SIGNAL, NOT A MISSING ONE.

   A client that cannot pull 64 KB off a 3%-loss link has told us something
   important about that link. Falling back to MEDIUM there would be an
   OPTIMISTIC error -- a bigger page and a shorter window handed to precisely
   the client least able to survive them, which is the exclusion this project
   exists to remove. So a partial transfer is reported as the low throughput it
   actually is, and the failure is additionally carried by `failed_requests`.
   `fallback=True` is reserved for "no view could be formed", never for "the
   view is bad news".

3. THE ESTIMATE IS SUBMITTED ONCE, ON THE FIRST ATTEMPT ONLY.

   See `EstimateGate`. The narrow rule is deliberate and is not simply a
   reading of section 4.3 -- it protects M1's I2 invariant. Details there.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx

from aaac.common.schemas import LinkEstimate, LinkSample

#: Lowest throughput synthdata.py ever generates (it floors at 30.0 kbps).
#: Live readings are clamped up to it so the model is never asked about a
#: region of the feature space it was not trained on.
TRAINING_FLOOR_KBPS = 30.0

#: Highest throughput the generator produces (measured: n=12000, seed=1 tops out
#: at ~803,000 kbps). Recorded for reference and used by tests -- but DELIBERATELY
#: NOT applied as a clamp. See to_link_sample() for why the two ends of the
#: distribution are not treated alike.
TRAINING_CEILING_KBPS = 800_000.0

#: A transfer window shorter than this is not a measurement. On a fast or
#: loopback link the whole 64 KB can arrive in one chunk, making first-byte and
#: last-byte the same instant; that is an absence of timing information, not
#: infinite bandwidth.
MIN_MEASURABLE_MS = 1.0

#: Re-run the probe once if the client is still waiting after this long: a
#: measurement taken a minute ago may describe a different network (section 4.1).
PROBE_REFRESH_AFTER_S = 60.0


@dataclass
class ProbeResult:
    """One timed probe download."""

    bytes_received: int
    duration_ms: float
    ok: bool
    error: str = ""

    @property
    def throughput_kbps(self) -> float:
        if self.duration_ms <= 0 or self.bytes_received <= 0:
            return 0.0
        return (self.bytes_received * 8.0) / self.duration_ms

    @property
    def degenerate(self) -> bool:
        """True when the transfer completed too fast to time.

        Not "very fast": unmeasurable. The bytes arrived in a single read, so
        there is no window to divide by. Callers must not read throughput_kbps
        as a bandwidth estimate when this is set.
        """
        return self.bytes_received > 0 and self.duration_ms < MIN_MEASURABLE_MS


async def run_probe(
    client: httpx.AsyncClient,
    base_url: str,
    n_bytes: int,
    *,
    timeout_s: float = 30.0,
) -> ProbeResult:
    """Time a `GET /probe/{n_bytes}` from first byte to last byte.

    Streams so the clock can start when the first byte lands rather than when
    the request is issued. A stall part-way through still yields a usable
    reading: the bytes that did arrive, over the time they took.
    """
    url = f"{base_url.rstrip('/')}/probe/{n_bytes}"
    received = 0
    first_byte_at: float | None = None
    last_byte_at: float | None = None

    try:
        async with client.stream("GET", url, timeout=timeout_s) as response:
            if response.status_code != 200:
                await response.aclose()
                return ProbeResult(0, 0.0, False, f"status {response.status_code}")
            async for chunk in response.aiter_bytes():
                now = time.perf_counter()
                if first_byte_at is None:
                    first_byte_at = now
                last_byte_at = now
                received += len(chunk)
    except httpx.HTTPError as exc:
        # Partial data is still a measurement. Nothing at all is a failure.
        if first_byte_at is not None and last_byte_at is not None and received > 0:
            return ProbeResult(
                received,
                max((last_byte_at - first_byte_at) * 1000.0, 0.001),
                False,
                type(exc).__name__,
            )
        return ProbeResult(0, 0.0, False, type(exc).__name__)

    if first_byte_at is None or received == 0:
        return ProbeResult(0, 0.0, False, "empty response")

    duration_ms = max((last_byte_at - first_byte_at) * 1000.0, 0.001)
    return ProbeResult(received, duration_ms, received >= n_bytes)


async def timed_poll(
    client: httpx.AsyncClient,
    url: str,
    *,
    timeout_s: float = 10.0,
) -> tuple[httpx.Response | None, float, bool]:
    """Issue one status poll and time it. Returns (response, rtt_ms, ok).

    The response is tiny, so its round trip approximates RTT. This is the whole
    RTT mechanism -- there is no separate ping, because a separate ping would
    be traffic the client would not otherwise have generated.
    """
    t0 = time.perf_counter()
    try:
        response = await client.get(url, timeout=timeout_s)
    except httpx.HTTPError:
        return None, (time.perf_counter() - t0) * 1000.0, False
    rtt_ms = (time.perf_counter() - t0) * 1000.0
    ok = response.status_code < 500
    return response, rtt_ms, ok


@dataclass
class LinkObservation:
    """Accumulates what the client has learned about its own link."""

    rtt_samples_ms: list[float] = field(default_factory=list)
    failed_requests: int = 0
    total_requests: int = 0
    probe: ProbeResult | None = None
    probe_taken_at: float | None = None

    def record_request(self, ok: bool, rtt_ms: float | None = None) -> None:
        """Log one request. A failed request contributes no timing sample."""
        self.total_requests += 1
        if not ok:
            self.failed_requests += 1
            return
        if rtt_ms is not None and rtt_ms > 0:
            self.rtt_samples_ms.append(float(rtt_ms))

    def record_probe(self, result: ProbeResult, *, at: float | None = None) -> None:
        self.probe = result
        self.probe_taken_at = at if at is not None else time.monotonic()
        self.total_requests += 1
        if not result.ok:
            self.failed_requests += 1

    def probe_is_stale(self, *, now: float | None = None) -> bool:
        """True if the wait has outlived the measurement (section 4.1)."""
        if self.probe_taken_at is None:
            return False
        clock = now if now is not None else time.monotonic()
        return (clock - self.probe_taken_at) > PROBE_REFRESH_AFTER_S

    def has_enough_rtts(self, min_rtt_samples: int) -> bool:
        return len(self.rtt_samples_ms) >= min_rtt_samples

    def to_link_sample(self, ticket_id: str) -> LinkSample:
        """Assemble the LinkSample the estimator consumes.

        The floor: a reading below TRAINING_FLOOR_KBPS is clamped up to it by
        shortening the reported duration. The model has never seen anything
        slower, and a tree asked about a region outside its training support
        gives an answer nobody has tested. Clamping costs nothing here, because
        everything at or below the floor is "the worst link we know how to
        describe" and classifies the same way.

        The one case this cannot cover is a probe that returned ZERO bytes:
        there is no bytes/duration pair that expresses "nothing arrived", and
        inventing one would put fabricated numbers into a LinkSample that M1
        logs and M3 reads. That case is reported honestly as 0 and sits just
        below training support; it routes to the lowest leaf, which is LOW, the
        safe direction. `failed_requests` carries the failure regardless.

        TODO before the measured-trace retrain: synthdata.py should model probe
        failure explicitly, so the zero-byte case is inside the training
        distribution rather than just below it.
        """
        probe_bytes = 0
        probe_duration_ms = 0.0

        if self.probe is not None and self.probe.bytes_received > 0:
            probe_bytes = self.probe.bytes_received
            probe_duration_ms = self.probe.duration_ms

            # THE FLOOR IS CLAMPED. THE CEILING IS NOT. The asymmetry is
            # deliberate, and it mirrors the project's cost asymmetry.
            #
            # Too slow to be in training: clamping reports the link as the
            # slowest thing the model knows, which is LOW. Correct, and safe.
            # Falling back to MEDIUM instead would be an UPGRADE -- the
            # optimistic error, handed to the client least able to absorb it.
            #
            # Too fast to be in training: clamping would report the link as the
            # fastest thing the model knows, which is HIGH. That is the
            # optimistic error, delivered with confidence, from a measurement
            # that carried no information at all. So the raw value is left
            # alone and fallback condition 5 catches it in infer.classify(),
            # which lands on MEDIUM -- the cheap direction.
            #
            # A first attempt DID clamp both ends. It made the harness stop
            # printing an absurd number while still returning HIGH with
            # fallback=False, which fixed the symptom and left the defect.
            max_duration = (probe_bytes * 8.0) / TRAINING_FLOOR_KBPS
            probe_duration_ms = min(probe_duration_ms, max_duration)

        return LinkSample(
            ticket_id=ticket_id,
            probe_bytes=probe_bytes,
            probe_duration_ms=probe_duration_ms,
            rtt_samples_ms=list(self.rtt_samples_ms),
            failed_requests=self.failed_requests,
            total_requests=max(self.total_requests, 1),
        )


class EstimateGate:
    """Decides whether an estimate may be submitted. Once, first attempt only.

    Section 4.3 says the estimate goes in once, as soon as the probe has enough
    RTTs, and that later attempts do not re-estimate.

    The narrow reading -- first attempt only -- is deliberate, and the reason is
    M1's, not ours. His I2 invariant is that a class is never upgraded. A client
    downgraded HIGH -> MEDIUM on a timeout, re-queued, and then submitting a
    fresh HIGH estimate on attempt 2 would be an upgrade arriving through a
    legitimate endpoint. `/queue/estimate` is guarded by requiring WAITING, but
    a re-queued ticket is WAITING again, so that guard may not close it.

    The cost of the narrow rule: a client admitted before it collects
    `min_rtt_samples` never estimates at all, and M1's MEDIUM default applies.
    That is the same outcome as submitting a fallback, without the invariant
    exposure. Raised with M1 as a question; widen only if he says the endpoint
    rejects attempt > 1.
    """

    def __init__(self, min_rtt_samples: int) -> None:
        self.min_rtt_samples = min_rtt_samples
        self.submitted = False
        self.never_qualified = False

    def may_submit(self, observation: LinkObservation, attempt: int) -> bool:
        if self.submitted or attempt != 1:
            return False
        return observation.has_enough_rtts(self.min_rtt_samples)

    def mark_submitted(self) -> None:
        self.submitted = True

    def mark_admitted_without_estimate(self) -> None:
        """The client was served before it could be classified.

        Counted so the never-classified population can be reported separately.
        An accuracy figure averaged over clients that were never classified is
        worse than no figure at all.
        """
        if not self.submitted:
            self.never_qualified = True


async def submit_estimate(
    client: httpx.AsyncClient,
    admission_base: str,
    estimate: LinkEstimate,
    *,
    timeout_s: float = 10.0,
) -> dict | None:
    """POST the estimate to M1's admission service. Returns its reply, or None.

    A refused estimate is not fatal: M1 defaults to MEDIUM, which is the same
    place a fallback lands. The client keeps waiting either way.
    """
    url = f"{admission_base.rstrip('/')}/queue/estimate"
    try:
        response = await client.post(
            url, json=estimate.model_dump(mode="json"), timeout=timeout_s
        )
    except httpx.HTTPError:
        return None
    if response.status_code >= 400:
        return None
    try:
        return response.json()
    except ValueError:
        return None
