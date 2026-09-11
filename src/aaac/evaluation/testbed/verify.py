"""Testbed verification gate (CLAUDE.md §4.2, §7.3).

    make verify-testbed

Measures achieved rate, RTT and loss from *inside* each shaped client container
and asserts each is within tolerance of the configured profile. A ``tc`` command
that silently no-ops looks identical to one that worked, so this runs at the
start of every experiment, not once at the start of the project.

TWO MEASUREMENTS OF RATE, AND WHY THAT IS NOT HEDGING
-----------------------------------------------------
§4.2 says a timed probe or iperf3 must land within 10% of the configured rate.
Taken with a TCP measurement that gate is **unachievable for the LOW profile**,
and not because the shaping failed. TCP throughput over a lossy, high-latency
path is bounded by roughly ``MSS / (RTT * sqrt(loss))``; at 250 ms and 3% that is
about 270 kbit/s, well under the configured 512 kbit/s. A TCP test would report
~53% of target - a 47% shortfall - and the gate would fail on a
correctly shaped link.

So the two things are measured separately:

* **UDP** downstream throughput gates the shaper. It bypasses congestion control
  and measures what ``tbf`` actually admits, which is the thing being verified.
* **TCP** downstream goodput is reported alongside as *informational*, with the
  Mathis prediction next to it.

The TCP number is not a footnote. That the LOW link delivers roughly half its
nominal capacity to a real transfer is a large part of *why* a 450 KB page does
not fit in a short admission window, and it belongs in the report.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from aaac.evaluation.access_class import AccessClass
from aaac.evaluation.testbed.profiles import PROFILES, VERIFY_TOLERANCE, LinkProfile

#: compose service name per class.
SERVICE_FOR: dict[AccessClass, str] = {
    AccessClass.HIGH: "client-high",
    AccessClass.MEDIUM: "client-medium",
    AccessClass.LOW: "client-low",
}

IPERF_HOST = "iperf"
PING_HOST = "origin"

_RTT_RE = re.compile(r"=\s*([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)\s*ms")
_LOSS_RE = re.compile(r"(\d+)\s+packets transmitted,\s*(\d+)\s+received")


class TestbedError(RuntimeError):
    """The testbed could not be measured at all — distinct from failing the gate."""

    __test__ = False  # not a pytest test class despite the name


# --------------------------------------------------------------------------
# measurement
# --------------------------------------------------------------------------


@dataclass
class Check:
    name: str
    configured: float
    measured: float
    unit: str
    tolerance: float
    gating: bool
    note: str = ""

    @property
    def rel_error(self) -> float:
        if self.configured == 0:
            return 0.0 if self.measured == 0 else math.inf
        return abs(self.measured - self.configured) / self.configured

    @property
    def passed(self) -> bool:
        return self.rel_error <= self.tolerance


@dataclass
class Measurement:
    profile: str
    service: str
    direction: str
    udp_down_kbps: float = 0.0
    tcp_down_kbps: float = 0.0
    tcp_up_kbps: float = 0.0
    rtt_mean_ms: float = 0.0
    rtt_jitter_ms: float = 0.0
    loss_pct: float = 0.0
    loss_ci_low_pct: float = 0.0
    loss_ci_high_pct: float = 0.0
    ping_sent: int = 0
    mathis_kbps: float = 0.0
    checks: list[Check] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks if c.gating)


def _compose(*args: str, timeout: float = 180.0) -> subprocess.CompletedProcess[str]:
    cmd = ["docker", "compose", *args]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:  # pragma: no cover - environment dependent
        raise TestbedError("`docker` is not on PATH") from exc
    except subprocess.TimeoutExpired as exc:  # pragma: no cover
        raise TestbedError(f"timed out running: {' '.join(cmd)}") from exc


def _exec(service: str, argv: list[str], timeout: float = 180.0) -> str:
    result = _compose("exec", "-T", service, *argv, timeout=timeout)
    if result.returncode != 0:
        raise TestbedError(
            f"`docker compose exec {service} {' '.join(argv)}` failed "
            f"(exit {result.returncode}):\n{result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout


def wilson_interval(successes: int, trials: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a binomial proportion."""
    if trials == 0:
        return 0.0, 1.0
    p = successes / trials
    denom = 1.0 + z * z / trials
    centre = (p + z * z / (2 * trials)) / denom
    half = z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def mathis_kbps(profile: LinkProfile, mss_bytes: int = 1460) -> float:
    """Rough TCP throughput ceiling: ``MSS / (RTT * sqrt(p))``."""
    loss = profile.loss_pct / 100.0
    if loss <= 0:
        return math.inf
    rtt_s = profile.delay_ms / 1000.0
    if rtt_s <= 0:
        return math.inf
    return (mss_bytes * 8.0) / (rtt_s * math.sqrt(loss)) / 1000.0


