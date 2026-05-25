"""Check 2 — Last candle color and formations.

For the last closed daily candle:
  1. Determine color (green / red / neutral).
  2. If color is red: return color only; formations list is empty.
  3. Otherwise: independently evaluate all 11 listed formations and
     return the names of every one that holds.

Single-candle formations use `last` only; two-candle use `prev` + `last`;
three-candle use `prev2` + `prev` + `last`.

Edge case: if Range = 0 set Body_Ratio = 0.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from ma20_screener.stage1_data.phase_c_indicators import TickerData

GREEN = "green"
RED = "red"
NEUTRAL = "neutral"

DOJI = "Doji"
HAMMER = "Hammer"
INVERTED_HAMMER = "Inverted Hammer"
BULLISH_MARUBOZU = "Bullish Marubozu"
DRAGONFLY_DOJI = "Dragonfly Doji"
BULLISH_ENGULFING = "Bullish Engulfing"
BULLISH_HARAMI = "Bullish Harami"
PIERCING_LINE = "Piercing Line"
TWEEZER_BOTTOM = "Tweezer Bottom"
MORNING_STAR = "Morning Star"
THREE_WHITE_SOLDIERS = "Three White Soldiers"


@dataclass(frozen=True)
class Candle:
    open: float
    high: float
    low: float
    close: float
    color: str
    body: float
    range: float
    upper_wick: float
    lower_wick: float
    body_ratio: float


@dataclass(frozen=True)
class CandleResult:
    color: str
    formations: list[str]


def _candle_color(o: float, c: float) -> str:
    if c > o:
        return GREEN
    if c < o:
        return RED
    return NEUTRAL


def _build(row: pd.Series) -> Candle:
    o = float(row["Open"])
    h = float(row["High"])
    l = float(row["Low"])
    c = float(row["Close"])
    body = abs(c - o)
    rng = h - l
    upper = h - max(o, c)
    lower = min(o, c) - l
    body_ratio = 0.0 if rng == 0 else body / rng
    return Candle(
        open=o, high=h, low=l, close=c,
        color=_candle_color(o, c),
        body=body, range=rng,
        upper_wick=upper, lower_wick=lower,
        body_ratio=body_ratio,
    )


# Single-candle formations --------------------------------------------------

def _is_doji(last: Candle) -> bool:
    return last.body_ratio <= 0.05


def _is_hammer(last: Candle) -> bool:
    return (
        last.color == GREEN
        and last.body_ratio <= 0.35
        and last.lower_wick >= 2 * last.body
        and last.upper_wick <= 0.1 * last.range
    )


def _is_inverted_hammer(last: Candle) -> bool:
    return (
        last.color == GREEN
        and last.body_ratio <= 0.35
        and last.upper_wick >= 2 * last.body
        and last.lower_wick <= 0.1 * last.range
    )


def _is_bullish_marubozu(last: Candle) -> bool:
    return last.color == GREEN and last.body_ratio >= 0.95


def _is_dragonfly_doji(last: Candle) -> bool:
    return (
        last.body_ratio <= 0.05
        and last.lower_wick >= 0.6 * last.range
        and last.upper_wick <= 0.1 * last.range
    )


# Two-candle formations -----------------------------------------------------

def _is_bullish_engulfing(prev: Candle, last: Candle) -> bool:
    return (
        prev.color == RED
        and last.color == GREEN
        and last.open <= prev.close
        and last.close >= prev.open
        and last.body > prev.body
    )


def _is_bullish_harami(prev: Candle, last: Candle) -> bool:
    return (
        prev.color == RED
        and last.color == GREEN
        and last.open >= prev.close
        and last.close <= prev.open
        and last.body < prev.body
    )


def _is_piercing_line(prev: Candle, last: Candle) -> bool:
    return (
        prev.color == RED
        and last.color == GREEN
        and last.open < prev.low
        and last.close > (prev.open + prev.close) / 2
        and last.close < prev.open
    )


def _is_tweezer_bottom(prev: Candle, last: Candle) -> bool:
    if last.close <= 0:
        return False
    return (
        prev.color == RED
        and last.color == GREEN
        and abs(prev.low - last.low) / last.close < 0.001
        and prev.lower_wick >= 0.3 * prev.range
        and last.lower_wick >= 0.3 * last.range
    )


# Three-candle formations ---------------------------------------------------

def _is_morning_star(prev2: Candle, prev: Candle, last: Candle) -> bool:
    return (
        prev2.color == RED
        and prev2.body_ratio >= 0.5
        and prev.body_ratio <= 0.3
        and last.color == GREEN
        and last.body_ratio >= 0.5
        and last.close > (prev2.open + prev2.close) / 2
    )


def _is_three_white_soldiers(prev2: Candle, prev: Candle, last: Candle) -> bool:
    return (
        prev2.color == GREEN and prev.color == GREEN and last.color == GREEN
        and prev2.body_ratio >= 0.6
        and prev.body_ratio >= 0.6
        and last.body_ratio >= 0.6
        and prev.close > prev2.close
        and last.close > prev.close
        and prev.open > prev2.open
        and prev.open < prev2.close
        and last.open > prev.open
        and last.open < prev.close
    )


def run_check_2(td: TickerData) -> CandleResult:
    df = td.ohlcv
    last = _build(df.iloc[-1])

    if last.color == RED:
        return CandleResult(color=RED, formations=[])

    # Build prev / prev2 when available; the dataset always has 60 rows so
    # this is guaranteed, but we defend against shorter inputs anyway.
    prev = _build(df.iloc[-2]) if len(df) >= 2 else None
    prev2 = _build(df.iloc[-3]) if len(df) >= 3 else None

    formations: list[str] = []
    if _is_doji(last):
        formations.append(DOJI)
    if _is_hammer(last):
        formations.append(HAMMER)
    if _is_inverted_hammer(last):
        formations.append(INVERTED_HAMMER)
    if _is_bullish_marubozu(last):
        formations.append(BULLISH_MARUBOZU)
    if _is_dragonfly_doji(last):
        formations.append(DRAGONFLY_DOJI)
    if prev is not None:
        if _is_bullish_engulfing(prev, last):
            formations.append(BULLISH_ENGULFING)
        if _is_bullish_harami(prev, last):
            formations.append(BULLISH_HARAMI)
        if _is_piercing_line(prev, last):
            formations.append(PIERCING_LINE)
        if _is_tweezer_bottom(prev, last):
            formations.append(TWEEZER_BOTTOM)
    if prev is not None and prev2 is not None:
        if _is_morning_star(prev2, prev, last):
            formations.append(MORNING_STAR)
        if _is_three_white_soldiers(prev2, prev, last):
            formations.append(THREE_WHITE_SOLDIERS)

    return CandleResult(color=last.color, formations=formations)
