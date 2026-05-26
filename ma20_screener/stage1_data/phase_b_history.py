"""Phase B: pull OHLCV history for the last 60 closed trading days, with
strict validation. Any ticker that fails validation is rejected and the
reason is logged. No imputation is ever performed.

Yahoo's chart/history endpoint (used by `yfinance.Ticker.history`) is
rate-limited more aggressively than the quote endpoint used by Phase A
(fast_info). To stay within Yahoo's limits and remain resilient to
transient errors, Phase B uses:
  * a dedicated, lower concurrency setting (`history_workers`) and a
    longer per-call sleep (`history_sleep_ms`);
  * a bounded retry-with-exponential-backoff around the actual fetch
    (`history_retries` attempts after the first try, starting at
    `history_retry_delay_s` seconds and doubling between retries);
  * explicit logging of the underlying yfinance exception type and
    message — so when Yahoo throttles, the operator sees `JSONDecodeError`
    or `HTTPError 429` instead of a generic "no data returned".
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import pandas as pd
import yfinance as yf

from ma20_screener.logger import (
    get_logger,
    log_stage_start,
    log_stage_summary,
    log_ticker_fail,
)
from ma20_screener.utils.concurrency import parallel_map
from ma20_screener.utils.market_time import get_last_n_closed_trading_days
from ma20_screener.stage1_data.phase_a_universe import UniverseEntry

_OHLCV_COLS = ["Open", "High", "Low", "Close", "Volume"]


@dataclass(frozen=True)
class HistoryEntry:
    ticker: str
    exchange: str
    market_cap: float
    ohlcv: pd.DataFrame  # exactly N rows, indexed by trading date, columns = _OHLCV_COLS


def _fetch_history_once(ticker: str, start, end) -> pd.DataFrame:
    """Single yfinance fetch. Raises on yfinance error so the caller's
    retry loop can react; returns the raw DataFrame (which may be empty
    when the symbol genuinely has no data)."""
    return yf.Ticker(ticker).history(
        start=start.strftime("%Y-%m-%d"),
        end=end.strftime("%Y-%m-%d"),
        interval="1d",
        auto_adjust=False,
        actions=False,
        timeout=30,
    )


def _fetch_history_with_retry(
    ticker: str,
    start,
    end,
    retries: int,
    retry_delay_s: float,
) -> tuple[pd.DataFrame | None, str | None]:
    """Fetch OHLCV with bounded retry on exception.

    Returns (DataFrame, None) on success (the DataFrame may still be
    empty — that is a genuine "no data" answer from Yahoo and not a
    transient error). Returns (None, reason) when every attempt
    raises; `reason` includes the exception type and message of the
    LAST attempt so the operator can see what Yahoo is actually
    returning (typical patterns: JSONDecodeError when Yahoo serves
    HTML throttle pages, HTTPError 429 / 401 / 403 when the request
    is rejected outright).
    """
    last_exc: Exception | None = None
    delay = retry_delay_s
    total_attempts = max(1, retries + 1)
    for attempt in range(total_attempts):
        try:
            return _fetch_history_once(ticker, start, end), None
        except Exception as e:
            last_exc = e
            if attempt < total_attempts - 1:
                time.sleep(delay)
                delay *= 2.0
    reason = (
        f"yfinance error after {total_attempts} attempts: "
        f"{type(last_exc).__name__}: {last_exc}"
    )
    return None, reason


def _validate_history(
    df: pd.DataFrame | None,
    expected_dates: list[pd.Timestamp],
) -> tuple[pd.DataFrame | None, str | None]:
    """Strict validation against the expected trading-day list.

    Returns (clean_df, None) on success or (None, reason) on failure. The
    clean_df has exactly len(expected_dates) rows indexed by date (no
    tz), with OHLCV float columns and no NaN.
    """
    if df is None:
        return None, "no data returned"
    if df.empty:
        return None, "empty dataframe"

    # Normalize the index to date (drop tz / time component).
    idx = df.index
    if isinstance(idx, pd.DatetimeIndex):
        if idx.tz is not None:
            idx = idx.tz_convert("UTC").tz_localize(None)
        df = df.copy()
        df.index = idx.normalize()

    # Keep only the expected OHLCV columns.
    for col in _OHLCV_COLS:
        if col not in df.columns:
            return None, f"missing column {col}"
    df = df[_OHLCV_COLS]

    # Reindex against expected_dates: any missing date -> NaN row -> failure.
    expected_norm = [pd.Timestamp(d).normalize() for d in expected_dates]
    aligned = df.reindex(expected_norm)

    if len(aligned) != len(expected_norm):
        return None, "length mismatch after alignment"

    if aligned.isna().any().any():
        # Identify the missing dates for a clearer reason.
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
    retries: int = 2,
    retry_delay_s: float = 3.0,
) -> list[HistoryEntry]:
    """Fetch OHLCV for every ticker in `universe`. Reject any ticker whose
    data does not cover exactly the last `history_trading_days` closed
    trading days with no missing values.

    `retries` is the number of *additional* attempts after the first
    yfinance call fails; `retry_delay_s` is the initial backoff (it
    doubles between successive retries).
    """
    log = get_logger()
    log_stage_start("STAGE 1 — Phase B (OHLCV pull + strict validation)")

    expected_dates = get_last_n_closed_trading_days(history_trading_days)
    start = expected_dates[0]
    end = expected_dates[-1] + pd.Timedelta(days=1)  # yfinance end is exclusive
    log.info(
        f"Phase B: pulling {history_trading_days} trading days "
        f"({start.strftime('%Y-%m-%d')} → {expected_dates[-1].strftime('%Y-%m-%d')}) "
        f"for {len(universe)} tickers "
        f"(workers={workers}, sleep_ms={fetch_sleep_ms}, "
        f"retries={retries}, retry_delay_s={retry_delay_s})…"
    )

    def _job(u: UniverseEntry) -> HistoryEntry | None:
        df, fetch_reason = _fetch_history_with_retry(
            u.ticker, start, end, retries=retries, retry_delay_s=retry_delay_s
        )
        if fetch_reason is not None:
            log_ticker_fail(u.ticker, fetch_reason)
            return None
        clean, reason = _validate_history(df, expected_dates)
        if reason is not None:
            log_ticker_fail(u.ticker, reason)
            return None
        return HistoryEntry(
            ticker=u.ticker,
            exchange=u.exchange,
            market_cap=u.market_cap,
            ohlcv=clean,
        )

    results = parallel_map(_job, universe, workers=workers, sleep_ms=fetch_sleep_ms)
    passed = [r for r in results if r is not None]
    log_stage_summary("STAGE 1 — Phase B", entered=len(universe), passed=len(passed))
    return passed
