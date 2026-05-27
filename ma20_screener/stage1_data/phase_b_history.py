"""Phase B: fetch 60-day OHLCV and market capitalization from Financial
Modeling Prep (FMP) free tier, then apply the >=$1B filter.

Two FMP endpoints are used:

  * /api/v3/historical-price-full/{1-3 symbols} → daily OHLCV.
    FMP requires symbols within one batch to share an exchange,
    so the universe is grouped by exchange before slicing into
    batches of 3.

  * /api/v3/quote/{up to 50 symbols} → marketCap.

Free-tier limits: 250 calls/day, 5 calls/min. The batch loop runs
single-worker with a 12.5 s inter-call sleep to stay under the
per-minute cap (~4.8 calls/min sustained).

`FMPNotFound` (symbol absent from a clean response) is treated as a
permanent answer per symbol. `FMPRateLimited` (HTTP 429) is retried
with a longer initial backoff than generic transient errors since the
rate-limit window is 60 seconds.
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
from ma20_screener.utils import fmp
from ma20_screener.utils.market_time import get_last_n_closed_trading_days
from ma20_screener.stage1_data.phase_a_universe import UniverseEntry

_OHLCV_COLS = ["Open", "High", "Low", "Close", "Volume"]

# Pad either side of the expected 60-trading-day window when asking
# FMP for candles. Absorbs weekends/holidays plus a safety buffer.
_HISTORICAL_WINDOW_PAD_DAYS = 7
# Backoff used specifically for HTTP 429 (the 60 s rate-limit window).
_RATE_LIMIT_INITIAL_DELAY_S = 20.0


@dataclass(frozen=True)
class HistoryEntry:
    ticker: str
    exchange: str
    market_cap: float
    ohlcv: pd.DataFrame  # exactly N rows, indexed by trading date, columns = _OHLCV_COLS


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


def _call_with_retry(
    func: Callable[..., dict],
    *args,
    retries: int,
    retry_delay_s: float,
    rate_limit_delay_s: float = _RATE_LIMIT_INITIAL_DELAY_S,
    label: str = "fmp",
) -> tuple[Optional[dict], Optional[str]]:
    """Call `func(*args)` returning a dict, with bounded retry on
    transient errors. `FMPNotFound` is permanent (no retry).
    `FMPRateLimited` uses a longer initial delay than generic errors;
    both still double per attempt.

    Returns (result, None) on success or (None, reason) on permanent
    failure / exhausted retries."""
    total_attempts = max(1, retries + 1)
    delay = retry_delay_s
    rl_delay = rate_limit_delay_s
    last_exc: Optional[Exception] = None
    last_was_rate_limit = False
    for attempt in range(total_attempts):
        try:
            return func(*args), None
        except fmp.FMPNotFound as e:
            return None, f"{label}: {e}"
        except fmp.FMPRateLimited as e:
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


def _chunks(items: list, size: int):
    """Yield successive `size`-sized chunks from `items`."""
    for i in range(0, len(items), size):
        yield items[i:i + size]


def run_phase_b(
    universe: list[UniverseEntry],
    history_trading_days: int,
    workers: int,           # currently ignored — FMP rate limit forces serial
    fetch_sleep_ms: int,
    min_market_cap_usd: float,
    fmp_api_key: str,
    retries: int = 3,
    retry_delay_s: float = 15.0,
) -> list[HistoryEntry]:
    """Run Phase B end-to-end.

       1. Group universe by exchange (FMP /historical-price-full
          requires same-exchange within a batch).
       2. For each exchange group, slice into batches of
          fmp.HISTORICAL_BATCH_MAX and fetch OHLCV. Validate each
          returned DataFrame against the expected 60-day window.
       3. Batch the survivors into groups of fmp.QUOTE_BATCH_MAX and
          fetch marketCap via /quote.
       4. Compose HistoryEntry; apply >=`min_market_cap_usd` filter.

    The single-worker design and `fetch_sleep_ms` pacing keep us
    safely under FMP free's 5 calls/min ceiling.
    """
    log = get_logger()
    log_stage_start("STAGE 1 — Phase B (FMP OHLCV + marketCap filter)")

    expected_dates = get_last_n_closed_trading_days(history_trading_days)
    pad = pd.Timedelta(days=_HISTORICAL_WINDOW_PAD_DAYS)
    window_start = (expected_dates[0] - pad).strftime("%Y-%m-%d")
    window_end = (expected_dates[-1] + pd.Timedelta(days=1)).strftime("%Y-%m-%d")

    sleep_s = max(0.0, fetch_sleep_ms / 1000.0)
    n_hist_batches = (
        (len([u for u in universe if u.exchange == "NASDAQ"]) + fmp.HISTORICAL_BATCH_MAX - 1)
        // fmp.HISTORICAL_BATCH_MAX
        + (len([u for u in universe if u.exchange != "NASDAQ"]) + fmp.HISTORICAL_BATCH_MAX - 1)
        // fmp.HISTORICAL_BATCH_MAX
    )
    log.info(
        f"Phase B: pulling {history_trading_days} trading days "
        f"({expected_dates[0].strftime('%Y-%m-%d')} → "
        f"{expected_dates[-1].strftime('%Y-%m-%d')}) "
        f"for {len(universe)} tickers from FMP "
        f"(workers=1, sleep_ms={fetch_sleep_ms}, "
        f"retries={retries}, retry_delay_s={retry_delay_s}); "
        f"expected ~{n_hist_batches} historical batches + "
        f"~{(len(universe) + fmp.QUOTE_BATCH_MAX - 1) // fmp.QUOTE_BATCH_MAX} "
        f"quote batches…"
    )

    # ---- 1. Group by exchange so each historical batch is mono-exchange.
    by_exchange: dict[str, list[UniverseEntry]] = {}
    for u in universe:
        by_exchange.setdefault(u.exchange, []).append(u)

    # ---- 2. Fetch OHLCV in 3-symbol batches per exchange group.
    ohlcv_by_ticker: dict[str, pd.DataFrame] = {}
    first_call = True
    for exchange, group in by_exchange.items():
        for batch in _chunks(group, fmp.HISTORICAL_BATCH_MAX):
            if not first_call and sleep_s > 0:
                time.sleep(sleep_s)
            first_call = False
            batch_tickers = [u.ticker for u in batch]
            result, reason = _call_with_retry(
                fmp.fetch_historical_prices_batch,
                batch_tickers, fmp_api_key, window_start, window_end,
                retries=retries,
                retry_delay_s=retry_delay_s,
                label=f"fmp historical [{exchange}]",
            )
            if reason is not None:
                # Whole-batch failure — mark every ticker in this batch as failed.
                for u in batch:
                    log_ticker_fail(u.ticker, reason)
                continue
            for u in batch:
                df = result.get(u.ticker)
                if df is None:
                    log_ticker_fail(
                        u.ticker,
                        "fmp historical: symbol absent from batch response",
                    )
                    continue
                clean, val_reason = _validate_history(df, expected_dates)
                if val_reason is not None:
                    log_ticker_fail(u.ticker, val_reason)
                    continue
                ohlcv_by_ticker[u.ticker] = clean

    log.info(
        f"Phase B: OHLCV validated for {len(ohlcv_by_ticker)} of "
        f"{len(universe)} tickers."
    )
    if not ohlcv_by_ticker:
        log_stage_summary("STAGE 1 — Phase B", entered=len(universe), passed=0)
        return []

    # ---- 3. Fetch marketCap in QUOTE_BATCH_MAX-sized batches.
    survivors = [u for u in universe if u.ticker in ohlcv_by_ticker]
    market_cap_by_ticker: dict[str, float] = {}
    for batch in _chunks(survivors, fmp.QUOTE_BATCH_MAX):
        if sleep_s > 0:
            time.sleep(sleep_s)
        batch_tickers = [u.ticker for u in batch]
        result, reason = _call_with_retry(
            fmp.fetch_quotes_batch,
            batch_tickers, fmp_api_key,
            retries=retries,
            retry_delay_s=retry_delay_s,
            label="fmp quote",
        )
        if reason is not None:
            for u in batch:
                log_ticker_fail(u.ticker, reason)
            continue
        for u in batch:
            quote = result.get(u.ticker)
            if quote is None:
                log_ticker_fail(
                    u.ticker,
                    "fmp quote: symbol absent from batch response",
                )
                continue
            mc_raw = quote.get("marketCap")
            try:
                mc = float(mc_raw) if mc_raw is not None else 0.0
            except (TypeError, ValueError):
                mc = 0.0
            if mc <= 0:
                log_ticker_fail(u.ticker, "fmp quote: no positive marketCap")
                continue
            market_cap_by_ticker[u.ticker] = mc

    # ---- 4. Compose HistoryEntry + apply $1B filter.
    passed: list[HistoryEntry] = []
    for u in survivors:
        if u.ticker not in market_cap_by_ticker:
            # Already logged above as quote-absent / no positive marketCap.
            continue
        mc = market_cap_by_ticker[u.ticker]
        if mc < min_market_cap_usd:
            log_ticker_fail(
                u.ticker,
                f"market cap ${mc:,.0f} below ${min_market_cap_usd:,.0f}",
            )
            continue
        passed.append(HistoryEntry(
            ticker=u.ticker,
            exchange=u.exchange,
            market_cap=mc,
            ohlcv=ohlcv_by_ticker[u.ticker],
        ))

    log_stage_summary("STAGE 1 — Phase B", entered=len(universe), passed=len(passed))
    return passed
