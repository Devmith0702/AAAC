"""Rolling-window latency and error tracking for ``GET /origin/health``.

CLAUDE.md §3.5 makes this the hard requirement: M1's controller polls this every
control tick, it must be O(1), and it must never block on the request semaphore.
If health checks queue behind real traffic the controller goes blind exactly when
it matters and the whole evaluation is invalid.

**How "O(1)" is honoured, and where it is approximate.** A true rolling p99 is not
O(1). This keeps a ring of fixed-width time buckets, each holding a fixed-size
log-spaced latency histogram: constant work per observation, constant work per
read. The consequence, which goes in the report rather than being called exact:
the reported p99 is *bucket-quantised*. It is always the upper edge of the
containing bucket, so it never understates latency — understating p99 would
flatter the system.

**What is counted where.**

- ``record_served`` — a request the origin actually served. Enters the histogram.
- ``record_rejected`` — a 503 shed because the bounded queue was full. Counted as
  an error but *excluded from the histogram*: a rejection completes in
  microseconds, and folding those into the latency distribution would drag p99
  down precisely when the origin is most overloaded.
- ``record_abandoned`` — the client disconnected mid-service. Counted separately
  and treated as neither an error nor a latency sample; the origin did nothing
  wrong, and timed-out clients are an expected feature of this experiment.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass

# Log-spaced histogram covering 0.5 ms .. 60 s in 128 buckets (~9.6% per bucket).
_HIST_MIN_MS = 0.5
_HIST_MAX_MS = 60_000.0
_HIST_BUCKETS = 128
_HIST_GROWTH = (_HIST_MAX_MS / _HIST_MIN_MS) ** (1.0 / (_HIST_BUCKETS - 1))
_LOG_GROWTH = math.log(_HIST_GROWTH)


def _bucket_index(ms: float) -> int:
    if ms <= _HIST_MIN_MS:
        return 0
    return min(int(math.log(ms / _HIST_MIN_MS) / _LOG_GROWTH) + 1, _HIST_BUCKETS - 1)


def bucket_upper_ms(i: int) -> float:
    """Upper edge of histogram bucket ``i``, in milliseconds."""
    if i <= 0:
        return _HIST_MIN_MS
    return _HIST_MIN_MS * (_HIST_GROWTH**i)


@dataclass(frozen=True)
class WindowStats:
    """Aggregates over the full rolling window, plus the trailing-1 s error rate."""

    window_s: float
    served: int
    errors: int
    rejected: int
    abandoned: int
    p50_ms: float
    p99_ms: float
    err_rate_1s: float


class _Bucket:
    __slots__ = ("epoch", "served", "errors", "rejected", "abandoned", "hist")

    def __init__(self) -> None:
        self.epoch = -1
        self.served = 0
        self.errors = 0
        self.rejected = 0
        self.abandoned = 0
        self.hist = [0] * _HIST_BUCKETS

    def reset(self, epoch: int) -> None:
        self.epoch = epoch
        self.served = 0
        self.errors = 0
        self.rejected = 0
        self.abandoned = 0
        hist = self.hist
        for i in range(_HIST_BUCKETS):
            hist[i] = 0


class HealthTracker:
    """Fixed-memory rolling window over origin request outcomes."""

    def __init__(
        self,
        window_s: float = 5.0,
        n_buckets: int = 5,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if window_s <= 0:
            raise ValueError(f"window_s must be > 0, got {window_s}")
        if n_buckets < 1:
            raise ValueError(f"n_buckets must be >= 1, got {n_buckets}")
        self.window_s = window_s
        self.n_buckets = n_buckets
        self.bucket_s = window_s / n_buckets
        self._clock = clock
        self._buckets = [_Bucket() for _ in range(n_buckets)]
        # How many whole buckets make up (at least) the trailing 1 s that
        # `err_rate_1s` is named for.
        self._n_err_buckets = max(1, round(1.0 / self.bucket_s))

    # -- observation ------------------------------------------------------

    def _current(self) -> _Bucket:
        epoch = int(self._clock() / self.bucket_s)
        bucket = self._buckets[epoch % self.n_buckets]
        if bucket.epoch != epoch:
            bucket.reset(epoch)
        return bucket

    def record_served(self, duration_ms: float, ok: bool = True) -> None:
        bucket = self._current()
        bucket.served += 1
        bucket.hist[_bucket_index(duration_ms)] += 1
        if not ok:
            bucket.errors += 1

    def record_rejected(self) -> None:
        bucket = self._current()
        bucket.rejected += 1
        bucket.errors += 1

    def record_abandoned(self) -> None:
        self._current().abandoned += 1

    # -- read -------------------------------------------------------------

    def _live_buckets(self, now_epoch: int, span: int) -> list[_Bucket]:
        oldest = now_epoch - span + 1
        return [b for b in self._buckets if oldest <= b.epoch <= now_epoch]

    def _quantile_ms(self, hist: list[int], total: int, q: float) -> float:
        if total == 0:
            return 0.0
        target = math.ceil(q * total)
        seen = 0
        for i, count in enumerate(hist):
            if count == 0:
                continue
            seen += count
            if seen >= target:
                return bucket_upper_ms(i)
        return bucket_upper_ms(_HIST_BUCKETS - 1)

    def window_stats(self) -> WindowStats:
        """Aggregate the window. Constant work: fixed bucket and histogram sizes."""
        now_epoch = int(self._clock() / self.bucket_s)
        live = self._live_buckets(now_epoch, self.n_buckets)

        merged = [0] * _HIST_BUCKETS
        served = errors = rejected = abandoned = 0
        for bucket in live:
            served += bucket.served
            errors += bucket.errors
            rejected += bucket.rejected
            abandoned += bucket.abandoned
            for i, count in enumerate(bucket.hist):
                merged[i] += count

        # err_rate_1s reads only *fully elapsed* buckets, so it is a genuine
        # trailing-1 s figure lagging by at most one bucket, rather than a
        # partially-filled bucket that reads spuriously low.
        err_window = self._live_buckets(now_epoch - 1, self._n_err_buckets)
        err_served = sum(b.served for b in err_window)
        err_rejected = sum(b.rejected for b in err_window)
        err_errors = sum(b.errors for b in err_window)
        err_total = err_served + err_rejected
        err_rate_1s = (err_errors / err_total) if err_total else 0.0

        return WindowStats(
            window_s=self.window_s,
            served=served,
            errors=errors,
            rejected=rejected,
            abandoned=abandoned,
            p50_ms=self._quantile_ms(merged, served, 0.50),
            p99_ms=self._quantile_ms(merged, served, 0.99),
            err_rate_1s=err_rate_1s,
        )
