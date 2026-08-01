"""Phase B: fetch 60-day OHLCV from Stooq, look up shares outstanding
from SEC EDGAR, compute marketCap, and apply the >=$1B filter.

This is the largest behavioural change in the project: Yahoo Finance
(yfinance) is no longer involved. Instead we combine two free,
anonymous HTTP sources:

  * Stooq — per-ticker daily CSV at
    https://stooq.com/q/d/l/?s={ticker}.us&i=d. Returns full daily
    history; we slice to the expected 60-trading-day window. No
    signup, no API key. Per-call retry-with-exponential-backoff
    handles transient errors; `StooqNotFound` (Stooq returns no CSV)
    is treated as a permanent answer and not retried.

  * SEC EDGAR XBRL Frames API — ONE bulk call per recent calendar
    quarter returns `CommonStockSharesOutstanding` for every U.S.
    filer at that period end. We iterate the most recent 2-4 quarters
    and take the first value found per CIK. Free, anonymous, but
    requires a real contact email in the User-Agent header.

For each universe ticker we:
  1. Look up shares outstanding by CIK (from the bulk SEC fetch).
     Tickers with `cik=None` or no shares data are rejected as
     "shares outstanding unavailable" (typical for ADRs filing 20-F).
  2. Fetch the daily OHLCV CSV from Stooq, validate that it covers
     the expected 60 closed trading days with no NaN values.
  3. Compute marketCap = latest_close × shares_outstanding, and read
     the traded volume of the last closed session. A ticker continues
     to Phase C only when BOTH liquidity thresholds hold:
       * marketCap >= min_market_cap_usd  (>=$1B; moved from Phase A)
       * last-day Volume > min_last_day_volume  (>1,000,000 shares)
     Failing either one rejects the ticker; the log line names every
     threshold that failed.

The shares-outstanding figure is up to 90 days stale (quarterly
filings); irrelevant for the $1B cut-off because large-cap share
counts move slowly.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

import pandas as pd

from ma20_screener.logger import (
    get_logger,
    log_stage_start,
    log_stage_summary,
    log_ticker_fail,
)
from ma20_screener.utils import sec_edgar, stooq
from ma20_screener.utils.concurrency import parallel_map
from ma20_screener.utils.market_time import get_last_n_closed_trading_days
from ma20_screener.stage1_data.phase_a_universe import UniverseEntry

_OHLCV_COLS = ["Open", "High", "Low", "Close", "Volume"]


@dataclass(frozen=True)
class HistoryEntry:
    ticker: str
    exchange: str
    market_cap: float
    last_day_volume: float  # Volume of the last closed session in the window
    ohlcv: pd.DataFrame  # exactly N rows, indexed by trading date, columns = _OHLCV_COLS


def _fetch_stooq_with_retry(
    ticker: str,
    user_agent: str,
    retries: int,
    retry_delay_s: float,
) -> tuple[Optional[pd.DataFrame], Optional[str]]:
    """Fetch full daily history from Stooq with bounded retry on
    transient errors. `StooqNotFound` is treated as permanent (no
    retry) since Stooq has answered cleanly that the ticker is not
    in its database.

    Returns (DataFrame, None) on success or (None, reason) on
    permanent failure. The reason string includes the exception type
    so the operator can distinguish Stooq HTTP throttles from genuine
    "ticker unknown" answers.
    """
    last_exc: Optional[Exception] = None
    delay = retry_delay_s
    total_attempts = max(1, retries + 1)
    for attempt in range(total_attempts):
        try:
            return stooq.fetch_ohlcv(ticker, user_agent), None
        except stooq.StooqNotFound as e:
            # Permanent — Stooq does not have this ticker. Don't retry.
            return None, f"stooq: {e}"
        except Exception as e:
            last_exc = e
            if attempt < total_attempts - 1:
                time.sleep(delay)
                delay *= 2.0
    reason = (
        f"stooq error after {total_attempts} attempts: "
        f"{type(last_exc).__name__}: {last_exc}"
    )
    return None, reason


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
    min_last_day_volume: float,
    sec_user_agent: str,
    stooq_user_agent: str,
    retries: int = 3,
    retry_delay_s: float = 5.0,
) -> list[HistoryEntry]:
    """Run Phase B end-to-end.

    1. ONE bulk fetch of `CommonStockSharesOutstanding` from SEC EDGAR
       (1-4 HTTP calls, no per-ticker round-trip).
    2. Per-ticker (parallel, throttled) Stooq OHLCV fetch + strict
       validation.
    3. Liquidity gate — BOTH conditions must hold:
         marketCap = latest close × shares >= `min_market_cap_usd`
         last-day Volume                   >  `min_last_day_volume`
    """
    log = get_logger()
    log_stage_start(
        "STAGE 1 — Phase B (Stooq OHLCV + SEC shares + marketCap/volume filter)"
    )

    # ---- 1. SEC EDGAR shares outstanding (bulk) ---------------------------
    log.info("Phase B: fetching shares outstanding from SEC EDGAR XBRL Frames …")
    shares_by_cik, quarters_used = sec_edgar.fetch_shares_outstanding(
        user_agent=sec_user_agent,
    )
    log.info(
        f"Phase B: SEC EDGAR returned shares for {len(shares_by_cik)} CIKs "
        f"(quarters: {quarters_used or '<none returned>'})."
    )

    # ---- 2. Per-ticker Stooq OHLCV + marketCap ----------------------------
    expected_dates = get_last_n_closed_trading_days(history_trading_days)
    log.info(
        f"Phase B: pulling {history_trading_days} trading days "
        f"({expected_dates[0].strftime('%Y-%m-%d')} → "
        f"{expected_dates[-1].strftime('%Y-%m-%d')}) "
        f"for {len(universe)} tickers from Stooq "
        f"(workers={workers}, sleep_ms={fetch_sleep_ms}, "
        f"retries={retries}, retry_delay_s={retry_delay_s})…"
    )
    log.info(
        f"Phase B: liquidity gate — market cap >= ${min_market_cap_usd:,.0f} "
        f"AND last-day volume > {min_last_day_volume:,.0f} shares "
        f"(both must hold)."
    )

    def _job(u: UniverseEntry) -> Optional[HistoryEntry]:
        # 2a. Shares outstanding from the pre-fetched bulk dict.
        if u.cik is None:
            log_ticker_fail(u.ticker, "shares outstanding unavailable (no SEC CIK)")
            return None
        shares = shares_by_cik.get(u.cik)
        if shares is None:
            log_ticker_fail(u.ticker, "shares outstanding unavailable")
            return None

        # 2b. OHLCV from Stooq with bounded retry.
        df, fetch_reason = _fetch_stooq_with_retry(
            u.ticker, stooq_user_agent, retries=retries, retry_delay_s=retry_delay_s
        )
        if fetch_reason is not None:
            log_ticker_fail(u.ticker, fetch_reason)
            return None
        clean, val_reason = _validate_history(df, expected_dates)
        if val_reason is not None:
            log_ticker_fail(u.ticker, val_reason)
            return None

        # 2c. Liquidity gate. Both conditions must hold for the ticker to
        # continue: marketCap = latest close × shares is at least
        # `min_market_cap_usd`, AND the last closed session traded more
        # than `min_last_day_volume` shares. Every failing threshold is
        # named in the log line so the operator sees the full picture.
        latest_close = float(clean["Close"].iloc[-1])
        market_cap = latest_close * float(shares)
        last_day_volume = float(clean["Volume"].iloc[-1])

        failures: list[str] = []
        if market_cap < min_market_cap_usd:
            failures.append(
                f"market cap ${market_cap:,.0f} below ${min_market_cap_usd:,.0f}"
            )
        if last_day_volume <= min_last_day_volume:
            failures.append(
                f"last-day volume {last_day_volume:,.0f} not above "
                f"{min_last_day_volume:,.0f}"
            )
        if failures:
            log_ticker_fail(u.ticker, "; ".join(failures))
            return None

        return HistoryEntry(
            ticker=u.ticker,
            exchange=u.exchange,
            market_cap=market_cap,
            last_day_volume=last_day_volume,
            ohlcv=clean,
        )

    results = parallel_map(_job, universe, workers=workers, sleep_ms=fetch_sleep_ms)
    passed = [r for r in results if r is not None]
    log_stage_summary("STAGE 1 — Phase B", entered=len(universe), passed=len(passed))
    return passed
