"""Helpers around the U.S. (NYSE) trading calendar.

The system must only ever look at CLOSED daily candles. If a scan runs
while the U.S. market is still open, the previous closed trading day is
used as the last candle.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pandas_market_calendars as mcal
import pytz

_NYSE = mcal.get_calendar("NYSE")
_UTC = pytz.UTC


def _now_utc() -> datetime:
    return datetime.now(tz=_UTC)


def get_last_n_closed_trading_days(n: int, now: datetime | None = None) -> list[pd.Timestamp]:
    """Return the last `n` U.S. trading days whose session has fully closed
    relative to `now` (UTC). The most recent date is last in the list."""
    if now is None:
        now = _now_utc()
    if now.tzinfo is None:
        now = _UTC.localize(now)

    # Look back enough calendar days to ensure we find >= n trading sessions.
    # 60 trading days fits comfortably in 100 calendar days; we use a wider
    # buffer to absorb long holiday weeks.
    lookback_days = max(int(n * 2), 30) + 60
    start = (now - timedelta(days=lookback_days)).date()
    end = now.date()

    schedule = _NYSE.schedule(start_date=start, end_date=end)
    if schedule.empty:
        raise RuntimeError("NYSE calendar returned no sessions in the lookback window.")

    # Keep only sessions whose market_close is strictly <= now (closed sessions).
    closed = schedule[schedule["market_close"] <= now]
    if len(closed) < n:
        raise RuntimeError(
            f"Only {len(closed)} closed trading days available in the lookback "
            f"window; need {n}."
        )

    last_n = closed.tail(n)
    # The index is the session date (midnight UTC); return as Timestamps.
    return [pd.Timestamp(d) for d in last_n.index]


def get_last_closed_trading_day(now: datetime | None = None) -> pd.Timestamp:
    """Return the date of the most recently completed U.S. trading session."""
    return get_last_n_closed_trading_days(1, now=now)[0]
