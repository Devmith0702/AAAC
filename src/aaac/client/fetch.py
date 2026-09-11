"""Client-side page fetch: the document plus what it pulls (section 4.3 step 4).

A browser does not stop at the HTML. It parses the document, finds the
stylesheet, the font, the crest and the script, and fetches them -- in parallel,
over kept-alive connections. Until all of that finishes, the student is looking
at an unstyled or blank page. So the honest measure of "did this client
complete" is the whole page load, not the first response.

WHY PARALLEL, NOT SEQUENTIAL

This is the single most consequential choice in this module. Fetching the five
sub-resources one after another would charge `full` five serial round trips
instead of roughly two. On a 250 ms link that is ~750 ms of latency we invented,
and it lands hardest on LOW clients in `baseline` mode -- precisely the case
whose failure makes AAAC look good. Inflating it would be marking our own
homework.

A real browser opens ~6 connections per origin and fetches concurrently, so
parallel is both the realistic and the conservative choice. Bandwidth is shaped
by `tc`, so concurrency does not conjure throughput: the byte cost is unchanged
and only the round-trip cost falls, which is the honest result.

Discovery uses aaac.delivery.discovery.discover -- the same function the
payload-budget check uses. One implementation, so the measured cost and the
budgeted cost cannot describe different sets of resources.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

import httpx

from aaac.delivery.discovery import discover

#: Connections a browser keeps open per origin. Not a tuning knob -- it is here
#: to imitate a browser, and raising it would understate the round-trip cost.
MAX_PARALLEL_FETCHES = 6


@dataclass
class PageTransfer:
    """The result of loading one page, document and sub-resources together."""

    ok: bool
    status: int
    bytes: int = 0
    duration_ms: float = 0.0
    variant: str = ""
    document_bytes: int = 0
    sub_resource_bytes: int = 0
    requests: int = 1
    failed_sub_resources: tuple[str, ...] = field(default=())
    timed_out: bool = False


def _same_origin(url: str, base: str) -> bool:
    """Only fetch what the page's own origin serves.

    A third-party URL would be a real browser request, but it is not bytes this
    testbed serves or shapes, so counting it would put noise in the goodput.
    """
    a, b = urlsplit(url), urlsplit(base)
    if not a.netloc:
        return True  # relative: same origin by definition
    return (a.scheme, a.netloc) == (b.scheme, b.netloc)


async def _get(
    client: httpx.AsyncClient,
    url: str,
    sem: asyncio.Semaphore,
    deadline: float | None,
) -> tuple[str, int, bool]:
    """Fetch one sub-resource. Returns (url, bytes, ok)."""
    async with sem:
        if deadline is not None and time.monotonic() >= deadline:
            return url, 0, False
        timeout = None if deadline is None else max(0.05, deadline - time.monotonic())
        try:
            r = await client.get(url, timeout=timeout)
        except httpx.HTTPError:
            return url, 0, False
        if r.status_code != 200:
            return url, len(r.content), False
        return url, len(r.content), True


async def fetch_page(
    client: httpx.AsyncClient,
    url: str,
    *,
    expires_at: float | None = None,
    now: float | None = None,
) -> PageTransfer:
    """GET `url`, then fetch its sub-resources in parallel. Time the whole thing.

    `expires_at` is the admission window deadline in unix seconds. Missing it
    aborts the transfer and reports ok=False, exactly as a browser abandoning a
    page load would leave the student with nothing (section 4.3 step 6).
    """
    started = time.monotonic()
    deadline = None
    if expires_at is not None:
        remaining = expires_at - (now if now is not None else time.time())
        deadline = started + max(0.0, remaining)

    def elapsed_ms() -> float:
        return (time.monotonic() - started) * 1000.0

    try:
        doc_timeout = None if deadline is None else max(0.05, deadline - time.monotonic())
        resp = await client.get(url, timeout=doc_timeout)
    except httpx.HTTPError:
        return PageTransfer(
            ok=False, status=0, duration_ms=elapsed_ms(), timed_out=deadline is not None
        )

    doc = resp.content
    variant = resp.headers.get("X-AAAC-Variant", "")

    if resp.status_code != 200:
        # A 503 from the origin is not a completion, and not a timeout either.
        return PageTransfer(
            ok=False,
            status=resp.status_code,
            bytes=len(doc),
            document_bytes=len(doc),
            duration_ms=elapsed_ms(),
            variant=variant,
        )

    try:
        html = doc.decode("utf-8", errors="replace")
    except Exception:
        html = ""

    urls = [
        urljoin(str(resp.url), r.url)
        for r in discover(html)
    ]
    urls = [u for u in urls if _same_origin(u, str(resp.url))]

    sub_bytes = 0
    failed: list[str] = []
    if urls:
        sem = asyncio.Semaphore(MAX_PARALLEL_FETCHES)
        results = await asyncio.gather(
            *(_get(client, u, sem, deadline) for u in urls)
        )
        for u, n, ok in results:
            sub_bytes += n
            if not ok:
                failed.append(u)

    over = deadline is not None and time.monotonic() > deadline
    return PageTransfer(
        ok=not failed and not over,
        status=resp.status_code,
        bytes=len(doc) + sub_bytes,
        duration_ms=elapsed_ms(),
        variant=variant,
        document_bytes=len(doc),
        sub_resource_bytes=sub_bytes,
        requests=1 + len(urls),
        failed_sub_resources=tuple(failed),
        timed_out=over,
    )
