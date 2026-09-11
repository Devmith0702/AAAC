"""Run the mock origin: ``python -m aaac.origin``.

Binds 0.0.0.0:8002 by default to match ``http://origin:8002`` in CLAUDE.md §3.5.
"""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "aaac.origin.service:create_app",
        factory=True,
        host=os.environ.get("AAAC_ORIGIN_HOST", "0.0.0.0"),  # noqa: S104 - container-local
        port=int(os.environ.get("AAAC_ORIGIN_PORT", "8002")),
        log_level=os.environ.get("AAAC_LOG_LEVEL", "warning"),
        access_log=False,
    )


if __name__ == "__main__":
    main()