def _iperf_kbps(raw: str) -> float:
    payload = json.loads(raw)
    if "error" in payload:
        raise TestbedError(f"iperf3 reported: {payload['error']}")
    received = payload["end"]["sum_received"]["bits_per_second"]
    return float(received) / 1000.0


def _parse_ping(raw: str) -> tuple[float, float, int, int]:
    """Return (rtt_mean_ms, rtt_mdev_ms, sent, received)."""
    loss_match = _LOSS_RE.search(raw)
    rtt_match = _RTT_RE.search(raw)
    if loss_match is None:
        raise TestbedError(f"could not parse ping statistics from:\n{raw}")
    sent, received = int(loss_match.group(1)), int(loss_match.group(2))
    if rtt_match is None:
        # Every packet was lost — real information, not a parse failure.
        return math.inf, 0.0, sent, received
    return float(rtt_match.group(2)), float(rtt_match.group(4)), sent, received


def measure(profile: LinkProfile, *, duration: int, ping_count: int) -> Measurement:
    service = SERVICE_FOR[profile.access_class]
    direction = _exec(service, ["sh", "-c", "echo ${AAAC_NETEM_DIRECTION:-ingress}"]).strip()
    m = Measurement(profile=profile.name, service=service, direction=direction)

    # RTT and loss. Interval kept short so a 3%-loss profile still accumulates
    # enough packets for a usable confidence interval.
    ping_out = _exec(
        service,
        ["ping", "-c", str(ping_count), "-i", "0.05", "-W", "2", "-q", PING_HOST],
        timeout=ping_count * 0.1 + 120.0,
    )
    m.rtt_mean_ms, m.rtt_jitter_ms, sent, received = _parse_ping(ping_out)
    m.ping_sent = sent
    lost = max(0, sent - received)
    m.loss_pct = 100.0 * lost / sent if sent else 0.0
    low, high = wilson_interval(lost, sent)
    m.loss_ci_low_pct, m.loss_ci_high_pct = 100.0 * low, 100.0 * high

    # UDP downstream — measures the shaper itself. Offer above the configured
    # rate so tbf, not the sender, is the binding constraint.
    offer = f"{int(profile.rate_kbps * 1.5)}K"
    m.udp_down_kbps = _iperf_kbps(
        _exec(
            service,
            ["iperf3", "-c", IPERF_HOST, "-R", "-u", "-b", offer,
             "-t", str(duration), "-J"],
            timeout=duration + 90.0,
        )
    )

    # TCP downstream and upstream — informational goodput.
    m.tcp_down_kbps = _iperf_kbps(
        _exec(service, ["iperf3", "-c", IPERF_HOST, "-R", "-t", str(duration), "-J"],
              timeout=duration + 90.0)
    )
    m.tcp_up_kbps = _iperf_kbps(
        _exec(service, ["iperf3", "-c", IPERF_HOST, "-t", str(duration), "-J"],
              timeout=duration + 90.0)
    )
    m.mathis_kbps = mathis_kbps(profile)

    m.checks = build_checks(profile, m)
    return m


