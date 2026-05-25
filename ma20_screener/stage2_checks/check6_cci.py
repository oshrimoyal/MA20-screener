"""Check 6 — CCI 14 status: value, slope direction, location zone."""
from __future__ import annotations

from dataclasses import dataclass

from ma20_screener.stage1_data.phase_c_indicators import TickerData

SLOPE_POSITIVE = "Positive"
SLOPE_NEGATIVE = "Negative"

ZONE_ABOVE_100 = "Above +100"
ZONE_0_TO_100 = "Between 0 and +100"
ZONE_NEG_100_TO_0 = "Between -100 and 0"
ZONE_BELOW_NEG_100 = "Below -100"


@dataclass(frozen=True)
class CCIResult:
    cci_today: float
    cci_yesterday: float
    slope: float
    slope_direction: str
    zone: str


def _zone(cci_today: float) -> str:
    if cci_today > 100:
        return ZONE_ABOVE_100
    if cci_today >= 0:
        # 0 <= cci_today <= 100
        return ZONE_0_TO_100
    if cci_today >= -100:
        # -100 <= cci_today < 0
        return ZONE_NEG_100_TO_0
    return ZONE_BELOW_NEG_100


def run_check_6(td: TickerData) -> CCIResult:
    slope = td.cci_today - td.cci_yesterday
    # Per doc: Positive iff slope > 0; Negative iff slope <= 0.
    direction = SLOPE_POSITIVE if slope > 0 else SLOPE_NEGATIVE
    return CCIResult(
        cci_today=td.cci_today,
        cci_yesterday=td.cci_yesterday,
        slope=slope,
        slope_direction=direction,
        zone=_zone(td.cci_today),
    )
