"""Link profiles, and the compose file that must agree with them.

CLAUDE.md §4.2 puts the profile table verbatim in the report. If ``profiles.py``
and ``docker-compose.yml`` ever disagree, the report describes a testbed that was
never actually run — which is why the drift check below is a test and not a
comment.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest
import yaml

from aaac.evaluation.access_class import AccessClass
from aaac.evaluation.testbed.profiles import PROFILES, VERIFY_TOLERANCE, profile_for
from aaac.evaluation.testbed.verify import SERVICE_FOR, mathis_kbps

REPO = Path(__file__).resolve().parents[1]
COMPOSE = REPO / "docker-compose.yml"


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def test_the_three_contract_classes_all_have_a_profile() -> None:
    assert set(PROFILES) == {AccessClass.HIGH, AccessClass.MEDIUM, AccessClass.LOW}


def test_profile_values_match_section_4_2() -> None:
    high = PROFILES[AccessClass.HIGH]
    medium = PROFILES[AccessClass.MEDIUM]
    low = PROFILES[AccessClass.LOW]
    assert (high.rate, high.delay_ms, high.jitter_ms, high.loss_pct) == ("50mbit", 15.0, 3.0, 0.01)
    assert (medium.rate, medium.delay_ms, medium.jitter_ms, medium.loss_pct) == (
        "5mbit", 60.0, 20.0, 0.5,
    )
    assert (low.rate, low.delay_ms, low.jitter_ms, low.loss_pct) == ("512kbit", 250.0, 120.0, 3.0)


def test_profiles_are_ordered_worst_to_best_by_class() -> None:
    rates = [PROFILES[c].rate_kbps for c in (AccessClass.HIGH, AccessClass.MEDIUM, AccessClass.LOW)]
    delays = [PROFILES[c].delay_ms for c in (AccessClass.HIGH, AccessClass.MEDIUM, AccessClass.LOW)]
    assert rates == sorted(rates, reverse=True)
    assert delays == sorted(delays)


def test_lookup_by_name_and_by_class() -> None:
    assert profile_for("low") is PROFILES[AccessClass.LOW]
    assert profile_for(AccessClass.HIGH) is PROFILES[AccessClass.HIGH]
    with pytest.raises(KeyError):
        profile_for("SATELLITE")


def test_burst_is_large_enough_for_tbf_to_reach_the_configured_rate() -> None:
    # A token bucket cannot reach `rate` if burst < rate / HZ. With HZ=1000 the
    # floor is rate_kbps/1000 kbit. The §4.2 snippet's literal `32kbit` fails
    # this for the HIGH profile, which is why burst is per-class.
    for profile in PROFILES.values():
        burst_kbit = float(profile.burst.removesuffix("kbit"))
        assert burst_kbit >= profile.rate_kbps / 1000.0, profile.name


def test_compose_defines_a_client_container_per_class(compose: dict) -> None:
    for service in SERVICE_FOR.values():
        assert service in compose["services"], service


def test_compose_netem_values_match_profiles(compose: dict) -> None:
    for access_class, service in SERVICE_FOR.items():
        profile = PROFILES[access_class]
        env = compose["services"][service]["environment"]
        assert env["AAAC_NETEM_PROFILE"] == profile.name
        assert env["AAAC_NETEM_RATE"] == profile.rate
        assert env["AAAC_NETEM_DELAY"] == profile.delay
        assert env["AAAC_NETEM_JITTER"] == profile.jitter
        assert env["AAAC_NETEM_LOSS"] == profile.loss
        assert env["AAAC_NETEM_BURST"] == profile.burst


def test_client_containers_have_net_admin(compose: dict) -> None:
    # netem.sh cannot apply a qdisc without it, and would fail the run.
    for service in SERVICE_FOR.values():
        assert "NET_ADMIN" in compose["services"][service]["cap_add"], service


def test_clients_shape_the_downstream_path_by_default(compose: dict) -> None:
    # The ~450 KB page travels downstream. Shaping only egress would leave the
    # LOW class effectively unthrottled and the experiment would measure nothing.
    for service in SERVICE_FOR.values():
        assert compose["services"][service]["environment"]["AAAC_NETEM_DIRECTION"] == "ingress"


def test_origin_is_published_on_the_contracted_port(compose: dict) -> None:
    assert any("8002" in str(p) for p in compose["services"]["origin"]["ports"])


def test_verify_tolerance_is_the_ten_percent_from_section_4_2() -> None:
    assert VERIFY_TOLERANCE == 0.10


def test_mathis_ceiling_is_below_nominal_rate_for_the_low_profile() -> None:
    # This is the reason the rate gate uses UDP: a correctly shaped LOW link
    # cannot deliver its nominal 512 kbit/s to a TCP transfer.
    low = PROFILES[AccessClass.LOW]
    assert mathis_kbps(low) < low.rate_kbps


def test_mathis_ceiling_is_not_binding_for_the_high_profile() -> None:
    high = PROFILES[AccessClass.HIGH]
    assert mathis_kbps(high) > high.rate_kbps


def test_mathis_is_unbounded_without_loss() -> None:
    lossless = PROFILES[AccessClass.HIGH].__class__(
        name="X", access_class=AccessClass.HIGH, rate="1mbit", rate_kbps=1000.0,
        delay_ms=10.0, jitter_ms=0.0, loss_pct=0.0, burst="32kbit", represents="ideal",
    )
    assert math.isinf(mathis_kbps(lossless))
