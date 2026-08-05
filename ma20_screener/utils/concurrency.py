"""Bounded thread pool with request pacing for API fetches.

Two pacing strategies are available:

  * **Rate limiter** (`rate_per_min`) — a single shared pacer that hands
    out evenly spaced slots across every worker thread. The sustained
    rate is exactly `rate_per_min` regardless of how long each call
    takes, so a slow upstream cannot silently waste the quota. This is
    the preferred mode.

  * **Per-call sleep** (`sleep_ms`) — each thread sleeps before its own
    call. The resulting rate is `workers / (sleep + latency)`, which
    drifts with upstream latency: if the API slows down, throughput
    drops and part of the paid quota goes unused. Kept for fallback.

When `rate_per_min` is set it wins and `sleep_ms` is ignored.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Iterable, Optional, TypeVar

T = TypeVar("T")
R = TypeVar("R")


class RateLimiter:
    """Paces calls across threads to a fixed sustained rate.

    Each `acquire()` reserves the next free slot on a shared timeline
    and sleeps until it arrives. Slots are spaced `60 / rate_per_min`
    seconds apart, so N threads together never exceed `rate_per_min`
    calls per minute — and, as long as enough threads are running to
    keep claiming slots, never fall short of it either.

    Deliberately burst-free: a slot that has already passed is taken
    immediately, but slots are never banked up for a later burst. That
    keeps the instantaneous rate under the ceiling too, not just the
    average, which is what an API's rate limiter actually measures.
    """

    def __init__(self, rate_per_min: float) -> None:
        if rate_per_min <= 0:
            raise ValueError("rate_per_min must be positive")
        self._interval = 60.0 / float(rate_per_min)
        self._lock = threading.Lock()
        self._next_slot = 0.0

    def acquire(self) -> None:
        """Block until this caller's slot is due."""
        with self._lock:
            now = time.monotonic()
            # A slot in the past means we are behind schedule; take it now.
            slot = self._next_slot if self._next_slot > now else now
            self._next_slot = slot + self._interval
        delay = slot - time.monotonic()
        if delay > 0:
            time.sleep(delay)


def parallel_map(
    func: Callable[[T], R],
    items: Iterable[T],
    workers: int,
    sleep_ms: int,
    rate_per_min: Optional[float] = None,
) -> list[R]:
    """Apply `func` to every item in `items` with at most `workers` threads
    running concurrently, paced so the data source is not overrun.
    Returns results in the same order as `items`.

    With `rate_per_min` set, a shared `RateLimiter` paces every call and
    `sleep_ms` is ignored; `workers` then only caps how many calls may be
    in flight at once, and should be high enough for the pool to keep
    claiming slots (roughly `rate_per_min * latency_seconds / 60`, with
    headroom for latency spikes).

    Exceptions raised by `func` propagate as-is; the caller is responsible
    for wrapping `func` to return None / a sentinel on failure when that
    behaviour is desired.
    """
    items_list = list(items)
    results: list[R | None] = [None] * len(items_list)
    sleep_s = sleep_ms / 1000.0
    limiter = RateLimiter(rate_per_min) if rate_per_min else None

    def _wrapped(idx_item: tuple[int, T]) -> tuple[int, R]:
        idx, item = idx_item
        if limiter is not None:
            limiter.acquire()
        elif sleep_s > 0:
            time.sleep(sleep_s)
        return idx, func(item)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(_wrapped, (i, it)) for i, it in enumerate(items_list)]
        for fut in as_completed(futures):
            idx, value = fut.result()
            results[idx] = value
    return results  # type: ignore[return-value]
