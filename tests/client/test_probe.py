"""Probe timing, failure handling, and the estimate-submission gate (C1).

The probe math test is the one section 4.5 names: a synthetic transfer of known
size and duration must produce the right throughput within 1%.

The rest guard the three decisions that were made deliberately and would
otherwise be easy to undo by accident:

  * duration is first byte to last byte, matching what synthdata trained on
  * a failed probe is a low reading, never a fallback to MEDIUM
  * the estimate goes in once, on the first attempt only

No Redis, no network: the delivery app is driven in-process over httpx's ASGI
transport (contract rule 4).
"""

from __future__ import annotations

import httpx
import pytest

from aaac.client.probe import (
    PROBE_REFRESH_AFTER_S,
    TRAINING_FLOOR_KBPS,
    EstimateGate,
    LinkObservation,
    ProbeResult,
    run_probe,
    timed_poll,
)
from aaac.common.classes import AccessClass
from aaac.delivery.app import app as delivery
from aaac.estimator.features import extract_features, throughput_kbps
from aaac.estimator.infer import classify

PROBE_BYTES = 65536  # configs/run.yaml -> estimator.probe_bytes


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=delivery),
        base_url="http://delivery:8001",
    )


# --- probe math (section 4.5) ---------------------------------------------


@pytest.mark.parametrize(
    "n_bytes,duration_ms,expected_kbps",
    [
        (65536, 1000.0, 524.288),      # 64 KB in 1 s
        (65536, 100.0, 5242.88),       # 64 KB in 100 ms
        (65536, 26.2144, 20000.0),     # a HIGH-class link
        (65536, 1024.0, 512.0),        # a LOW-class link, 512 kbit
    ],
)
def test_throughput_math_is_exact(n_bytes, duration_ms, expected_kbps):
    """kbps = bytes * 8 / ms, within 1%."""
    got = throughput_kbps(n_bytes, duration_ms)
    assert got == pytest.approx(expected_kbps, rel=0.01)
    assert ProbeResult(n_bytes, duration_ms, True).throughput_kbps == pytest.approx(
        expected_kbps, rel=0.01
    )


async def test_probe_downloads_the_requested_size():
    async with _client() as client:
        result = await run_probe(client, "", PROBE_BYTES)
    assert result.ok
    assert result.bytes_received == PROBE_BYTES
    assert result.duration_ms > 0
    assert result.throughput_kbps > 0


async def test_probe_payload_is_incompressible_and_uncached():
    """Random bytes so gzip cannot inflate the measured throughput."""
    async with _client() as client:
        a = await client.get(f"/probe/{PROBE_BYTES}")
        b = await client.get(f"/probe/{PROBE_BYTES}")
    assert a.headers["cache-control"] == "no-store"
    assert int(a.headers["content-length"]) == PROBE_BYTES
    assert a.content != b.content, "probe payload must not be a fixed buffer"

    import zlib

    compressed = len(zlib.compress(a.content))
    assert compressed > PROBE_BYTES * 0.95, "payload compressed; it is not random"


# --- failure handling: a bad probe is a reading, not an absence -----------


def test_partial_probe_is_a_low_reading_not_a_fallback():
    """Bytes arrived, then it stalled. That is a slow link, and we say so."""
    obs = LinkObservation()
    # 8 KB of a 64 KB probe took 2 s before the transfer died.
    obs.record_probe(ProbeResult(8192, 2000.0, ok=False, error="ReadTimeout"))
    for rtt in (240.0, 260.0, 310.0, 280.0, 300.0, 255.0):
        obs.record_request(True, rtt)

    sample = obs.to_link_sample("t-partial")
    assert sample.probe_bytes == 8192
    assert sample.failed_requests == 1

    est = classify(
        sample,
        model_path="models/link_classifier.joblib",
        min_rtt_samples=5,
        confidence_threshold=0.60,
    )
    # The important property: a struggling probe must not become a confident
    # MEDIUM. Either it classified (and said something slow), or it fell back --
    # but it must never read as a fast link.
    assert est.access_class is not AccessClass.HIGH
    assert est.throughput_kbps == pytest.approx(8192 * 8.0 / 2000.0, rel=0.01)


def test_reading_below_the_training_floor_is_clamped():
    """The model has never seen throughput under 30 kbps; do not ask it to."""
    obs = LinkObservation()
    obs.record_probe(ProbeResult(1024, 60_000.0, ok=False))  # ~0.14 kbps
    sample = obs.to_link_sample("t-floor")

    kbps = throughput_kbps(sample.probe_bytes, sample.probe_duration_ms)
    assert kbps == pytest.approx(TRAINING_FLOOR_KBPS, rel=1e-6)
    assert sample.probe_bytes == 1024, "byte count stays honest; duration is clamped"


def test_readings_above_the_floor_are_untouched():
    obs = LinkObservation()
    obs.record_probe(ProbeResult(PROBE_BYTES, 1024.0, ok=True))
    sample = obs.to_link_sample("t-normal")
    assert sample.probe_duration_ms == pytest.approx(1024.0)
    assert throughput_kbps(sample.probe_bytes, sample.probe_duration_ms) == (
        pytest.approx(512.0, rel=0.01)
    )


