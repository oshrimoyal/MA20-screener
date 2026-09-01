"""Check 5 — Open gaps relative to the current price.

The set of open gaps is reused from Stage 1 Phase C; no recomputation.
Each gap is classified into exactly one of:
    above   : gap_bottom > current_close
    below   : gap_top    < current_close
    inside  : gap_bottom <= current_close <= gap_top

Every gap also carries its DISTANCE from the price, measured to the near
edge — the first level the price would touch on its way into the gap:

    above   : gap_bottom - close
    below   : close - gap_top
    inside  : 0

`distance_atr` expresses that in ATR units, i.e. roughly how many normal
sessions of movement separate the price from the gap. That figure is for
the CSV and the Telegram message; the ranking itself uses the raw price
distance, because every gap on a ticker divides by the same ATR and the
order is therefore identical either way. Ranking on the raw distance
keeps the check well-defined even if ATR ever collapses to zero.

`nearest_position` names the side the closest gap sits on, which is what
Category 3 of the document — Category 5 here — now turns on.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from ma20_screener.stage1_data.phase_c_indicators import TickerData

POS_ABOVE = "above"
POS_BELOW = "below"
POS_INSIDE = "inside"


POS_NONE = "none"


@dataclass(frozen=True)
class GapClassified:
    position: str        # above / below / inside
    date: pd.Timestamp
    kind: str            # "Up" / "Down" (from Stage 1)
    bottom: float
    top: float
    size_pct: float
    distance: float      # price units to the near edge; 0 when inside
    distance_atr: float  # the same distance in ATR units (reporting)


@dataclass(frozen=True)
class GapsResult:
    has_gap_above: bool
    has_gap_below: bool
    price_inside_gap: bool
    gaps: list[GapClassified]
    # The side the closest open gap sits on, and how far away it is.
    # POS_NONE with distance 0 when the ticker has no open gaps at all.
    # A gap the price sits inside is at distance 0 and therefore always
    # the nearest.
    nearest_position: str
    nearest_distance_atr: float


def _classify(bottom: float, top: float, current: float) -> str:
    if bottom > current:
        return POS_ABOVE
    if top < current:
        return POS_BELOW
    return POS_INSIDE  # bottom <= current <= top


def _edge_distance(pos: str, bottom: float, top: float, close: float) -> float:
    """Price units from the close to the gap's near edge — the first
    level the price would reach on its way into the gap. Zero when the
    price is already inside it."""
    if pos == POS_ABOVE:
        return bottom - close
    if pos == POS_BELOW:
        return close - top
    return 0.0


def _nearest(gaps: list[GapClassified]) -> tuple[str, float]:
    """The side of the closest gap, and its distance in ATR units.

    Ties resolve to BELOW. An exact tie between an equidistant gap above
    and one below is a float coincidence that should never occur in
    practice, but the outcome has to be defined, and the conservative
    reading of a gap underfoot is the one this check exists to enforce.
    """
    if not gaps:
        return POS_NONE, 0.0
    closest = min(g.distance for g in gaps)
    below = [g for g in gaps if g.distance == closest and g.position == POS_BELOW]
    if below:
        return POS_BELOW, below[0].distance_atr
    pick = next(g for g in gaps if g.distance == closest)
    return pick.position, pick.distance_atr


def run_check_5(td: TickerData) -> GapsResult:
    current_close = float(td.ohlcv["Close"].iloc[-1])
    atr_pct = td.atr_14_pct
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
        dist = _edge_distance(pos, g.gap_bottom, g.gap_top, current_close)
        # Same shape as Check 4's distance: percent of price over ATR
        # percent, which reduces to (price distance / ATR).
        if atr_pct > 0 and current_close > 0:
            dist_atr = (dist / current_close) * 100.0 / atr_pct
        else:
            dist_atr = float("inf")
        classified.append(
            GapClassified(
                position=pos,
                date=g.date,
                kind=g.kind,
                bottom=g.gap_bottom,
                top=g.gap_top,
                size_pct=g.size_pct,
                distance=dist,
                distance_atr=dist_atr,
            )
        )
    nearest_pos, nearest_atr = _nearest(classified)
    return GapsResult(
        has_gap_above=above,
        has_gap_below=below,
        price_inside_gap=inside,
        gaps=classified,
        nearest_position=nearest_pos,
        nearest_distance_atr=nearest_atr,
    )
