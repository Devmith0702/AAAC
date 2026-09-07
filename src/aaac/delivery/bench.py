"""Capacity check for the delivery service.

    $env:PYTHONPATH="src"
    python -m aaac.delivery.bench --concurrency 64 --seconds 10

NOT A LOAD GENERATOR. Load generation, arrival shaping and metric computation
are M3's (section 4.7). This measures one thing only: how much traffic this
service sustains before it becomes the constraint, so that a saturation event
during evaluation can be attributed to the right component.

WHY THE NUMBER MATTERS

The origin runs with `concurrency_limit: 64` and sheds past it. This service has
no limit at all. If it saturates first, congestion collapse shows up in the
wrong place and the evaluation measures the delivery service instead of
admission control. A headroom figure -- delivery capacity against the origin's
64 -- is what makes a saturation event interpretable rather than confusing.

Run it in-process (default) to measure the handler cost without a network in the
way, or against a running server with --url to include the full stack.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time

import httpx

from aaac.common.classes import AccessClass
from aaac.common.tokens import issue_token


async def _worker(
    client: httpx.AsyncClient,
    path: str,
    deadline: float,
    latencies: list[float],
    counts: dict[str, int],
) -> None:
    while time.perf_counter() < deadline:
        t0 = time.perf_counter()
        try:
            response = await client.get(path)
        except httpx.HTTPError:
            counts["errors"] += 1
            continue
        latencies.append((time.perf_counter() - t0) * 1000.0)
        counts["bytes"] += len(response.content)
        if response.status_code == 200:
            counts["ok"] += 1
        else:
            counts["errors"] += 1


async def measure(
    client: httpx.AsyncClient, path: str, concurrency: int, seconds: float
) -> dict:
    latencies: list[float] = []
    counts = {"ok": 0, "errors": 0, "bytes": 0}

    # Warm up so the first request's import/allocation cost is not counted.
    try:
        await client.get(path)
    except httpx.HTTPError:
        pass

    started = time.perf_counter()
    deadline = started + seconds
    await asyncio.gather(
        *(_worker(client, path, deadline, latencies, counts) for _ in range(concurrency))
    )
    elapsed = time.perf_counter() - started

    latencies.sort()
    def pct(p: float) -> float:
        if not latencies:
            return 0.0
        return latencies[min(len(latencies) - 1, int(len(latencies) * p))]

    return {
        "path": path,
        "rps": counts["ok"] / elapsed if elapsed else 0.0,
        "mbps": counts["bytes"] * 8 / elapsed / 1e6 if elapsed else 0.0,
        "p50": statistics.median(latencies) if latencies else 0.0,
        "p95": pct(0.95),
        "p99": pct(0.99),
        "ok": counts["ok"],
        "errors": counts["errors"],
        "elapsed": elapsed,
    }


async def run(args) -> None:
    token = issue_token("bench", AccessClass.HIGH, 1, ttl_s=3600)
    paths = [
        ("/probe/65536", "probe payload (C1)"),
        ("/static/bootstrap.min.css", "largest sub-resource, 232 KB"),
        ("/static/crest.png", "crest, 47 KB"),
    ]
    if args.url:
        client = httpx.AsyncClient(base_url=args.url, timeout=30.0)
        where = args.url
    else:
        from aaac.delivery.app import app

        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://delivery:8001",
            timeout=30.0,
        )
        where = "in-process (ASGI, no network)"

    print(f"delivery capacity -- {where}")
    print(f"concurrency {args.concurrency}, {args.seconds:.0f}s per endpoint\n")
    print(f"  {'endpoint':<30} {'req/s':>9} {'Mbit/s':>9} "
          f"{'p50 ms':>8} {'p95 ms':>8} {'p99 ms':>8} {'err':>5}")
    print("  " + "-" * 82)

    results = []
    async with client:
        for path, label in paths:
            r = await measure(client, path, args.concurrency, args.seconds)
            results.append((label, r))
            print(f"  {label:<30} {r['rps']:>9,.0f} {r['mbps']:>9,.1f} "
                  f"{r['p50']:>8.2f} {r['p95']:>8.2f} {r['p99']:>8.2f} "
                  f"{r['errors']:>5}")

    # Headroom, expressed the way it will actually be used.
    static_rps = min(r["rps"] for _, r in results if "sub-resource" in _ or "crest" in _)
    print()
    print("  Headroom against the origin")
    print(f"    origin concurrency_limit          64 concurrent requests")
    print(f"    a `full` client costs             1 document + 5 sub-resources")
    print(f"    delivery sustains                 {static_rps:,.0f} static req/s")
    print(f"    -> full-variant clients/s         {static_rps/5:,.0f}")
    print()
    print("    If the origin sheds at 64 concurrent and the delivery service")
    print("    sustains far more than that, a saturation event during evaluation")
    print("    is the origin -- which is what the experiment is about.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--concurrency", type=int, default=64,
                    help="matches the origin's concurrency_limit by default")
    ap.add_argument("--seconds", type=float, default=5.0)
    ap.add_argument("--url", default=None,
                    help="benchmark a running server instead of in-process")
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
