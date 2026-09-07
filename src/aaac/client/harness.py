"""Run one client against REAL services and narrate every step.

    $env:PYTHONPATH="src"
    python -m aaac.client.harness

Why this exists: every component so far has been tested against a mock, and a
mock agrees with our assumptions by construction. The estimate path has never
met M1's real endpoints. That is the same failure shape as synthetic data that
is too easy -- a green result reflecting our own expectations back at us.

It is built to be run against a HALF-FINISHED system. Devmith will point it at
an admission service Thisaru is still writing. So it:

  * preflights every endpoint and says, in plain language, which one is missing
    and what it expected to find
  * never reports a missing M1 endpoint as a client problem
  * falls back to a delivery-only run so the C3 path is demonstrable with no
    admission service at all

The delivery-only path mints its own admit token. That is a HARNESS-ONLY
capability for demonstrating C3 in isolation -- it needs AAAC_TOKEN_SECRET, and
it is exactly what M1 does in production. The SDK itself never mints a token and
never infers a class from anything but a verified one (section 3.7).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

import httpx

from aaac.client.fetch import fetch_page
from aaac.client.probe import LinkObservation, run_probe, timed_poll
from aaac.client.sdk import Outcome, run_client
from aaac.common.classes import AccessClass
from aaac.common.tokens import issue_token
from aaac.estimator.infer import classify

DEFAULT_ADMISSION = "http://127.0.0.1:8000"
DEFAULT_DELIVERY = "http://127.0.0.1:8001"
DEFAULT_ORIGIN = "http://127.0.0.1:8002"

#: What each endpoint is for, in plain language, so a missing one is actionable.
ENDPOINTS = [
    ("delivery", "GET", "/probe/1024",
     "throughput probe payload (C1) -- incompressible bytes, no-store"),
    ("delivery", "GET", "/static/crest.png",
     "a `full`-variant sub-resource (C3)"),
    ("admission", "POST", "/queue/join",
     "join the queue; expects {client_id, true_class} and returns "
     "{ticket_id, join_seq, position, eta_s, poll_interval_ms}"),
    ("admission", "GET", "/queue/status/PREFLIGHT",
     "ticket status; expects a TicketStatus body (state, position, attempt, "
     "access_class, window_s, admit_token, expires_at)"),
    ("admission", "POST", "/queue/estimate",
     "accept a LinkEstimate (C1 output)"),
    ("admission", "POST", "/queue/complete",
     "report the transfer; expects {ticket_id, ok, bytes, duration_ms, variant}"),
    ("origin", "GET", "/origin/result?index=4218866",
     "the result record the delivery service renders (M3)"),
]

#: Concrete probe path -> the route template a service declares for it.
TEMPLATES = {
    "/probe/1024": "/probe/{n_bytes}",
    "/static/crest.png": "/static/{asset}",
    "/queue/status/PREFLIGHT": "/queue/status/{ticket_id}",
    "/origin/result?index=4218866": "/origin/result",
}


def h1(text: str) -> None:
    print(f"\n\033[1m{text}\033[0m\n" + "-" * len(text))


def line(label: str, value: object) -> None:
    print(f"  {label:<26} {value}")


async def _registered_routes(client: httpx.AsyncClient, base: str) -> set[str] | None:
    """Route templates the service declares, from its OpenAPI schema.

    Asking the service what it implements beats inferring it from status codes.
    A 404 is ambiguous -- FastAPI returns it both for an unregistered route and
    for a registered route whose resource does not exist. `GET
    /queue/status/PREFLIGHT` on a working admission service returns 404 "Ticket
    not found", and a status-code preflight reports that healthy endpoint as
    missing. That is a check which lies in the one direction that matters.
    """
    try:
        r = await client.get(base.rstrip("/") + "/openapi.json")
        if r.status_code != 200:
            return None
        return set(r.json().get("paths", {}))
    except (httpx.HTTPError, ValueError):
        return None


async def preflight(bases: dict[str, str]) -> dict[str, bool]:
    """Probe every endpoint and report what is there, in plain language."""
    h1("Preflight")
    alive: dict[str, bool] = {}

    async with httpx.AsyncClient(timeout=5.0) as client:
        schemas = {
            svc: await _registered_routes(client, base)
            for svc, base in bases.items()
        }

        for service, method, path, purpose in ENDPOINTS:
            url = bases[service].rstrip("/") + path
            declared = schemas.get(service)
            try:
                if method == "GET":
                    r = await client.get(url)
                else:
                    r = await client.request(method, url, json={})
                status = r.status_code

                if declared is not None:
                    # Authoritative: match against declared route templates,
                    # so a 404 for a missing *ticket* is not read as a missing
                    # *route*.
                    template = TEMPLATES.get(path, path)
                    ok = template in declared
                    detail = f"HTTP {status} -- route declared" if ok else (
                        f"HTTP {status} -- not in the service's OpenAPI schema"
                    )
                else:
                    # No schema available; fall back to the status code and say
                    # so, because this inference is weaker.
                    ok = status != 404
                    detail = f"HTTP {status} (inferred; no OpenAPI schema)"
                mark = "OK  " if ok else "MISS"
            except httpx.HTTPError as exc:
                ok = False
                mark = "DOWN"
                detail = f"{type(exc).__name__} -- service not running"

            alive[f"{service}{path}"] = ok
            alive.setdefault(service, False)
            alive[service] = alive[service] or ok
            print(f"  [{mark}] {service:<10} {method:<5} {path}")
            print(f"         {detail}")
            if not ok:
                print(f"         needs: {purpose}")

    return alive


async def delivery_only_run(delivery_base: str, index_no: str,
                            access_class: AccessClass) -> int:
    """Demonstrate the C1 measurement and C3 delivery path with no M1 at all."""
    h1(f"Delivery-only run  (no admission service; class {access_class.name})")

    if not os.environ.get("AAAC_TOKEN_SECRET"):
        print("  NOTE: AAAC_TOKEN_SECRET is unset; using common/'s dev fallback.")

    observation = LinkObservation()
    async with httpx.AsyncClient(base_url=delivery_base, timeout=30.0) as client:
        # --- C1: probe --------------------------------------------------
        t0 = time.perf_counter()
        probe = await run_probe(client, "", 65536)
        observation.record_probe(probe)
        line("probe bytes", f"{probe.bytes_received:,}")
        line("probe duration (ms)", f"{probe.duration_ms:.1f}  (first byte -> last byte)")
        line("probe throughput", f"{probe.throughput_kbps:,.0f} kbps")
        line("probe ok", probe.ok or f"NO ({probe.error})")
        if probe.degenerate:
            print()
            print("  WARNING: the probe completed too fast to time. The whole")
            print("  payload arrived in one read, so first-byte and last-byte are")
            print("  the same instant. This is an ABSENCE of timing information,")
            print("  not infinite bandwidth. The reading has been clamped to the")
            print("  top of the training distribution; treat the class below as")
            print("  meaningless. Expected on loopback; should not happen against")
            print("  a netem-shaped link, where 64 KB takes at least ~10 ms.")
            print()

        # --- C1: RTT from polls ------------------------------------------
        for _ in range(6):
            _, rtt_ms, ok = await timed_poll(client, "/probe/16")
            observation.record_request(ok, rtt_ms if ok else None)
        rtts = observation.rtt_samples_ms
        line("rtt samples", f"{len(rtts)}  mean {sum(rtts)/max(len(rtts),1):.1f} ms")

        # --- C1: classify -------------------------------------------------
        sample = observation.to_link_sample("harness-ticket")
        estimate = classify(sample)
        line("classified as", estimate.access_class.name)
        line("confidence", f"{estimate.confidence:.3f}")
        line("fallback", estimate.fallback)
        line("model_version", estimate.model_version)

        # --- C3: fetch ------------------------------------------------------
        token = issue_token("harness-ticket", access_class, 1, ttl_s=120)
        transfer = await fetch_page(
            client, f"/result?token={token}&index={index_no}"
        )
        print()
        line("variant served", transfer.variant or "(none)")
        line("document bytes", f"{transfer.document_bytes:,}")
        line("sub-resource bytes", f"{transfer.sub_resource_bytes:,}")
        line("TOTAL bytes", f"{transfer.bytes:,}")
        line("requests", transfer.requests)
        line("transfer ms", f"{transfer.duration_ms:.1f}")
        line("ok", transfer.ok)
        if transfer.status != 200:
            line("http status", transfer.status)
        if transfer.failed_sub_resources:
            line("failed sub-resources", transfer.failed_sub_resources)

    print()
    line("wall time (s)", f"{time.perf_counter() - t0:.2f}")
    if transfer.status == 502:
        print()
        print("  The delivery service could not reach the origin.")
        print("  /result renders a record from GET /origin/result (M3's service).")
        print()
        print("  NOTE: the delivery service defaults to http://origin:8002 -- a")
        print("  DOCKER hostname. Preflight above checks 127.0.0.1, so the origin")
        print("  can look reachable here while the delivery service still cannot")
        print("  resolve it. Outside Docker, start delivery with:")
        print()
        print('    $env:AAAC_ORIGIN_BASE="http://127.0.0.1:8002"')
        print("    python -m uvicorn aaac.delivery.app:app --port 8001")
        return 2
    return 0 if transfer.ok else 1


async def _start_stub_origin(origin_base: str):
    """Throwaway origin so /result can render without M3's service.

    HARNESS ONLY. This is M3's component (section 1.4) and this stand-in exists
    purely so the C3 path is demonstrable before it ships. It is not in
    src/aaac/origin/, it implements no load shedding, and nothing but this
    harness may import it. Delete the flag once M3's origin exists.
    """
    import uvicorn
    from fastapi import FastAPI

    from aaac.delivery.variants import sample_record

    stub = FastAPI()

    @stub.get("/origin/result")
    async def result(index: str):          # noqa: D401
        record = dict(sample_record())
        record["index_no"] = index
        return record

    @stub.get("/origin/health")
    async def health():
        return {"in_flight": 0, "p99_ms": 0.0, "err_rate_1s": 0.0}

    port = int(origin_base.rsplit(":", 1)[-1])
    config = uvicorn.Config(stub, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    print(f"  [stub] throwaway origin listening on 127.0.0.1:{port} (HARNESS ONLY)")

    async def stop() -> None:
        server.should_exit = True
        await task

    return stop


async def full_run(args) -> int:
    """The real thing: the SDK loop against real services."""
    h1("Full run  (admission + delivery)")
    t0 = time.perf_counter()
    outcome = await run_client(
        client_id=args.client_id,
        true_class=AccessClass[args.true_class],
        index_no=args.index,
        admission_base=args.admission,
        delivery_base=args.delivery,
        min_rtt_samples=args.min_rtt_samples,
        abandon_after_s=args.abandon_after_s,
        seed=args.seed,
    )

    line("outcome", outcome.outcome.value)
    line("ticket", outcome.ticket_id)
    line("attempts", outcome.attempts)
    line("polls", outcome.polls)
    line("estimate submitted", outcome.estimate_submitted)
    line("never classified", outcome.never_classified)
    line("variant served", outcome.variant or "(none)")
    line("TOTAL bytes", f"{outcome.bytes:,}")
    line("requests", outcome.requests)
    line("transfer ms", f"{outcome.duration_ms:.1f}")
    line("wall time (s)", f"{time.perf_counter() - t0:.2f}")
    if outcome.error:
        line("error", outcome.error)

    if outcome.outcome is Outcome.ADMISSION_UNAVAILABLE:
        print("\n  This is an INFRASTRUCTURE failure, not a client failure.")
        print("  The admission service is missing, failing, or answering with a")
        print("  body the client could not read. It is NOT counted as a timeout")
        print("  or an abandonment, and it must not be treated as one in results.")
        return 2
    return 0 if outcome.outcome is Outcome.COMPLETED else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Run one AAAC client and narrate it.")
    ap.add_argument("--admission", default=DEFAULT_ADMISSION)
    ap.add_argument("--delivery", default=DEFAULT_DELIVERY)
    ap.add_argument("--origin", default=DEFAULT_ORIGIN)
    ap.add_argument("--index", default="4218866")
    ap.add_argument("--client-id", default="harness-1")
    ap.add_argument("--true-class", default="LOW",
                    choices=[c.name for c in AccessClass])
    ap.add_argument("--variant-class", default=None,
                    choices=[c.name for c in AccessClass],
                    help="delivery-only: which class of token to mint")
    ap.add_argument("--min-rtt-samples", type=int, default=5)
    ap.add_argument("--abandon-after-s", type=float, default=60.0)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--delivery-only", action="store_true",
                    help="skip the admission service entirely")
    ap.add_argument("--stub-origin", action="store_true",
                    help="run a throwaway origin on --origin so /result can "
                         "render without M3's service (HARNESS ONLY)")
    args = ap.parse_args()

    bases = {
        "admission": args.admission,
        "delivery": args.delivery,
        "origin": args.origin,
    }

    async def go() -> int:
        stub = None
        if args.stub_origin:
            stub = await _start_stub_origin(args.origin)
        try:
            return await _run_everything()
        finally:
            if stub is not None:
                await stub()

    async def _run_everything() -> int:
        alive = await preflight(bases)

        if not alive.get("delivery"):
            h1("Cannot continue")
            print("  The delivery service is not reachable, and it serves both the")
            print("  probe (C1) and the result page (C3). Start it with:")
            print("\n    $env:PYTHONPATH=\"src\"")
            print("    python -m uvicorn aaac.delivery.app:app --port 8001\n")
            return 2

        if args.delivery_only or not alive.get("admission"):
            if not args.delivery_only:
                h1("No admission service")
                print("  Nothing is answering on the admission service, so the queue")
                print("  path cannot run. This is M1's component and its absence is")
                print("  NOT a client failure. Falling back to a delivery-only run so")
                print("  the C1 measurement and C3 delivery path are still visible.")
            cls = AccessClass[args.variant_class or args.true_class]
            return await delivery_only_run(args.delivery, args.index, cls)

        return await full_run(args)

    try:
        return asyncio.run(go())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
