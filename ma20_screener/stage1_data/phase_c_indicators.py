"""Phase C: compute indicators per the document.

For every ticker that passed Phase B:
  * SMA 20 of closing prices (last value).
  * ATR 14 normalized to percent using Wilder smoothing:
        ATR_pct = (ATR_14 / current_close) * 100
  * CCI 14 today and yesterday (standard Lambert formula with constant 0.015):
        TP = (H + L + C) / 3
        SMA(TP, 14) used as the centerline
        MeanDev = mean( | TP - SMA(TP,14) | ) over the trailing 14 periods
        CCI = (TP - SMA(TP,14)) / (0.015 * MeanDev)
  * Open gaps formed inside the 60-day window:
        Up Gap   : Open(N) > High(N-1)     gap_bottom = High(N-1), gap_top = Open(N)
        Down Gap : Open(N) < Low(N-1)      gap_bottom = Open(N),  gap_top = Low(N-1)
        Size %   : ((gap_top - gap_bottom) / Close(N-1)) * 100, must be > 0.10%
        Closed   : a later candle inside the window has Low <= gap_bottom (up)
                   or High >= gap_top (down). Partial fills remain open.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from ma20_screener.logger import (
    get_logger,
    log_stage_start,
    log_stage_summary,
    log_ticker_fail,
)
from ma20_screener.stage1_data.phase_b_history import HistoryEntry

_GAP_MIN_PCT = 0.10


@dataclass(frozen=True)
class OpenGap:
    date: pd.Timestamp     # the date on which the gap formed (day N)
    kind: str              # "Up" or "Down"
    gap_bottom: float
    gap_top: float
    size_pct: float        # percentage of Close(N-1)


@dataclass(frozen=True)
class TickerData:
    ticker: str
    exchange: str
    market_cap: float
    ohlcv: pd.DataFrame
    sma_20: float
    atr_14_pct: float
    cci_today: float
    cci_yesterday: float
    open_gaps: list[OpenGap]


def _sma(series: pd.Series, length: int) -> pd.Series:
    return series.rolling(window=length, min_periods=length).mean()


def _wilder_atr(high: pd.Series, low: pd.Series, close: pd.Series, length: int) -> pd.Series:
    """Wilder's ATR. Seed = simple mean of first `length` TR values, then
    smoothed as: ATR_i = (ATR_{i-1} * (length - 1) + TR_i) / length."""
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    atr = pd.Series(np.nan, index=close.index, dtype=float)
    # Need at least `length` TR values; the first TR is NaN because
    # prev_close is NaN, so we need length+1 rows to seed properly.
    if len(tr.dropna()) < length:
        return atr

    first_valid_pos = tr.first_valid_index()
    start_pos = tr.index.get_loc(first_valid_pos)
    # Seed at index start_pos + length - 1 with the simple mean of the first
    # `length` TR values starting at start_pos.
    seed_pos = start_pos + length - 1
    if seed_pos >= len(tr):
        return atr
    atr.iloc[seed_pos] = tr.iloc[start_pos : seed_pos + 1].mean()
    for i in range(seed_pos + 1, len(tr)):
        atr.iloc[i] = (atr.iloc[i - 1] * (length - 1) + tr.iloc[i]) / length
    return atr


def _cci(high: pd.Series, low: pd.Series, close: pd.Series, length: int) -> pd.Series:
    """Standard Lambert CCI with constant 0.015."""
    tp = (high + low + close) / 3.0
    sma_tp = tp.rolling(window=length, min_periods=length).mean()
    # Mean deviation of TP from its SMA over the trailing window.
    mean_dev = tp.rolling(window=length, min_periods=length).apply(
        lambda w: np.mean(np.abs(w - w.mean())), raw=True
    )
    # Avoid divide-by-zero: when mean_dev is 0 the CCI is undefined; emit NaN.
    denom = 0.015 * mean_dev
    return (tp - sma_tp) / denom.replace(0.0, np.nan)


def _find_open_gaps(df: pd.DataFrame) -> list[OpenGap]:
    """Scan the OHLCV frame for gaps formed inside the window that have not
    yet been closed by a later candle inside the same window."""
    opens = df["Open"].to_numpy(dtype=float)
    highs = df["High"].to_numpy(dtype=float)
    lows = df["Low"].to_numpy(dtype=float)
    closes = df["Close"].to_numpy(dtype=float)
    dates = list(df.index)

    gaps: list[OpenGap] = []
    n = len(df)
    for i in range(1, n):
        open_i = opens[i]
        high_prev = highs[i - 1]
        low_prev = lows[i - 1]
        close_prev = closes[i - 1]
        if close_prev <= 0:
            continue

        kind: str | None = None
        gap_bottom: float = 0.0
        gap_top: float = 0.0
        if open_i > high_prev:
            kind = "Up"
            gap_bottom = high_prev
            gap_top = open_i
        elif open_i < low_prev:
            kind = "Down"
            gap_bottom = open_i
            gap_top = low_prev
        if kind is None:
            continue

        size_pct = ((gap_top - gap_bottom) / close_prev) * 100.0
        if size_pct <= _GAP_MIN_PCT:
            continue

        closed = False
        # Any candle from day i onward (including day i itself) that traverses
        # the full gap range fills it.
        if kind == "Up":
            for j in range(i, n):
                if lows[j] <= gap_bottom:
                    closed = True
                    break
        else:  # Down
            for j in range(i, n):
                if highs[j] >= gap_top:
                    closed = True
                    break
        if closed:
            continue

        gaps.append(
            OpenGap(
                date=pd.Timestamp(dates[i]),
                kind=kind,
                gap_bottom=float(gap_bottom),
                gap_top=float(gap_top),
                size_pct=float(size_pct),
            )
        )
    return gaps


def compute_indicators(entry: HistoryEntry) -> TickerData | None:
    """Return a TickerData with all indicators populated, or None on failure
    (e.g. CCI undefined because mean deviation collapses to zero)."""
    df = entry.ohlcv
    close = df["Close"]
    high = df["High"]
    low = df["Low"]

    sma20_series = _sma(close, 20)
    atr14_series = _wilder_atr(high, low, close, 14)
    cci14_series = _cci(high, low, close, 14)

    sma_20 = sma20_series.iloc[-1]
    atr_14 = atr14_series.iloc[-1]
    cci_today = cci14_series.iloc[-1]
    cci_yest = cci14_series.iloc[-2]
    current_close = float(close.iloc[-1])

    if not np.isfinite(sma_20):
        return None
    if not np.isfinite(atr_14) or current_close <= 0:
        return None
    if not np.isfinite(cci_today) or not np.isfinite(cci_yest):
        return None

    atr_14_pct = (float(atr_14) / current_close) * 100.0
    gaps = _find_open_gaps(df)

    return TickerData(
        ticker=entry.ticker,
        exchange=entry.exchange,
        market_cap=entry.market_cap,
        ohlcv=df,
        sma_20=float(sma_20),
        atr_14_pct=float(atr_14_pct),
        cci_today=float(cci_today),
        cci_yesterday=float(cci_yest),
        open_gaps=gaps,
    )


def run_phase_c(entries: list[HistoryEntry]) -> list[TickerData]:
    log = get_logger()
    log_stage_start("STAGE 1 — Phase C (indicator calculation)")
    log.info(f"Phase C: computing SMA20, ATR14_%, CCI14, gaps for {len(entries)} tickers…")

    out: list[TickerData] = []
    for e in entries:
        try:
            td = compute_indicators(e)
        except Exception as ex:  # defensive: indicator math should not raise
            log_ticker_fail(e.ticker, f"indicator computation error: {ex}")
            continue
        if td is None:
            log_ticker_fail(e.ticker, "indicator value not finite (insufficient variance)")
            continue
        out.append(td)
    log_stage_summary("STAGE 1 — Phase C", entered=len(entries), passed=len(out))
    return out
