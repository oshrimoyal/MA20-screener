"""Check 5 — Open gaps relative to the current price.

The set of open gaps is reused from Stage 1 Phase C; no recomputation.
Each gap is classified into exactly one of:
    above   : gap_bottom > current_close
    below   : gap_top    < current_close
    inside  : gap_bottom <= current_close <= gap_top
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from ma20_screener.stage1_data.phase_c_indicators import TickerData

POS_ABOVE = "above"
POS_BELOW = "below"
POS_INSIDE = "inside"


@dataclass(frozen=True)
class GapClassified:
    position: str        # above / below / inside
    date: pd.Timestamp
    kind: str            # "Up" / "Down" (from Stage 1)
    bottom: float
    top: float
    size_pct: float


@dataclass(frozen=True)
class GapsResult:
    has_gap_above: bool
    has_gap_below: bool
    price_inside_gap: bool
    gaps: list[GapClassified]


def _classify(bottom: float, top: float, current: float) -> str:
    if bottom > current:
        return POS_ABOVE
    if top < current:
        return POS_BELOW
    return POS_INSIDE  # bottom <= current <= top


def run_check_5(td: TickerData) -> GapsResult:
    current_close = float(td.ohlcv["Close"].iloc[-1])
    classified: list[GapClassified] = []
    above = below = inside = False

    for g in td.open_gaps:
        pos = _classify(g.gap_bottom, g.gap_top, current_close)
        if pos == POS_ABOVE:
            above = True
        elif pos == POS_BELOW:
            below = True
        else:
            inside = True
        classified.append(
            GapClassified(
                position=pos,
                date=g.date,
                kind=g.kind,
                bottom=g.gap_bottom,
                top=g.gap_top,
                size_pct=g.size_pct,
            )
        )
    return GapsResult(
        has_gap_above=above,
        has_gap_below=below,
        price_inside_gap=inside,
        gaps=classified,
    )
