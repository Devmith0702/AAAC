"""The client SDK: one waiting student, end to end (section 4.3).

This is the stand-in browser M3's load generator drives. One coroutine per
client: join, probe, poll, estimate, fetch, complete.

MODE BLINDNESS IS THE PROPERTY THAT MATTERS MOST
------------------------------------------------
Section 3.4 requires `none`, `baseline` and `aaac` to run through the same code
paths, with the difference living entirely in M1's config and behaviour. If this
module ever branched on mode -- or on anything correlated with it -- the
three-way comparison would be invalid and the evaluation worthless.

It cannot branch on mode, because mode appears in nothing the client reads:
`/queue/join` returns ticket_id, join_seq, position, eta_s and poll_interval_ms;
`TicketStatus` has no mode field; the token payload is tid/cls/att/exp/var. The
client obeys what it is told -- the window it is given, the variant its token
authorises -- and never infers why it was told that. Both properties are tested,
statically and behaviourally, in tests/client/test_mode_blind.py.

`none` mode depends on M1 returning an immediately-ADMITTED ticket rather than
expecting the client to skip the queue. Raised with M1: if it is built the other
way, mode blindness is impossible.

`true_class` is an opaque passthrough for M3's scoring (section 3.8). It is sent
on join and never read again. Nothing here may branch on it.

DETERMINISM, HONESTLY
---------------------
Rule 5 asks for deterministic behaviour given a seed. What this delivers is
reproducible DECISIONS, not reproducible OUTCOMES. Every client-side random
choice comes from an RNG seeded on (seed, client_id), and there is no unseeded
randomness. But wall-clock timing, network RTT, the completion order of parallel
sub-resource fetches, and whether a transfer beats `expires_at` are all real and
none of them repeat. Two runs at the same seed make the same choices and can
still finish differently.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

import httpx
from pydantic import ValidationError

from aaac.client.fetch import fetch_page
from aaac.client.probe import (
    EstimateGate,
    LinkObservation,
    run_probe,
    submit_estimate,
    timed_poll,
)
from aaac.common.classes import AccessClass
from aaac.common.schemas import TicketStatus
from aaac.estimator.infer import classify

#: Give up on a admission service that has failed this many times in a row.
#: Hammering a dead server produces noise, not data.
MAX_CONSECUTIVE_ADMISSION_FAILURES = 5

#: Floor on the poll interval, so a misconfigured server cannot make the client
#: spin. Not a behaviour change: M1 supplies 2000 ms.
MIN_POLL_INTERVAL_MS = 50.0


class Outcome(str, Enum):
    """How one client's session ended.

    ADMISSION_UNAVAILABLE is deliberately NOT a timeout and NOT an abandonment.
    A half-built or crashed admission service must never be countable as a
    client that failed to finish: the event log is written by M1 (section 3.8),
    so if M1 is down there is no log at all, and the only place this distinction
    can live is here in the return value. Blurring it would move the headline
    gap for a reason that has nothing to do with the system under test.
    """

    COMPLETED = "COMPLETED"
    TIMED_OUT = "TIMED_OUT"
    ABANDONED = "ABANDONED"
    EXPIRED = "EXPIRED"
    ADMISSION_UNAVAILABLE = "ADMISSION_UNAVAILABLE"
    ORIGIN_UNAVAILABLE = "ORIGIN_UNAVAILABLE"
    COMPLETED_UNREPORTED = "COMPLETED_UNREPORTED"


@dataclass
class ClientOutcome:
    """Raw facts about one client's session. Metrics are M3's (section 4.7)."""

    client_id: str
    outcome: Outcome
    ticket_id: str | None = None
    attempts: int = 0
    bytes: int = 0
    duration_ms: float = 0.0
    variant: str = ""
    requests: int = 0
    polls: int = 0
    estimate_submitted: bool = False
    never_classified: bool = False
    estimate_fallback: bool | None = None
    wall_time_s: float = 0.0
    error: str = ""
    transfer_failures: int = 0


@dataclass
class _Session:
    """Mutable state for one client. Kept separate to keep run_client readable."""

    observation: LinkObservation = field(default_factory=LinkObservation)
    consecutive_failures: int = 0
    probe_reruns: int = 0
    polls: int = 0
    attempts: int = 0
    transfer_failures: int = 0


def _default_client_factory(base_url: str) -> Callable[[], httpx.AsyncClient]:
    def make() -> httpx.AsyncClient:
        # One connection pool per client. Sharing a pool across 20,000 clients
        # would collapse them onto a handful of connections and make per-client
        # link shaping fiction.
        return httpx.AsyncClient(base_url=base_url, timeout=30.0)

    return make


async def run_client(
    *,
    client_id: str,
    true_class: AccessClass,
    index_no: str,
    admission_base: str,
    delivery_base: str,
    probe_bytes: int = 65536,
    min_rtt_samples: int = 5,
    confidence_threshold: float = 0.60,
    model_path: str = "models/link_classifier.joblib",
    abandon_after_s: float = 900.0,
    poll_jitter_frac: float = 0.0,
    seed: int = 1,
    admission_client: httpx.AsyncClient | None = None,
    delivery_client: httpx.AsyncClient | None = None,
) -> ClientOutcome:
    """Run one client to completion. Never raises except CancelledError.

    A load generator drives 20,000 of these. One raising client must not take
    down a gather of the rest, so every failure path returns an outcome instead.

    `poll_jitter_frac` defaults to 0.0: the client polls exactly as often as M1
    tells it to. Jitter would spread a 20,000-client thundering herd, but adding
    it by default would change the experiment being measured, which is not this
    module's call to make.
    """
    started = time.monotonic()
    rng = random.Random(f"{seed}:{client_id}")   # reproducible decisions
    session = _Session()
    gate = EstimateGate(min_rtt_samples=min_rtt_samples)

    own_admission = admission_client is None
    own_delivery = delivery_client is None
    adm = admission_client or _default_client_factory(admission_base)()
    dlv = delivery_client or _default_client_factory(delivery_base)()

    def elapsed() -> float:
        return time.monotonic() - started

    def finish(outcome: Outcome, **kw) -> ClientOutcome:
        return ClientOutcome(
            client_id=client_id,
            outcome=outcome,
            attempts=session.attempts,
            polls=session.polls,
            estimate_submitted=gate.submitted,
            never_classified=gate.never_qualified,
            wall_time_s=elapsed(),
            transfer_failures=session.transfer_failures,
            **kw,
        )

    try:
        # --- 1. join ------------------------------------------------------
        try:
            response = await adm.post(
                f"{admission_base.rstrip('/')}/queue/join",
                json={"client_id": client_id, "true_class": int(true_class)},
            )
        except httpx.HTTPError as exc:
            return finish(Outcome.ADMISSION_UNAVAILABLE, error=f"join: {exc!r}")
        if response.status_code >= 400:
            return finish(
                Outcome.ADMISSION_UNAVAILABLE,
                error=f"join returned {response.status_code}: {response.text[:200]}",
            )
        try:
            joined = response.json()
            ticket_id = str(joined["ticket_id"])
            poll_interval_ms = max(
                float(joined.get("poll_interval_ms", 2000)), MIN_POLL_INTERVAL_MS
            )
        except (ValueError, KeyError, TypeError) as exc:
            return finish(
                Outcome.ADMISSION_UNAVAILABLE,
                error=f"join body malformed: {exc!r} :: {response.text[:200]}",
            )

        # --- 2. probe -----------------------------------------------------
        session.observation.record_probe(
            await run_probe(dlv, delivery_base, probe_bytes)
        )

        status_url = f"{admission_base.rstrip('/')}/queue/status/{ticket_id}"

        # --- 3. wait, estimate, fetch ------------------------------------
        while True:
            if elapsed() > abandon_after_s:
                gate.mark_admitted_without_estimate()
                return finish(Outcome.ABANDONED, ticket_id=ticket_id,
                              error="abandon_after_s exceeded")

            response, rtt_ms, ok = await timed_poll(adm, status_url)
            session.polls += 1
            session.observation.record_request(ok, rtt_ms if ok else None)

            if response is None or response.status_code >= 500:
                session.consecutive_failures += 1
                if session.consecutive_failures >= MAX_CONSECUTIVE_ADMISSION_FAILURES:
                    return finish(
                        Outcome.ADMISSION_UNAVAILABLE,
                        ticket_id=ticket_id,
                        error="admission service unreachable or failing",
                    )
                await _sleep(poll_interval_ms, rng, poll_jitter_frac)
                continue

            # The counter is reset only after a status that actually parsed.
            # Resetting on any non-5xx would clobber it every iteration, and a
            # server returning 200 with a malformed body would loop until
            # abandon_after_s -- reporting a broken M1 as a client that gave up.
            try:
                status = TicketStatus.model_validate(response.json())
            except (ValidationError, ValueError) as exc:
                # Malformed is an infrastructure failure, not a client timeout.
                session.consecutive_failures += 1
                if session.consecutive_failures >= MAX_CONSECUTIVE_ADMISSION_FAILURES:
                    return finish(
                        Outcome.ADMISSION_UNAVAILABLE,
                        ticket_id=ticket_id,
                        error=f"status malformed: {exc!r} :: {response.text[:200]}",
                    )
                await _sleep(poll_interval_ms, rng, poll_jitter_frac)
                continue

            session.consecutive_failures = 0
            session.attempts = max(session.attempts, status.attempt)

            # Re-probe once if the wait has outlived the measurement.
            if session.probe_reruns == 0 and session.observation.probe_is_stale():
                session.probe_reruns = 1
                session.observation.record_probe(
                    await run_probe(dlv, delivery_base, probe_bytes)
                )

            # --- estimate: once, first attempt only ----------------------
            if gate.may_submit(session.observation, status.attempt):
                sample = session.observation.to_link_sample(ticket_id)
                estimate = classify(
                    sample,
                    model_path=model_path,
                    min_rtt_samples=min_rtt_samples,
                    confidence_threshold=confidence_threshold,
                )
                await submit_estimate(adm, admission_base, estimate)
                # Submitted is marked regardless of M1's reply: the one
                # submission has been spent either way, and a rejected estimate
                # leaves M1 on its MEDIUM default, which is where a fallback
                # would have landed anyway.
                gate.mark_submitted()

            if status.state in ("EXPIRED", "ABANDONED"):
                gate.mark_admitted_without_estimate()
                return finish(
                    Outcome.EXPIRED if status.state == "EXPIRED" else Outcome.ABANDONED,
                    ticket_id=ticket_id,
                )

            if status.state == "COMPLETED":
                return finish(Outcome.COMPLETED, ticket_id=ticket_id)

            if status.state == "ADMITTED" and status.admit_token:
                gate.mark_admitted_without_estimate()
                result = await _attempt_transfer(
                    dlv=dlv,
                    adm=adm,
                    delivery_base=delivery_base,
                    admission_base=admission_base,
                    ticket_id=ticket_id,
                    token=status.admit_token,
                    index_no=index_no,
                    expires_at=status.expires_at,
                    session=session,
                )
                if result is not None:
                    return finish(result[0], ticket_id=ticket_id, **result[1])
                # Transfer missed its window. Report it, keep waiting: M1
                # decides whether this ticket is re-queued (section 4.3 step 6).

            await _sleep(poll_interval_ms, rng, poll_jitter_frac)

    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - a client must never take down the run
        return finish(Outcome.ADMISSION_UNAVAILABLE, error=f"unexpected: {exc!r}")
    finally:
        if own_admission:
            await adm.aclose()
        if own_delivery:
            await dlv.aclose()


async def _attempt_transfer(
    *,
    dlv: httpx.AsyncClient,
    adm: httpx.AsyncClient,
    delivery_base: str,
    admission_base: str,
    ticket_id: str,
    token: str,
    index_no: str,
    expires_at: float | None,
    session: _Session,
) -> tuple[Outcome, dict] | None:
    """Fetch the page and report it. None means "missed the window, keep going".

    The transfer is the document plus every sub-resource it pulls, timed as one
    whole (see client/fetch.py). A page that is not finished is not delivered.
    """
    url = f"{delivery_base.rstrip('/')}/result?token={token}&index={index_no}"
    transfer = await fetch_page(dlv, url, expires_at=expires_at)

    reported = await _report_complete(
        adm,
        admission_base,
        ticket_id=ticket_id,
        ok=transfer.ok,
        n_bytes=transfer.bytes,
        duration_ms=transfer.duration_ms,
        variant=transfer.variant,
    )

    common = {
        "bytes": transfer.bytes,
        "duration_ms": transfer.duration_ms,
        "variant": transfer.variant,
        "requests": transfer.requests,
    }

    if transfer.status == 503:
        # Origin shedding load. Not a completion, and not the client's failure.
        return Outcome.ORIGIN_UNAVAILABLE, common

    if transfer.ok:
        if not reported:
            return Outcome.COMPLETED_UNREPORTED, common
        return Outcome.COMPLETED, common

    session.transfer_failures += 1
    return None  # missed the window; resume polling


async def _report_complete(
    adm: httpx.AsyncClient,
    admission_base: str,
    *,
    ticket_id: str,
    ok: bool,
    n_bytes: int,
    duration_ms: float,
    variant: str,
) -> bool:
    """POST /queue/complete. Returns whether the report landed.

    `bytes` and `duration_ms` cover the document AND its sub-resources, per the
    CONTRACT CHANGE recorded in CLAUDE.md section 4.2.
    """
    try:
        response = await adm.post(
            f"{admission_base.rstrip('/')}/queue/complete",
            json={
                "ticket_id": ticket_id,
                "ok": ok,
                "bytes": n_bytes,
                "duration_ms": duration_ms,
                "variant": variant,
            },
        )
    except httpx.HTTPError:
        return False
    return response.status_code < 400


async def _sleep(interval_ms: float, rng: random.Random, jitter_frac: float) -> None:
    """Wait one poll interval. Jitter is seeded, and off unless asked for."""
    delay = interval_ms / 1000.0
    if jitter_frac > 0:
        delay *= 1.0 + rng.uniform(-jitter_frac, jitter_frac)
    await asyncio.sleep(max(delay, 0.0))
