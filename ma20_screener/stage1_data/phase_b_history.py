"""Phase B: fetch 60-day OHLCV and market capitalization from Finnhub
and apply the >=$1B filter.

Two Finnhub endpoints are used per ticker (free tier, 60 calls/minute
total, one-time email signup):

  * /stock/candle    — daily OHLCV bars across a Unix-timestamp window
                       wide enough to cover the expected 60 trading
                       days plus a safety pad.
  * /stock/profile2  — company profile containing
                       `marketCapitalization` in MILLIONS of USD.

For each universe ticker we:
  1. Fetch daily candles, validate that the response covers the
     expected 60 closed trading days with no NaN values.
  2. Fetch the company profile, read marketCapitalization, multiply by
     1_000_000, apply the >=$1B filter.

`FinnhubNotFound` (Finnhub does not have data for this ticker /
window) is treated as a permanent answer and is NOT retried.
`FinnhubRateLimited` (HTTP 429) is retried with a longer backoff than
generic transient errors since the rate-limit window is 60 seconds.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

import pandas as pd

from ma20_screener.logger import (
    get_logger,
    log_stage_start,
    log_stage_summary,
    log_ticker_fail,
)
from ma20_screener.utils import finnhub
from ma20_screener.utils.concurrency import parallel_map
from ma20_screener.utils.market_time import get_last_n_closed_trading_days
from ma20_screener.stage1_data.phase_a_universe import UniverseEntry

_OHLCV_COLS = ["Open", "High", "Low", "Close", "Volume"]

# Pad each side of the expected 60-trading-day window when asking
# Finnhub for candles. Generous enough to absorb timezone offsets and
# the few calendar days of slack between two trading days.
_CANDLE_WINDOW_PAD_DAYS = 7
# Rate-limit backoff is harder than for generic transient errors
# because Finnhub's free-tier window is 60 seconds.
_RATE_LIMIT_INITIAL_DELAY_S = 20.0


@dataclass(frozen=True)
class HistoryEntry:
    ticker: str
    exchange: str
    market_cap: float
    ohlcv: pd.DataFrame  # exactly N rows, indexed by trading date, columns = _OHLCV_COLS


def _fetch_with_retry(
    func: Callable[..., object],
    ticker: str,
    *args,
    retries: int,
    retry_delay_s: float,
    rate_limit_delay_s: float = _RATE_LIMIT_INITIAL_DELAY_S,
    label: str = "finnhub",
) -> tuple[Optional[object], Optional[str]]:
    """Call `func(ticker, *args)` with bounded retry on transient
    errors. `FinnhubNotFound` is treated as permanent (no retry).
    `FinnhubRateLimited` is retried with the rate-limit-specific
    initial delay (which still doubles each attempt). Other exceptions
    use the generic `retry_delay_s` and also double.

    Returns (result, None) on success or (None, reason) on permanent
    failure / exhausted retries."""
    total_attempts = max(1, retries + 1)
    delay = retry_delay_s
    rl_delay = rate_limit_delay_s
    last_exc: Optional[Exception] = None
    last_was_rate_limit = False
    for attempt in range(total_attempts):
        try:
            return func(ticker, *args), None
        except finnhub.FinnhubNotFound as e:
            return None, f"{label}: {e}"
        except finnhub.FinnhubRateLimited as e:
            last_exc = e
            last_was_rate_limit = True
            if attempt < total_attempts - 1:
                time.sleep(rl_delay)
                rl_delay *= 2.0
        except Exception as e:
            last_exc = e
            last_was_rate_limit = False
            if attempt < total_attempts - 1:
                time.sleep(delay)
                delay *= 2.0
    if last_was_rate_limit:
        return None, f"{label} rate-limited after {total_attempts} attempts: {last_exc}"
    return None, (
        f"{label} error after {total_attempts} attempts: "
        f"{type(last_exc).__name__}: {last_exc}"
    )


def _validate_history(
    df: Optional[pd.DataFrame],
    expected_dates: list[pd.Timestamp],
) -> tuple[Optional[pd.DataFrame], Optional[str]]:
    """Strict validation against the expected trading-day list. Returns
    (clean_df, None) on success or (None, reason) on failure. clean_df
    has exactly len(expected_dates) rows indexed by date (no tz), with
    OHLCV float columns and no NaN."""
    if df is None:
        return None, "no data returned"
    if df.empty:
        return None, "empty dataframe"

    # Normalize index to date (drop tz/time).
    idx = df.index
    if isinstance(idx, pd.DatetimeIndex):
        if idx.tz is not None:
            idx = idx.tz_convert("UTC").tz_localize(None)
        df = df.copy()
        df.index = idx.normalize()

    for col in _OHLCV_COLS:
        if col not in df.columns:
            return None, f"missing column {col}"
    df = df[_OHLCV_COLS]

    expected_norm = [pd.Timestamp(d).normalize() for d in expected_dates]
    aligned = df.reindex(expected_norm)

    if len(aligned) != len(expected_norm):
        return None, "length mismatch after alignment"

    if aligned.isna().any().any():
        bad_rows = aligned.index[aligned.isna().any(axis=1)]
        first_bad = bad_rows[0].strftime("%Y-%m-%d") if len(bad_rows) > 0 else "?"
        return None, f"missing/NaN OHLCV (first missing date {first_bad})"

    aligned = aligned.astype(float)
    return aligned, None


def run_phase_b(
    universe: list[UniverseEntry],
    history_trading_days: int,
    workers: int,
    fetch_sleep_ms: int,
    min_market_cap_usd: float,
    finnhub_api_key: str,
    retries: int = 3,
    retry_delay_s: float = 10.0,
) -> list[HistoryEntry]:
    """Run Phase B end-to-end.

    1. Compute the closed-trading-day window and convert it to Unix
       timestamps (with a safety pad) for the Finnhub candle endpoint.
    2. Per-ticker (parallel, throttled):
         a. /stock/candle  -> validate against the 60-day window.
         b. /stock/profile2 -> read marketCapitalization (millions USD).
    3. marketCap = profile.marketCapitalization * 1_000_000;
       filter >= `min_market_cap_usd`.
    """
    log = get_logger()
    log_stage_start("STAGE 1 — Phase B (Finnhub OHLCV + marketCap filter)")

    expected_dates = get_last_n_closed_trading_days(history_trading_days)
    pad = pd.Timedelta(days=_CANDLE_WINDOW_PAD_DAYS)
    window_start = expected_dates[0] - pad
    window_end = expected_dates[-1] + pd.Timedelta(days=1)
    unix_from = int(window_start.tz_localize("UTC").timestamp())
    unix_to = int(window_end.tz_localize("UTC").timestamp())

    log.info(
        f"Phase B: pulling {history_trading_days} trading days "
        f"({expected_dates[0].strftime('%Y-%m-%d')} → "
        f"{expected_dates[-1].strftime('%Y-%m-%d')}) "
        f"for {len(universe)} tickers from Finnhub "
        f"(workers={workers}, sleep_ms={fetch_sleep_ms}, "
        f"retries={retries}, retry_delay_s={retry_delay_s})…"
    )

    def _job(u: UniverseEntry) -> Optional[HistoryEntry]:
        # 1. Candles.
        df, fetch_reason = _fetch_with_retry(
            finnhub.fetch_candles,
            u.ticker,
            finnhub_api_key, unix_from, unix_to,
            retries=retries,
            retry_delay_s=retry_delay_s,
            label="finnhub candle",
        )
        if fetch_reason is not None:
            log_ticker_fail(u.ticker, fetch_reason)
            return None
        clean, val_reason = _validate_history(df, expected_dates)
        if val_reason is not None:
            log_ticker_fail(u.ticker, val_reason)
            return None

        # 2. Profile (marketCapitalization in MILLIONS USD).
        profile, prof_reason = _fetch_with_retry(
            finnhub.fetch_profile,
            u.ticker,
            finnhub_api_key,
            retries=retries,
            retry_delay_s=retry_delay_s,
            label="finnhub profile",
        )
        if prof_reason is not None:
            log_ticker_fail(u.ticker, prof_reason)
            return None

        market_cap_millions = float(profile.get("marketCapitalization") or 0.0)
        market_cap = market_cap_millions * 1_000_000.0
        if market_cap < min_market_cap_usd:
            log_ticker_fail(
                u.ticker,
                f"market cap ${market_cap:,.0f} below ${min_market_cap_usd:,.0f}",
            )
            return None

        return HistoryEntry(
            ticker=u.ticker,
            exchange=u.exchange,
            market_cap=market_cap,
            ohlcv=clean,
        )

    results = parallel_map(_job, universe, workers=workers, sleep_ms=fetch_sleep_ms)
    passed = [r for r in results if r is not None]
    log_stage_summary("STAGE 1 — Phase B", entered=len(universe), passed=len(passed))
    return passed
