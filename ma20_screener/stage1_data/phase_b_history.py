"""Phase B: fetch 60-day OHLCV from Financial Modeling Prep
(Starter tier). Market cap is already attached to each UniverseEntry
by Phase A (from the FMP company-screener response), so Phase B
makes ONE FMP call per ticker instead of two.

Endpoint: /stable/historical-price-eod/full?symbol=X&from=&to=

Starter tier limits: 300 calls/minute, no daily cap. One call per
ticker × ~1,900 NASDAQ+NYSE tickers = ~1,900 calls. The parallel_map
config (workers + sleep_ms) paces the loop under the 300/min
ceiling.

`FMPNotFound` is permanent (no retry). `FMPRateLimited` (HTTP 429)
retries with a separate longer backoff.
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
from ma20_screener.utils.concurrency import parallel_map
from ma20_screener.utils.market_time import get_last_n_closed_trading_days
from ma20_screener.stage1_data.phase_a_universe import UniverseEntry

_OHLCV_COLS = ["Open", "High", "Low", "Close", "Volume"]

# Pad either side of the expected 60-trading-day window when asking
# FMP for candles. Absorbs weekends/holidays plus a safety buffer.
_HISTORICAL_WINDOW_PAD_DAYS = 7
# Initial backoff for HTTP 429. Doubles per attempt.
_RATE_LIMIT_INITIAL_DELAY_S = 10.0
# Trailing window, in closed sessions, used for the average-volume gate.
# Must be <= history_trading_days; the 60-day window guarantees that.
_VOLUME_MA_DAYS = 14


@dataclass(frozen=True)
class HistoryEntry:
    ticker: str
    exchange: str
    market_cap: float
    volume_ma: float  # Mean Volume over the last _VOLUME_MA_DAYS closed sessions
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


def _fetch_with_retry(
    func: Callable[..., object],
    ticker: str,
    *args,
    retries: int,
    retry_delay_s: float,
    rate_limit_delay_s: float = _RATE_LIMIT_INITIAL_DELAY_S,
    label: str = "fmp",
) -> tuple[Optional[object], Optional[str]]:
    """Call `func(ticker, *args)` with bounded retry on transient
    errors. `FMPNotFound` is permanent (no retry). `FMPRateLimited`
    uses a separate initial delay; both back off exponentially.

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


