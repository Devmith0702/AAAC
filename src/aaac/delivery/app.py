"""Estimator/delivery service (section 3.5, port 8001).

    GET /probe/{n_bytes}   incompressible bytes, for the throughput probe
    GET /result            the exam result, in the variant the token authorises
    GET /static/{asset}    sub-resources referenced by `full` and `reduced`

`/static/*` is not in the section 3.5 table. It arrived with the CONTRACT CHANGE
that made `full` reference real sub-resources, so that its cost includes round
trips and not only bytes. See CLAUDE.md section 4.2.

THE VARIANT COMES FROM THE VERIFIED TOKEN, AND NOWHERE ELSE.

Not a query parameter, not a header, not a user agent, and never from
`true_class`. A client that could ask for `full` by editing a URL would make the
whole admission control advisory. That is a security property, not a style
preference (section 3.7).
"""

from __future__ import annotations

import hashlib
import os
import secrets
from pathlib import Path
from typing import NamedTuple

import httpx
from fastapi import FastAPI, HTTPException, Query, Response

from aaac.common.tokens import TokenError, verify_token

from .variants import STATIC_DIR, VARIANTS, render

app = FastAPI(title="AAAC delivery", version="1.0")

ORIGIN_BASE = os.environ.get("AAAC_ORIGIN_BASE", "http://origin:8002")
ORIGIN_TIMEOUT_S = float(os.environ.get("AAAC_ORIGIN_TIMEOUT_S", "10"))

#: Content types for the sub-resources we serve. Explicit rather than guessed:
#: a font served as text/plain will not load, and a silent styling failure is
#: exactly the sort of thing that survives to the demo.
MEDIA_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".woff2": "font/woff2",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".txt": "text/plain; charset=utf-8",
}

#: Bytes the probe pool holds. Requests are served as a random window of it, so
#: every response differs while nothing is generated per request.
PROBE_POOL_BYTES = 4 * 1024 * 1024

MAX_PROBE_BYTES = 8 * 1024 * 1024


class _Asset(NamedTuple):
    body: bytes
    media_type: str
    etag: str


def _load_assets() -> dict[str, _Asset]:
    """Read every sub-resource once, at import, and precompute its ETag.

    WHY THIS MATTERS FOR THE EXPERIMENT, not just for tidiness.

    Serving `full` costs five static hits per client. Reading and hashing them
    per request measured 0.455 ms each, ~2.3 ms of CPU per client, or roughly
    46 seconds of single-core time across a 20,000-client run. The origin has
    concurrency_limit 64; this service has no limit at all. If the delivery
    service saturates before the origin does, congestion collapse appears in
    the wrong component and the evaluation measures the wrong thing.
    """
    assets: dict[str, _Asset] = {}
    if not STATIC_DIR.is_dir():
        return assets
    for path in sorted(STATIC_DIR.iterdir()):
        if not path.is_file():
            continue
        body = path.read_bytes()
        assets[path.name] = _Asset(
            body=body,
            media_type=MEDIA_TYPES.get(
                path.suffix.lower(), "application/octet-stream"
            ),
            etag='"' + hashlib.sha256(body).hexdigest()[:16] + '"',
        )
    return assets


ASSETS: dict[str, _Asset] = _load_assets()

#: Allocated once. Never regenerated: entropy is drawn at startup and reused
#: through a moving window, which keeps responses distinct without paying for
#: randomness on the request path.
_PROBE_POOL = os.urandom(PROBE_POOL_BYTES)


@app.get("/probe/{n_bytes}")
async def probe(n_bytes: int) -> Response:
    """Incompressible payload of an exact size, for the throughput probe.

    Random bytes so gzip cannot shrink it in transit and inflate the measured
    throughput. `no-store` so a second probe measures the link again rather
    than a cache hit.
    """
    if n_bytes < 0 or n_bytes > MAX_PROBE_BYTES:
        raise HTTPException(status_code=400, detail="n_bytes out of range")

    # A random window of the pool. Distinct bytes per request, so a
    # content-addressing proxy cannot dedupe the probe and answer it locally --
    # that would make the probe measure the middlebox instead of the link, and
    # the probe is the measurement all of C1 rests on. no-store covers
    # compliant caches; this covers the rest.
    if n_bytes <= PROBE_POOL_BYTES:
        start = secrets.randbelow(PROBE_POOL_BYTES - n_bytes + 1)
        body = _PROBE_POOL[start : start + n_bytes]
    else:
        body = os.urandom(n_bytes)
    return Response(
        content=body,
        media_type="application/octet-stream",
        headers={
            "Cache-Control": "no-store",
            "Content-Length": str(len(body)),
        },
    )


@app.get("/static/{asset}")
async def static(asset: str) -> Response:
    """Serve one sub-resource with an accurate Content-Length.

    M3 computes goodput from Content-Length, and with sub-resources that means
    summing across every response a page pulls -- so each one has to be right.
    """
    entry = ASSETS.get(asset)
    if entry is None:
        raise HTTPException(status_code=404, detail="not found")

    return Response(
        content=entry.body,
        media_type=entry.media_type,
        headers={
            "Content-Length": str(len(entry.body)),
            # Assets are immutable and content-addressed by name; a real portal
            # caches them hard. The document itself is never cached.
            "Cache-Control": "public, max-age=31536000, immutable",
            "ETag": entry.etag,
        },
    )


async def fetch_record(index: str) -> dict:
    """Get the result record from M3's origin service.

    On a 503 the origin is shedding load. That is passed straight through and
    must not be counted as a completion (section 4.2).
    """
    url = f"{ORIGIN_BASE}/origin/result"
    async with httpx.AsyncClient(timeout=ORIGIN_TIMEOUT_S) as client:
        try:
            r = await client.get(url, params={"index": index})
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"origin unreachable: {exc}")
    if r.status_code == 503:
        raise HTTPException(status_code=503, detail="origin shedding load")
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"origin returned {r.status_code}")
    return r.json()


@app.get("/result")
async def result(
    token: str = Query(...),
    index: str = Query(...),
) -> Response:
    """The result page, in the variant the verified token authorises."""
    try:
        payload = verify_token(token)
    except TokenError as exc:
        # 401 for a bad or expired signature. No detail about which, and no
        # fallback to a default variant: an unverifiable token gets nothing.
        raise HTTPException(status_code=401, detail=str(exc))

    variant = payload.get("var")
    if variant not in VARIANTS:
        raise HTTPException(status_code=401, detail="token carries no valid variant")

    record = await fetch_record(index)
    body = render(variant, record)

    return Response(
        content=body,
        media_type="text/html; charset=utf-8",
        headers={
            "Content-Length": str(len(body)),
            "Cache-Control": "no-store",
            "X-AAAC-Variant": variant,
        },
    )
