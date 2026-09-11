"""The verification gate's own logic, exercised without Docker.

Parsing and pass/fail logic are tested against captured tool output. What cannot
be tested here — that `tc` actually shapes a real interface — is exactly what
``make verify-testbed`` exists to measure on the running testbed.
"""

from __future__ import annotations

import math

import pytest

from aaac.evaluation.access_class import AccessClass
from aaac.evaluation.testbed.profiles import PROFILES
from aaac.evaluation.testbed.verify import (
    Check,
    Measurement,
    TestbedError,
    _iperf_kbps,
    _parse_ping,
    build_checks,
    render,
    wilson_interval,
)

PING_OUTPUT = """PING origin (172.19.0.3) 56(84) bytes of data.

--- origin ping statistics ---
2000 packets transmitted, 1940 received, 3% packet loss, time 101234ms
rtt min/avg/max/mdev = 248.012/250.431/402.115/28.774 ms
"""

PING_TOTAL_LOSS = """PING origin (172.19.0.3) 56(84) bytes of data.

--- origin ping statistics ---
100 packets transmitted, 0 received, 100% packet loss, time 9999ms
"""

IPERF_OUTPUT = '{"end": {"sum_received": {"bits_per_second": 512345.0}}}'


def test_parses_ping_statistics() -> None:
    rtt, mdev, sent, received = _parse_ping(PING_OUTPUT)
    assert rtt == pytest.approx(250.431)
    assert mdev == pytest.approx(28.774)
    assert (sent, received) == (2000, 1940)


def test_total_packet_loss_is_reported_not_treated_as_a_parse_error() -> None:
    rtt, _mdev, sent, received = _parse_ping(PING_TOTAL_LOSS)
    assert math.isinf(rtt)
    assert (sent, received) == (100, 0)


def test_unparseable_ping_output_raises() -> None:
    with pytest.raises(TestbedError):
        _parse_ping("something else entirely")


def test_parses_iperf_json() -> None:
    assert _iperf_kbps(IPERF_OUTPUT) == pytest.approx(512.345)


def test_iperf_error_is_surfaced() -> None:
    with pytest.raises(TestbedError, match="unable to connect"):
        _iperf_kbps('{"error": "unable to connect to server"}')


@pytest.mark.parametrize(
    ("k", "n"), [(0, 0), (0, 100), (3, 100), (100, 100)]
)
def test_wilson_interval_stays_inside_zero_one(k: int, n: int) -> None:
    low, high = wilson_interval(k, n)
    assert 0.0 <= low <= high <= 1.0


def test_wilson_interval_brackets_the_point_estimate() -> None:
    low, high = wilson_interval(60, 2000)
    assert low < 0.03 < high


def test_check_passes_within_tolerance() -> None:
    check = Check("rate", 512.0, 500.0, "kbit/s", 0.10, gating=True)
    assert check.passed


def test_check_fails_outside_tolerance() -> None:
    check = Check("rate", 512.0, 270.0, "kbit/s", 0.10, gating=True)
    assert not check.passed


def _measurement_for(profile_name: str, **kwargs: float) -> Measurement:
    profile = PROFILES[AccessClass[profile_name]]
    m = Measurement(profile=profile.name, service="client-x", direction="ingress")
    m.udp_down_kbps = kwargs.get("udp", profile.rate_kbps)
    m.tcp_down_kbps = kwargs.get("tcp", profile.rate_kbps * 0.5)
    m.rtt_mean_ms = kwargs.get("rtt", profile.delay_ms)
    m.loss_pct = kwargs.get("loss", profile.loss_pct)
    m.ping_sent = int(kwargs.get("sent", 2000))
    lost = int(round(m.ping_sent * m.loss_pct / 100.0))
    low, high = wilson_interval(lost, m.ping_sent)
    m.loss_ci_low_pct, m.loss_ci_high_pct = 100.0 * low, 100.0 * high
    m.checks = build_checks(profile, m)
    return m


def test_a_correctly_shaped_link_passes() -> None:
    assert _measurement_for("LOW").passed


def test_a_link_shaped_to_the_wrong_rate_fails() -> None:
    assert not _measurement_for("LOW", udp=270.0).passed


def test_a_link_with_the_wrong_rtt_fails() -> None:
    assert not _measurement_for("LOW", rtt=15.0).passed


def test_tcp_goodput_below_nominal_does_not_fail_the_gate() -> None:
    # A LOW link delivering ~47% of nominal to TCP is the expected physics, not
    # a shaping fault. Gating on it would fail every correct run.
    m = _measurement_for("LOW", tcp=240.0)
    assert m.passed
    tcp_check = next(c for c in m.checks if "TCP" in c.name)
    assert not tcp_check.gating


def test_loss_is_not_gated_when_the_sample_is_too_small_to_be_informative() -> None:
    # 0.01% loss cannot be verified to 10% with any feasible packet count.
    m = _measurement_for("HIGH", sent=2000)
    loss_check = next(c for c in m.checks if c.name == "loss")
    assert not loss_check.gating
    assert "informational" in loss_check.note


def test_loss_is_gated_when_the_sample_supports_it() -> None:
    m = _measurement_for("LOW", sent=50_000)
    loss_check = next(c for c in m.checks if c.name == "loss")
    assert loss_check.gating
    assert loss_check.passed


def test_render_marks_failures_and_flags_unshaped_downstream() -> None:
    failing = _measurement_for("LOW", udp=100.0)
    failing.direction = "egress"
    output = render([failing])
    assert "FAIL" in output
    assert "UNSHAPED" in output