def run_phase_b(
    universe: list[UniverseEntry],
    history_trading_days: int,
    workers: int,
    fetch_sleep_ms: int,
    min_market_cap_usd: float,
    min_volume_ma: float,
    fmp_api_key: str,
    retries: int = 3,
    retry_delay_s: float = 5.0,
    rate_per_min: float = 0.0,
) -> list[HistoryEntry]:
    """Run Phase B end-to-end.

       1. Compute the closed-trading-day window.
       2. Per-ticker (parallel, throttled): fetch historical OHLCV,
          validate against the 60-day window, then apply the liquidity
          gate — BOTH conditions must hold:
            marketCap        >= `min_market_cap_usd`
            mean Volume over the last 14 sessions > `min_volume_ma`

    Two FMP calls per ticker. At ~503 S&P 500 tickers and Starter's
    300 calls/min, the full run completes in ~3-4 minutes assuming
    the standard config (`workers=1`, `sleep_ms=250` → ~4 calls/sec).
    """
    log = get_logger()
    log_stage_start("STAGE 1 — Phase B (FMP OHLCV + marketCap/volume filter)")

    expected_dates = get_last_n_closed_trading_days(history_trading_days)
    pad = pd.Timedelta(days=_HISTORICAL_WINDOW_PAD_DAYS)
    window_start = (expected_dates[0] - pad).strftime("%Y-%m-%d")
    window_end = (expected_dates[-1] + pd.Timedelta(days=1)).strftime("%Y-%m-%d")

    log.info(
        f"Phase B: pulling {history_trading_days} trading days "
        f"({expected_dates[0].strftime('%Y-%m-%d')} → "
        f"{expected_dates[-1].strftime('%Y-%m-%d')}) "
        f"for {len(universe)} tickers from FMP "
        f"(workers={workers}, "
        f"{f'rate={rate_per_min:,.0f}/min' if rate_per_min else f'sleep_ms={fetch_sleep_ms}'}, "
        f"retries={retries}, retry_delay_s={retry_delay_s})…"
    )
    if rate_per_min:
        log.info(
            f"Phase B: pacing via shared rate limiter at {rate_per_min:,.0f} "
            f"calls/min (history_sleep_ms is ignored in this mode)."
        )
    else:
        log.info(
            "Phase B: pacing via per-call sleep — the effective rate follows "
            "upstream latency. Set runtime.history_rate_per_min to pace "
            "against the API quota instead."
        )
    log.info(
        f"Phase B: liquidity gate — market cap >= ${min_market_cap_usd:,.0f} "
        f"AND {_VOLUME_MA_DAYS}-day avg volume > {min_volume_ma:,.0f} shares "
        f"(both must hold)."
    )

    def _job(u: UniverseEntry) -> Optional[HistoryEntry]:
        # Historical OHLCV (the only FMP call we need per ticker —
        # marketCap was already attached in Phase A by the screener
        # for normal runs, or by the test-mode quote pass).
        df, hist_reason = _fetch_with_retry(
            fmp.fetch_historical_prices,
            u.ticker,
            fmp_api_key, window_start, window_end,
            retries=retries,
            retry_delay_s=retry_delay_s,
            label="fmp historical",
        )
        if hist_reason is not None:
            log_ticker_fail(u.ticker, hist_reason)
            return None
        clean, val_reason = _validate_history(df, expected_dates)
        if val_reason is not None:
            log_ticker_fail(u.ticker, val_reason)
            return None

        # Liquidity gate. Both conditions must hold for the ticker to
        # continue. The market-cap floor is also applied server-side by
        # Phase A's screener, but is enforced again here so test-mode
        # tickers (which bypass the screener) still get filtered. The
        # volume threshold averages the last `_VOLUME_MA_DAYS` closed
        # sessions rather than reading a single candle, so one unusually
        # quiet or unusually busy day cannot decide a ticker's fate. The
        # data is already in hand, so this costs no extra FMP call.
        # Every failing threshold is named in the log line so the
        # operator sees the full picture.
        volume_ma = float(clean["Volume"].iloc[-_VOLUME_MA_DAYS:].mean())

        failures: list[str] = []
        if u.market_cap < min_market_cap_usd:
            failures.append(
                f"market cap ${u.market_cap:,.0f} below ${min_market_cap_usd:,.0f}"
            )
        if volume_ma <= min_volume_ma:
            failures.append(
                f"{_VOLUME_MA_DAYS}-day avg volume {volume_ma:,.0f} not above "
                f"{min_volume_ma:,.0f}"
            )
        if failures:
            log_ticker_fail(u.ticker, "; ".join(failures))
            return None

        return HistoryEntry(
            ticker=u.ticker,
            exchange=u.exchange,
            market_cap=u.market_cap,
            volume_ma=volume_ma,
            ohlcv=clean,
        )

    t0 = time.monotonic()
    results = parallel_map(
        _job, universe, workers=workers, sleep_ms=fetch_sleep_ms,
        rate_per_min=rate_per_min or None,
    )
    elapsed = max(time.monotonic() - t0, 1e-9)
    # Report the rate actually achieved. If it sits well below the
    # configured ceiling, `history_workers` is the binding constraint,
    # not the limiter — raise it.
    log.info(
        f"Phase B: {len(universe)} calls in {elapsed:,.1f}s = "
        f"{len(universe) / elapsed * 60:,.0f} calls/min achieved"
        + (f" (ceiling {rate_per_min:,.0f}/min)." if rate_per_min else ".")
    )
    passed = [r for r in results if r is not None]
    log_stage_summary("STAGE 1 — Phase B", entered=len(universe), passed=len(passed))
    return passed