def build_checks(profile: LinkProfile, m: Measurement) -> list[Check]:
    checks = [
        Check(
            name="downstream rate (UDP)",
            configured=profile.rate_kbps,
            measured=m.udp_down_kbps,
            unit="kbit/s",
            tolerance=VERIFY_TOLERANCE,
            gating=True,
        ),
        Check(
            name="RTT",
            configured=profile.delay_ms,
            measured=m.rtt_mean_ms,
            unit="ms",
            tolerance=VERIFY_TOLERANCE,
            gating=True,
        ),
    ]

    # Loss is gated by whether the configured value falls inside the measured
    # 95% CI, not by a flat 10%. At 0.01% loss a 10% relative check would need
    # millions of packets; pretending otherwise would be a fake gate.
    in_ci = m.loss_ci_low_pct <= profile.loss_pct <= m.loss_ci_high_pct
    ci_width = m.loss_ci_high_pct - m.loss_ci_low_pct
    informative = ci_width <= max(0.5 * profile.loss_pct, 0.05)
    checks.append(
        Check(
            name="loss",
            configured=profile.loss_pct,
            measured=m.loss_pct,
            unit="%",
            tolerance=math.inf if in_ci else 0.0,
            gating=informative,
            note=(
                f"95% CI [{m.loss_ci_low_pct:.3f}, {m.loss_ci_high_pct:.3f}]% "
                f"over {m.ping_sent} packets"
                + ("" if informative else " — too few packets to gate; informational")
            ),
        )
    )

    checks.append(
        Check(
            name="downstream goodput (TCP)",
            configured=profile.rate_kbps,
            measured=m.tcp_down_kbps,
            unit="kbit/s",
            tolerance=math.inf,
            gating=False,
            note=(
                f"Mathis ceiling ~{m.mathis_kbps:.0f} kbit/s — informational, "
                "see the module docstring"
            ),
        )
    )
    return checks


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------


def render(measurements: list[Measurement]) -> str:
    lines: list[str] = []
    header = f"{'profile':<8} {'check':<26} {'configured':>12} {'measured':>12} {'err':>8}  result"
    for m in measurements:
        lines.append("")
        lines.append(f"=== {m.profile}  ({m.service}, shaping direction: {m.direction}) ===")
        lines.append(header)
        lines.append("-" * len(header))
        for c in m.checks:
            verdict = ("PASS" if c.passed else "FAIL") if c.gating else "info"
            err = "-" if math.isinf(c.rel_error) else f"{c.rel_error * 100:6.1f}%"
            measured = "inf" if math.isinf(c.measured) else f"{c.measured:12.2f}"
            lines.append(
                f"{m.profile:<8} {c.name:<26} {c.configured:>12.2f} {measured:>12} "
                f"{err:>8}  {verdict}"
            )
            if c.note:
                lines.append(f"{'':<8} {'':<26} {c.note}")
        if m.direction == "egress":
            lines.append(
                f"{'':<8} WARNING: downstream is UNSHAPED (direction=egress). The 450 KB "
                "page travels\n         downstream, so a completion gap measured in this "
                "configuration is not valid."
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify the netem testbed (CLAUDE.md §4.2)")
    parser.add_argument("--duration", type=int, default=8, help="iperf3 seconds per direction")
    parser.add_argument("--ping-count", type=int, default=2000, help="packets for RTT/loss")
    parser.add_argument(
        "--classes",
        default="HIGH,MEDIUM,LOW",
        help="comma-separated subset of classes to verify",
    )
    parser.add_argument("--json-out", type=Path, default=None, help="write raw measurements here")
    args = parser.parse_args(argv)

    if shutil.which("docker") is None:
        print("verify-testbed: `docker` is not on PATH.", file=sys.stderr)
        return 2

    wanted = [AccessClass[name.strip().upper()] for name in args.classes.split(",") if name.strip()]

    try:
        measurements = [
            measure(PROFILES[cls], duration=args.duration, ping_count=args.ping_count)
            for cls in wanted
        ]
    except TestbedError as exc:
        print(f"verify-testbed: {exc}", file=sys.stderr)
        print(
            "\nIs the environment up? Try:  docker compose up -d --build",
            file=sys.stderr,
        )
        return 2

    print(render(measurements))

    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps([asdict(m) for m in measurements], indent=2), encoding="utf-8"
        )
        print(f"\nwrote {args.json_out}")

    failed = [m for m in measurements if not m.passed]
    print("")
    if failed:
        names = ", ".join(m.profile for m in failed)
        print(f"VERIFICATION FAILED for: {names}")
        print("Do not run an experiment against this testbed. Every number downstream")
        print("of an unverified testbed is invalid (CLAUDE.md §4.2).")
        return 1

    print("VERIFICATION PASSED — all gating checks within "
          f"{VERIFY_TOLERANCE * 100:.0f}% of configuration.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
