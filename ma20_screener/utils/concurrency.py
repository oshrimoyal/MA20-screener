"""Bounded thread pool with per-call throttling for yfinance fetches."""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Iterable, TypeVar

T = TypeVar("T")
R = TypeVar("R")


def parallel_map(
    func: Callable[[T], R],
    items: Iterable[T],
    workers: int,
    sleep_ms: int,
) -> list[R]:
    """Apply `func` to every item in `items` with at most `workers` threads
    running concurrently. Each call sleeps `sleep_ms` ms before invoking
    `func` to throttle requests to the data source. Returns results in the
    same order as `items`.

    Exceptions raised by `func` propagate as-is; the caller is responsible
    for wrapping `func` to return None / a sentinel on failure when that
    behaviour is desired.
    """
    items_list = list(items)
    results: list[R | None] = [None] * len(items_list)
    sleep_s = sleep_ms / 1000.0

    def _wrapped(idx_item: tuple[int, T]) -> tuple[int, R]:
        idx, item = idx_item
        if sleep_s > 0:
            time.sleep(sleep_s)
        return idx, func(item)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(_wrapped, (i, it)) for i, it in enumerate(items_list)]
        for fut in as_completed(futures):
            idx, value = fut.result()
            results[idx] = value
    return results  # type: ignore[return-value]
