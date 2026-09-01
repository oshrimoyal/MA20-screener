"""Check 7 — how far the recent price action sits above the 52-week low.

The point of the check is to keep falling knives out: a stock camped on
its lows is not a pullback, it is a downtrend, and the rest of the
categories cannot tell the difference.

Two numbers are produced:

  * `low_52w` — the lowest Low across the long lookback window. Computed
    in Phase B from the wider slice of the same FMP response, so it
    costs no extra call. For a recently listed ticker it covers whatever
    history exists; `low_52w_sessions` says how many sessions that was.

  * `recent_low` — the lowest Low across the last `lookback_sessions`
    closed sessions. The check deliberately uses the lowest LOW of a
    window rather than the last close, so a stock that tagged its low
    last week and has since bounced is still caught.

Their relationship is reported as a percentage:

    pct_above_low = (recent_low - low_52w) / low_52w * 100

Zero means the stock printed its 52-week low inside the lookback window.
Stage 3 owns the threshold; this module only measures, in keeping with
how the other six checks are split.

`pct_above_low` is NaN when `low_52w` is not a usable positive price
(a broken feed). Stage 3 treats a non-finite value as a rejection rather
than waving the ticker through on bad data.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from ma20_screener.stage1_data.phase_c_indicators import TickerData

DEFAULT_LOOKBACK_SESSIONS = 10


@dataclass(frozen=True)
class LowProximityResult:
    low_52w: float
    low_52w_sessions: int   # sessions the 52-week low was derived from
    recent_low: float       # lowest Low over the lookback window
    lookback_sessions: int  # sessions actually used for recent_low
    pct_above_low: float    # NaN when low_52w is not a positive price


def run_check_7(
    td: TickerData,
    lookback_sessions: int = DEFAULT_LOOKBACK_SESSIONS,
) -> LowProximityResult:
    n = max(1, int(lookback_sessions))
    window = td.ohlcv["Low"].iloc[-n:]
    recent_low = float(window.min())
    low_52w = float(td.low_52w)

    if low_52w > 0 and math.isfinite(low_52w) and math.isfinite(recent_low):
        pct = (recent_low - low_52w) / low_52w * 100.0
    else:
        pct = float("nan")

    return LowProximityResult(
        low_52w=low_52w,
        low_52w_sessions=int(td.low_52w_sessions),
        recent_low=recent_low,
        lookback_sessions=int(len(window)),
        pct_above_low=pct,
    )
