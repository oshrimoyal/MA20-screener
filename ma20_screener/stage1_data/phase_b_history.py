"""Phase B: pull OHLCV history for the last 60 closed trading days, with
strict validation. Any ticker that fails validation is rejected and the
reason is logged. No imputation is ever performed.
"""
from __future__ import annotations

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


def _fetch_history(ticker: str, start, end) -> pd.DataFrame | None:
    """Return the raw yfinance DataFrame for `ticker` between [start, end),
    or None on error."""
    try:
        df = yf.Ticker(ticker).history(
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
            interval="1d",
            auto_adjust=False,
            actions=False,
            timeout=30,
        )
        return df
    except Exception:
        return None


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
) -> list[HistoryEntry]:
    """Fetch OHLCV for every ticker in `universe`. Reject any ticker whose
    data does not cover exactly the last `history_trading_days` closed
    trading days with no missing values."""
    log = get_logger()
    log_stage_start("STAGE 1 — Phase B (OHLCV pull + strict validation)")

    expected_dates = get_last_n_closed_trading_days(history_trading_days)
    start = expected_dates[0]
    end = expected_dates[-1] + pd.Timedelta(days=1)  # yfinance end is exclusive
    log.info(
        f"Phase B: pulling {history_trading_days} trading days "
        f"({start.strftime('%Y-%m-%d')} → {expected_dates[-1].strftime('%Y-%m-%d')}) "
        f"for {len(universe)} tickers (workers={workers}, sleep_ms={fetch_sleep_ms})…"
    )

    def _job(u: UniverseEntry) -> HistoryEntry | None:
        df = _fetch_history(u.ticker, start, end)
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
