"""What the client does when M1 is broken, and how it reports the difference.

Devmith runs this against a half-built admission service. The property that
matters: **a broken M1 must never be countable as a client that failed to
finish.** The event log is written by M1 (section 3.8), so when M1 is down there
is no log at all -- the only place the distinction can live is the return value.
If infrastructure failure showed up as TIMED_OUT, the completion-rate gap would
move for reasons that have nothing to do with the system under test.

Also covers: one client must never take down a gather of 20,000, so run_client
returns an outcome for every failure path rather than raising.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from aaac.client.sdk import Outcome, run_client
from aaac.common.classes import AccessClass
from aaac.common.tokens import issue_token
from aaac.delivery.app import app as delivery_app
from aaac.delivery.variants import sample_record


@pytest.fixture(autouse=True)
def _stub_origin(monkeypatch):
    from aaac.delivery import app as delivery_module

    async def fake(index: str) -> dict:
        return sample_record()

    monkeypatch.setattr(delivery_module, "fetch_record", fake)


def _delivery() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=delivery_app), base_url="http://delivery:8001"
    )


async def _run(handler, **kw):
    adm = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://admission:8000"
    )
    dlv = _delivery()
    try:
        return await run_client(
            client_id="c-res",
            true_class=AccessClass.LOW,
            index_no="1",
            admission_base="",
            delivery_base="",
            min_rtt_samples=3,
            abandon_after_s=kw.pop("abandon_after_s", 20.0),
            admission_client=adm,
            delivery_client=dlv,
            **kw,
        )
    finally:
        await adm.aclose()
        await dlv.aclose()


# --- a broken M1 is not a client failure ----------------------------------


async def test_admission_refusing_connections_is_not_a_timeout():
    def handler(request):
        raise httpx.ConnectError("connection refused")

    out = await _run(handler)
    assert out.outcome is Outcome.ADMISSION_UNAVAILABLE
    assert out.outcome is not Outcome.TIMED_OUT
    assert out.outcome is not Outcome.ABANDONED
    # Full string, not a fragment: a resilience test is the one we will trust
    # when something breaks mid-evaluation, so it must pin the whole message.
    assert out.error.startswith("join: ")
    assert "ConnectError" in out.error


async def test_join_returning_500_is_not_a_timeout():
    def handler(request):
        return httpx.Response(500, text="internal error")

    out = await _run(handler)
    assert out.outcome is Outcome.ADMISSION_UNAVAILABLE
    assert out.error == "join returned 500: internal error"


async def test_malformed_join_body_is_reported_not_crashed():
    def handler(request):
        return httpx.Response(200, text="<html>not json</html>")

    out = await _run(handler)
    assert out.outcome is Outcome.ADMISSION_UNAVAILABLE
    assert "malformed" in out.error


async def test_join_missing_ticket_id_is_reported():
    def handler(request):
        return httpx.Response(200, json={"position": 1})

    out = await _run(handler)
    assert out.outcome is Outcome.ADMISSION_UNAVAILABLE


async def test_status_failing_repeatedly_gives_up_as_unavailable():
    """Bounded retries: hammering a dead server produces noise, not data."""
    calls = {"n": 0}

    def handler(request):
        if request.url.path == "/queue/join":
            return httpx.Response(
                200,
                json={"ticket_id": "t", "join_seq": 1, "position": 1,
                      "eta_s": 1.0, "poll_interval_ms": 1},
            )
        calls["n"] += 1
        return httpx.Response(503, text="down")

    out = await _run(handler)
    assert out.outcome is Outcome.ADMISSION_UNAVAILABLE
    assert calls["n"] <= 8, f"kept retrying a dead server {calls['n']} times"


async def test_malformed_status_body_is_infrastructure_not_timeout():
    def handler(request):
        if request.url.path == "/queue/join":
            return httpx.Response(
                200,
                json={"ticket_id": "t", "join_seq": 1, "position": 1,
                      "eta_s": 1.0, "poll_interval_ms": 1},
            )
        return httpx.Response(200, json={"state": "WOBBLE"})   # not a valid state

    out = await _run(handler)
    assert out.outcome is Outcome.ADMISSION_UNAVAILABLE
    assert "malformed" in out.error


async def test_transient_status_failure_recovers():
    """A blip is not an outage. Two failures then success must still complete."""
    state = {"polls": 0}

    def handler(request):
        p = request.url.path
        if p == "/queue/join":
            return httpx.Response(
                200,
                json={"ticket_id": "t", "join_seq": 1, "position": 1,
                      "eta_s": 1.0, "poll_interval_ms": 1},
            )
        if p == "/queue/complete":
            return httpx.Response(200, json={"state": "COMPLETED"})
        if p == "/queue/estimate":
            return httpx.Response(200, json={"accepted": True, "access_class": 2})
        state["polls"] += 1
        if state["polls"] in (2, 3):
            return httpx.Response(500, text="blip")
        if state["polls"] < 6:
            return httpx.Response(
                200,
                json={"ticket_id": "t", "state": "WAITING", "position": 1,
                      "attempt": 1, "access_class": 1, "window_s": None,
                      "admit_token": None, "expires_at": None},
            )
        return httpx.Response(
            200,
            json={"ticket_id": "t", "state": "ADMITTED", "position": 0,
                  "attempt": 1, "access_class": 2, "window_s": 30.0,
                  "admit_token": issue_token("t", AccessClass.LOW, 1, 30.0),
                  "expires_at": time.time() + 30.0},
        )

    out = await _run(handler)
    assert out.outcome is Outcome.COMPLETED
    assert out.variant == "essential"


# --- the completed-but-unreported case ------------------------------------


async def test_fetch_succeeds_but_complete_fails():
    """The student got their result; the report did not land. Distinct outcome.

    Counting this as a failure would understate completion; counting it as a
    plain success would hide that M1's log is missing the record.
    """
    def handler(request):
        p = request.url.path
        if p == "/queue/join":
            return httpx.Response(
                200,
                json={"ticket_id": "t", "join_seq": 1, "position": 0,
                      "eta_s": 0.0, "poll_interval_ms": 1},
            )
        if p == "/queue/estimate":
            return httpx.Response(200, json={"accepted": True, "access_class": 2})
        if p == "/queue/complete":
            return httpx.Response(500, text="log write failed")
        return httpx.Response(
            200,
            json={"ticket_id": "t", "state": "ADMITTED", "position": 0,
                  "attempt": 1, "access_class": 2, "window_s": 30.0,
                  "admit_token": issue_token("t", AccessClass.LOW, 1, 30.0),
                  "expires_at": time.time() + 30.0},
        )

    out = await _run(handler)
    assert out.outcome is Outcome.COMPLETED_UNREPORTED
    assert out.bytes > 0, "the transfer really happened"


# --- window expiry --------------------------------------------------------


async def test_missing_the_window_is_a_timeout_not_an_infrastructure_error():
    """expires_at already past: the transfer cannot finish in time."""
    def handler(request):
        p = request.url.path
        if p == "/queue/join":
            return httpx.Response(
                200,
                json={"ticket_id": "t", "join_seq": 1, "position": 0,
                      "eta_s": 0.0, "poll_interval_ms": 1},
            )
        if p == "/queue/estimate":
            return httpx.Response(200, json={"accepted": True, "access_class": 0})
        if p == "/queue/complete":
            return httpx.Response(200, json={"state": "TIMEOUT"})
        return httpx.Response(
            200,
            json={"ticket_id": "t", "state": "ADMITTED", "position": 0,
                  "attempt": 1, "access_class": 0, "window_s": 0.0,
                  "admit_token": issue_token("t", AccessClass.HIGH, 1, 300.0),
                  "expires_at": time.time() - 5.0},
        )

    out = await _run(handler, abandon_after_s=1.0)
    # It keeps polling after a missed window (section 4.3 step 6), so it ends
    # by abandoning -- but never as an infrastructure failure.
    assert out.outcome is not Outcome.ADMISSION_UNAVAILABLE
    assert out.outcome in (Outcome.ABANDONED, Outcome.TIMED_OUT)
    assert out.transfer_failures >= 1


async def test_abandons_after_abandon_after_s():
    def handler(request):
        p = request.url.path
        if p == "/queue/join":
            return httpx.Response(
                200,
                json={"ticket_id": "t", "join_seq": 1, "position": 9,
                      "eta_s": 99.0, "poll_interval_ms": 1},
            )
        if p == "/queue/estimate":
            return httpx.Response(200, json={"accepted": True, "access_class": 1})
        return httpx.Response(
            200,
            json={"ticket_id": "t", "state": "WAITING", "position": 9,
                  "attempt": 1, "access_class": 1, "window_s": None,
                  "admit_token": None, "expires_at": None},
        )

    out = await _run(handler, abandon_after_s=0.4)
    assert out.outcome is Outcome.ABANDONED
    assert out.polls > 0


# --- never raises ---------------------------------------------------------


async def test_one_bad_client_does_not_break_a_gather():
    """20,000 of these run concurrently; one must not poison the rest."""
    def bad(request):
        raise httpx.ConnectError("refused")

    def good(request):
        p = request.url.path
        if p == "/queue/join":
            return httpx.Response(
                200,
                json={"ticket_id": "t", "join_seq": 1, "position": 0,
                      "eta_s": 0.0, "poll_interval_ms": 1},
            )
        if p == "/queue/estimate":
            return httpx.Response(200, json={"accepted": True, "access_class": 2})
        if p == "/queue/complete":
            return httpx.Response(200, json={"state": "COMPLETED"})
        return httpx.Response(
            200,
            json={"ticket_id": "t", "state": "ADMITTED", "position": 0,
                  "attempt": 1, "access_class": 2, "window_s": 30.0,
                  "admit_token": issue_token("t", AccessClass.LOW, 1, 30.0),
                  "expires_at": time.time() + 30.0},
        )

    results = await asyncio.gather(
        _run(bad), _run(good), _run(bad), _run(good), return_exceptions=True
    )
    assert not any(isinstance(r, BaseException) for r in results)
    assert [r.outcome for r in results] == [
        Outcome.ADMISSION_UNAVAILABLE,
        Outcome.COMPLETED,
        Outcome.ADMISSION_UNAVAILABLE,
        Outcome.COMPLETED,
    ]


async def test_cancellation_propagates():
    """Run teardown must be able to cancel clients cleanly."""
    def handler(request):
        if request.url.path == "/queue/join":
            return httpx.Response(
                200,
                json={"ticket_id": "t", "join_seq": 1, "position": 5,
                      "eta_s": 50.0, "poll_interval_ms": 50},
            )
        return httpx.Response(
            200,
            json={"ticket_id": "t", "state": "WAITING", "position": 5,
                  "attempt": 1, "access_class": 1, "window_s": None,
                  "admit_token": None, "expires_at": None},
        )

    task = asyncio.create_task(_run(handler, abandon_after_s=60.0))
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