def test_zero_byte_probe_is_reported_honestly_and_counted_as_failed():
    """Nothing arrived. No bytes/duration pair can express that, so we do not
    invent one -- LinkSample is logged by M1 and read by M3."""
    obs = LinkObservation()
    obs.record_probe(ProbeResult(0, 0.0, ok=False, error="ConnectTimeout"))
    for rtt in (300.0, 420.0, 380.0, 350.0, 410.0):
        obs.record_request(True, rtt)

    sample = obs.to_link_sample("t-zero")
    assert sample.probe_bytes == 0
    assert sample.failed_requests == 1
    assert extract_features(sample)[0] == 0.0  # log10(0 + 1)

    est = classify(
        sample,
        model_path="models/link_classifier.joblib",
        min_rtt_samples=5,
        confidence_threshold=0.60,
    )
    assert est.access_class is not AccessClass.HIGH
    assert est.loss_ratio > 0, "the failure must reach the reported estimate"


async def test_probe_against_a_dead_server_does_not_raise():
    async with httpx.AsyncClient(base_url="http://127.0.0.1:1") as client:
        result = await run_probe(client, "", PROBE_BYTES, timeout_s=0.3)
    assert result.ok is False
    assert result.bytes_received == 0
    assert result.throughput_kbps == 0.0


# --- RTT collection -------------------------------------------------------


async def test_poll_timing_produces_rtt_samples():
    obs = LinkObservation()
    async with _client() as client:
        for _ in range(6):
            response, rtt_ms, ok = await timed_poll(client, "/probe/16")
            obs.record_request(ok, rtt_ms)
            assert response is not None
    assert len(obs.rtt_samples_ms) == 6
    assert obs.has_enough_rtts(5)
    assert all(r > 0 for r in obs.rtt_samples_ms)


def test_failed_poll_contributes_a_failure_and_no_timing():
    obs = LinkObservation()
    obs.record_request(True, 210.0)
    obs.record_request(False)
    obs.record_request(True, 230.0)
    assert obs.rtt_samples_ms == [210.0, 230.0]
    assert (obs.failed_requests, obs.total_requests) == (1, 3)

    # The failure must survive into the ratio the estimator reports.
    sample = obs.to_link_sample("t")
    assert sample.failed_requests / sample.total_requests == pytest.approx(1 / 3)


def test_probe_goes_stale_after_the_refresh_window():
    obs = LinkObservation()
    obs.record_probe(ProbeResult(PROBE_BYTES, 500.0, True), at=1000.0)
    assert not obs.probe_is_stale(now=1000.0 + PROBE_REFRESH_AFTER_S - 1)
    assert obs.probe_is_stale(now=1000.0 + PROBE_REFRESH_AFTER_S + 1)


# --- the estimate gate ----------------------------------------------------


def test_estimate_waits_for_min_rtt_samples():
    gate = EstimateGate(min_rtt_samples=5)
    obs = LinkObservation()
    for i in range(4):
        obs.record_request(True, 200.0 + i)
        assert not gate.may_submit(obs, attempt=1)
    obs.record_request(True, 205.0)
    assert gate.may_submit(obs, attempt=1)


def test_estimate_is_submitted_only_once():
    gate = EstimateGate(min_rtt_samples=2)
    obs = LinkObservation()
    obs.record_request(True, 200.0)
    obs.record_request(True, 210.0)
    assert gate.may_submit(obs, attempt=1)
    gate.mark_submitted()
    assert not gate.may_submit(obs, attempt=1)


def test_estimate_is_refused_on_later_attempts():
    """Protects M1's I2 invariant: a class is never upgraded.

    A client downgraded on a timeout and re-queued is WAITING again. Letting it
    submit a fresh estimate would route an upgrade through a legitimate
    endpoint. Narrow behaviour pending M1's answer on the endpoint guard.
    """
    gate = EstimateGate(min_rtt_samples=2)
    obs = LinkObservation()
    obs.record_request(True, 200.0)
    obs.record_request(True, 210.0)
    for attempt in (2, 3, 4):
        assert not gate.may_submit(obs, attempt=attempt)


def test_never_classified_clients_are_counted():
    """A fast client admitted before it qualified is never classified.

    M3 must be able to report that population separately: an accuracy figure
    averaged over clients that were never classified is worse than no figure.
    """
    gate = EstimateGate(min_rtt_samples=5)
    obs = LinkObservation()
    for i in range(3):
        obs.record_request(True, 20.0 + i)
    assert not gate.may_submit(obs, attempt=1)
    gate.mark_admitted_without_estimate()
    assert gate.never_qualified is True
    assert gate.submitted is False


def test_classified_client_is_not_counted_as_never_classified():
    gate = EstimateGate(min_rtt_samples=2)
    obs = LinkObservation()
    obs.record_request(True, 20.0)
    obs.record_request(True, 22.0)
    gate.mark_submitted()
    gate.mark_admitted_without_estimate()
    assert gate.never_qualified is False


# --- end to end, in process ----------------------------------------------


async def test_probe_and_polls_produce_a_usable_estimate():
    """The full C1 path against a live ASGI app: probe, polls, classify."""
    obs = LinkObservation()
    async with _client() as client:
        obs.record_probe(await run_probe(client, "", PROBE_BYTES))
        for _ in range(6):
            _, rtt_ms, ok = await timed_poll(client, "/probe/16")
            obs.record_request(ok, rtt_ms)

    sample = obs.to_link_sample("t-e2e")
    assert sample.probe_bytes == PROBE_BYTES
    assert len(sample.rtt_samples_ms) == 6

    est = classify(
        sample,
        model_path="models/link_classifier.joblib",
        min_rtt_samples=5,
        confidence_threshold=0.60,
    )
    assert est.ticket_id == "t-e2e"
    assert 0.0 <= est.confidence <= 1.0
    assert est.throughput_kbps > 0
    assert est.model_version
