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
from pathlib import Path

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


@app.get("/probe/{n_bytes}")
async def probe(n_bytes: int) -> Response:
    """Incompressible payload of an exact size, for the throughput probe.

    Random bytes so gzip cannot shrink it in transit and inflate the measured
    throughput. `no-store` so a second probe measures the link again rather
    than a cache hit.
    """
    if n_bytes < 0 or n_bytes > 8 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="n_bytes out of range")
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
    if "/" in asset or "\\" in asset or asset.startswith("."):
        raise HTTPException(status_code=404, detail="not found")
    path = (STATIC_DIR / asset).resolve()
    if not path.is_file() or STATIC_DIR.resolve() not in path.parents:
        raise HTTPException(status_code=404, detail="not found")

    body = path.read_bytes()
    return Response(
        content=body,
        media_type=MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream"),
        headers={
            "Content-Length": str(len(body)),
            # Assets are immutable and content-addressed by name; a real portal
            # caches them hard. The document itself is never cached.
            "Cache-Control": "public, max-age=31536000, immutable",
            "ETag": '"' + hashlib.sha256(body).hexdigest()[:16] + '"',
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
