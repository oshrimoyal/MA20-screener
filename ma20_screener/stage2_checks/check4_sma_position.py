"""Check 4 — Position of the last daily candle relative to the SMA 20.

Distance formula (signed, in ATR units, denominator always Close):
    Distance_in_ATR = ((Price - SMA_20) / Close) * 100 / ATR_pct
    Low_distance_in_ATR  = ((Low  - SMA_20) / Close) * 100 / ATR_pct
    High_distance_in_ATR = ((SMA_20 - High) / Close) * 100 / ATR_pct

Outputs follow the document verbatim; string values must match exactly so
Stage 3 can pattern-match them.
"""
from __future__ import annotations

from dataclasses import dataclass

from ma20_screener.stage1_data.phase_c_indicators import TickerData

ON = "On the moving average"
ABOVE = "Above"
BELOW = "Below"

LBL_ON = "On the moving average"
LBL_CLOSE = "Close (flirting)"
LBL_MEDIUM = "Medium"
LBL_FAR = "Far"
LBL_VERY_FAR = "Very far"

ROLE_SUPPORT = "Served as support"
ROLE_RESISTANCE = "Served as resistance"
ROLE_NONE = "Not relevant"

BREAK_UP = "Breakout up"
BREAK_DOWN = "Breakout down"
BREAK_NONE = "No breakout"


@dataclass(frozen=True)
class SMAPositionResult:
    sma_20: float
    position: str
    distance_atr: float        # signed ATR units
    distance_label: str
    physical_touch: bool
    role: str
    breakout: str


def _distance_label(abs_d: float) -> str:
    if abs_d <= 0.2:
        return LBL_ON
    if abs_d <= 1.0:
        return LBL_CLOSE
    if abs_d <= 2.0:
        return LBL_MEDIUM
    if abs_d <= 4.0:
        return LBL_FAR
    return LBL_VERY_FAR


def _position(abs_d: float, close: float, sma: float) -> str:
    if abs_d <= 0.2:
        return ON
    if close > sma:
        return ABOVE
    return BELOW


def _role(close: float, sma: float, touch: bool, low_atr: float, high_atr: float) -> str:
    if close > sma and (touch or low_atr <= 0.5):
        return ROLE_SUPPORT
    if close < sma and (touch or high_atr <= 0.5):
        return ROLE_RESISTANCE
    return ROLE_NONE


def _breakout(open_: float, close: float, sma: float) -> str:
    if open_ <= sma and close > sma:
        return BREAK_UP
    if open_ >= sma and close < sma:
        return BREAK_DOWN
    return BREAK_NONE


def run_check_4(td: TickerData) -> SMAPositionResult:
    df = td.ohlcv
    row = df.iloc[-1]
    open_ = float(row["Open"])
    high = float(row["High"])
    low = float(row["Low"])
    close = float(row["Close"])
    sma = td.sma_20
    atr_pct = td.atr_14_pct

    distance_atr = ((close - sma) / close) * 100.0 / atr_pct
    low_atr = ((low - sma) / close) * 100.0 / atr_pct
    high_atr = ((sma - high) / close) * 100.0 / atr_pct
    abs_d = abs(distance_atr)

    touch = (low <= sma) and (sma <= high)

    return SMAPositionResult(
        sma_20=sma,
        position=_position(abs_d, close, sma),
        distance_atr=distance_atr,
        distance_label=_distance_label(abs_d),
        physical_touch=touch,
        role=_role(close, sma, touch, low_atr, high_atr),
        breakout=_breakout(open_, close, sma),
    )
