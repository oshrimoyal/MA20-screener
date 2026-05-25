"""Check 1 — Monthly and Weekly trend (rising / falling / consolidating).

All trend calculations use the candle body only:
    body_high(candle) = max(Open, Close)
    body_low(candle)  = min(Open, Close)
Ranges are compared to ATR% from Stage 1 in percentages.

Tight consolidation precedes any other check.

Definitions are taken verbatim from the document.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from ma20_screener.stage1_data.phase_c_indicators import TickerData

TIGHT_FACTOR = 1.5
MONTHLY_DAYS = 22
WEEKLY_DAYS = 5

RISING = "rising"
FALLING = "falling"
CONSOLIDATING = "consolidating"


@dataclass(frozen=True)
class TrendResult:
    monthly: str
    weekly: str


def _body_high(df: pd.DataFrame) -> pd.Series:
    return np.maximum(df["Open"], df["Close"])


def _body_low(df: pd.DataFrame) -> pd.Series:
    return np.minimum(df["Open"], df["Close"])


def _monthly_status(df: pd.DataFrame, atr_pct: float, current_close: float) -> str:
    window = df.iloc[-MONTHLY_DAYS:]
    if len(window) < MONTHLY_DAYS:
        return CONSOLIDATING
    bh = _body_high(window)
    bl = _body_low(window)
    highest = float(bh.max())
    lowest = float(bl.min())
    body_range_pct = ((highest - lowest) / current_close) * 100.0

    # Tight consolidation precedes any other check.
    if body_range_pct < TIGHT_FACTOR * atr_pct:
        return CONSOLIDATING

    # Old half = first 11 days; new half = last 11 days.
    old_half = window.iloc[:11]
    new_half = window.iloc[11:]
    bl_old_min = float(_body_low(old_half).min())
    bl_new_min = float(_body_low(new_half).min())
    bh_old_max = float(_body_high(old_half).max())
    bh_new_max = float(_body_high(new_half).max())
    span = highest - lowest

    rising = (bl_new_min > bl_old_min) and (current_close >= lowest + 0.7 * span)
    falling = (bh_new_max < bh_old_max) and (current_close <= lowest + 0.3 * span)
    if rising:
        return RISING
    if falling:
        return FALLING
    return CONSOLIDATING


def _weekly_status(df: pd.DataFrame, atr_pct: float, current_close: float) -> str:
    window = df.iloc[-WEEKLY_DAYS:]
    if len(window) < WEEKLY_DAYS:
        return CONSOLIDATING
    bh = _body_high(window)
    bl = _body_low(window)
    highest = float(bh.max())
    lowest = float(bl.min())
    body_range_pct = ((highest - lowest) / current_close) * 100.0

    if body_range_pct < TIGHT_FACTOR * atr_pct:
        return CONSOLIDATING

    # Beginning = first 3 days, end = last 2 days.
    beginning = window.iloc[:3]
    end = window.iloc[3:]
    bl_beg_min = float(_body_low(beginning).min())
    bl_end_min = float(_body_low(end).min())
    bh_beg_max = float(_body_high(beginning).max())
    bh_end_max = float(_body_high(end).max())
    close_day_1 = float(window["Close"].iloc[0])

    rising = (bl_end_min > bl_beg_min) and (current_close > close_day_1)
    falling = (bh_end_max < bh_beg_max) and (current_close < close_day_1)
    if rising:
        return RISING
    if falling:
        return FALLING
    return CONSOLIDATING


def run_check_1(td: TickerData) -> TrendResult:
    current_close = float(td.ohlcv["Close"].iloc[-1])
    monthly = _monthly_status(td.ohlcv, td.atr_14_pct, current_close)
    weekly = _weekly_status(td.ohlcv, td.atr_14_pct, current_close)
    return TrendResult(monthly=monthly, weekly=weekly)
