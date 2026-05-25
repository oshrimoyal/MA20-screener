"""Check 3 — Volume over the last 7 trading days.

Color rule (per doc, intraday only — no reference to the previous close):
    green   if Close > Open
    red     if Close < Open
    neutral if Close == Open OR Volume == 0
"""
from __future__ import annotations

from dataclasses import dataclass

from ma20_screener.stage1_data.phase_c_indicators import TickerData

GREEN = "green"
RED = "red"
NEUTRAL = "neutral"

VOLUME_DAYS = 7


@dataclass(frozen=True)
class VolumeDay:
    day: int           # -6 ... 0 (chronological)
    volume: int
    color: str


@dataclass(frozen=True)
class VolumeResult:
    days: list[VolumeDay]  # length 7, day -6 first, day 0 last


def _volume_color(open_: float, close: float, volume: int) -> str:
    if volume == 0:
        return NEUTRAL
    if close > open_:
        return GREEN
    if close < open_:
        return RED
    return NEUTRAL


def run_check_3(td: TickerData) -> VolumeResult:
    window = td.ohlcv.iloc[-VOLUME_DAYS:]
    if len(window) != VOLUME_DAYS:
        # Stage 1 guarantees 60 rows, so this branch should never trigger;
        # defensively return what we have padded with neutral.
        raise ValueError(
            f"Check 3 needs {VOLUME_DAYS} rows; got {len(window)} for {td.ticker}"
        )

    days: list[VolumeDay] = []
    for offset, (_, row) in enumerate(window.iterrows()):
        day_index = -(VOLUME_DAYS - 1 - offset)  # -6 ... 0
        v = int(row["Volume"])
        days.append(
            VolumeDay(
                day=day_index,
                volume=v,
                color=_volume_color(float(row["Open"]), float(row["Close"]), v),
            )
        )
    return VolumeResult(days=days)
