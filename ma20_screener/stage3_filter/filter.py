"""Stage 3 — seven-category filter (AND between categories).

A ticker passes iff every active category passes. The function also
reports which categories failed (for the CSV "Reason" column) and, when
Category 3 fails, whether the failure was caused by a negative filter
(filter a or filter b) — this is reported separately per the document.

NOTE — Category 1 disabled.
    Per operator decision, Category 1 (monthly + weekly trend) is NOT
    used as a filter factor. Check 1 in Stage 2 still runs and produces
    the monthly/weekly trend status, which is still written to the CSV
    (Trend_Month, Trend_Week columns) and the Telegram message. Only
    the pass/reject decision below ignores Category 1. The _eval_cat1
    helper is intentionally kept in this file as the canonical
    implementation of the original spec, in case the filter is
    re-enabled later — to re-enable, restore the call site in
    `evaluate()`.

NOTE — Category 4 is a pure distance test.
    Per operator decision, Category 4 no longer looks at the SMA20's
    role (support / resistance) or at breakouts. It asks one question:
    how far is the close from the SMA 20, measured in ATR%. A ticker
    passes only when it closes above the average but less than
    `sma20_max_atr_above` ATR% above it, or below the average by more
    than `sma20_min_atr_below` ATR%. Check 4 still computes the role,
    breakout and distance label, and they still reach the CSV and the
    Telegram message — they are informational only. Both thresholds
    come from config.yaml.

NOTE — Category 7 added.
    Category 7 rejects a ticker whose recent price action sits too close
    to its 52-week low. The lowest low of the last
    `low52w_lookback_sessions` sessions must be at least
    `low52w_min_pct_above` percent above the 52-week low. The intent is
    to keep falling knives out: a stock camped on its lows is in a
    downtrend, which the other categories cannot distinguish from a
    pullback. Both values come from config.yaml.

For passing tickers we additionally surface:
  * volume_positive_option (1-4): which Category 3 positive option held —
    used by Stage 4 to write the concise volume description in Telegram.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from ma20_screener.logger import (
    get_logger,
    log_stage_start,
    log_stage_summary,
)
from ma20_screener.stage2_checks.check2_candle import (
    BULLISH_ENGULFING, BULLISH_HARAMI, BULLISH_MARUBOZU, DOJI,
    DRAGONFLY_DOJI, GREEN, HAMMER, INVERTED_HAMMER, MORNING_STAR,
    PIERCING_LINE, THREE_WHITE_SOLDIERS, TWEEZER_BOTTOM,
)
from ma20_screener.stage2_checks.check3_volume import GREEN as V_GREEN, RED as V_RED
from ma20_screener.stage2_checks.check6_cci import SLOPE_POSITIVE
from ma20_screener.stage2_checks.orchestrator import Stage2Result

DIRECTIONAL_FORMATIONS = frozenset({
    DOJI, HAMMER, INVERTED_HAMMER, DRAGONFLY_DOJI,
    BULLISH_ENGULFING, BULLISH_HARAMI, PIERCING_LINE,
    TWEEZER_BOTTOM, MORNING_STAR,
})
MOMENTUM_FORMATIONS = frozenset({BULLISH_MARUBOZU, THREE_WHITE_SOLDIERS})

# Category 4 thresholds. Overridden per-run from config.yaml; these
# defaults keep `evaluate()` callable without a config.
DEFAULT_SMA20_MAX_ATR_ABOVE = 2.0
DEFAULT_SMA20_MIN_ATR_BELOW = 1.5

# Category 7 threshold. Overridden per-run from config.yaml.
DEFAULT_LOW52W_MIN_PCT_ABOVE = 10.0


@dataclass(frozen=True)
class FilterDecision:
    s2: Stage2Result
    passed: bool
    failed_categories: list[int]       # e.g. [1, 4]; empty if passed
    cat3_negative_filter: bool         # True iff Cat 3 failed due to negative filter
    volume_positive_option: int        # 1..4 if a positive option held in Cat 3, else 0


# Category 1 — trend combinations -----------------------------------------
# Kept for documentation / future re-enable. Not used by `evaluate()` —
# see the module docstring for context.

_VALID_TREND_PAIRS = frozenset({
    ("rising", "falling"),
    ("rising", "consolidating"),
    ("consolidating", "falling"),
    ("consolidating", "consolidating"),
    ("falling", "consolidating"),
})


def _eval_cat1(s2: Stage2Result) -> bool:
    return (s2.trend.monthly, s2.trend.weekly) in _VALID_TREND_PAIRS


# Category 2 — candle color + formation ------------------------------------

def _eval_cat2(s2: Stage2Result) -> bool:
    if s2.candle.color != GREEN:
        return False
    formations = set(s2.candle.formations)
    return bool(formations & DIRECTIONAL_FORMATIONS) or bool(formations & MOMENTUM_FORMATIONS)


# Category 3 — volume (a AND b) --------------------------------------------

def _eval_cat3(s2: Stage2Result) -> tuple[bool, bool, int]:
    """Returns (passed, negative_filter_triggered, positive_option).

    positive_option is 1..4 if any positive option held; 0 if none held.
    negative_filter_triggered is True only when a positive option held
    but a negative filter rejected the ticker.
    """
    by_day = {d.day: d for d in s2.volume.days}
    v0 = by_day[0]
    vm1 = by_day[-1]
    vm2 = by_day[-2]
    vm3 = by_day[-3]
    vm4 = by_day[-4]

    # Part a — positive options (OR). When several options hold we report
    # the most specific (highest-numbered) one because Stage 4 surfaces
    # that label in the Telegram message; the doc's options 3 and 4 can
    # both hold at once (V_-1 red, V_-2 red, etc.) and option 4
    # ("4+ red days") is the more informative description.
    opt = 0
    if v0.color == V_GREEN and vm1.color == V_GREEN and vm1.volume < v0.volume:
        opt = 1
    elif v0.color == V_GREEN and vm1.color == V_GREEN and vm1.volume == v0.volume:
        opt = 2
    if (
        v0.color == V_GREEN
        and vm1.color == V_RED and vm2.color == V_RED
        and vm2.volume > vm1.volume
    ):
        opt = 3
    if (
        v0.color == V_GREEN
        and vm1.color == V_RED and vm2.color == V_RED
        and vm3.color == V_RED and vm4.color == V_RED
    ):
        opt = 4

    if opt == 0:
        return False, False, 0  # part a failed; not negative-filter related

    # Part b — negative filters (BOTH must hold).
    # Filter a: NOT 4 consecutive green days at positions -1..-4.
    four_green_before = (
        vm1.color == V_GREEN and vm2.color == V_GREEN
        and vm3.color == V_GREEN and vm4.color == V_GREEN
    )
    filter_a_holds = not four_green_before

    # Filter b: NOT (3 consecutive green days at -2,-1,0 with strictly
    # declining volume -2 > -1 > 0).
    three_green_declining = (
        vm2.color == V_GREEN and vm1.color == V_GREEN and v0.color == V_GREEN
        and vm2.volume > vm1.volume > v0.volume
    )
    filter_b_holds = not three_green_declining

    if not (filter_a_holds and filter_b_holds):
        return False, True, opt
    return True, False, opt


# Category 4 — distance from the SMA 20 ------------------------------------
# `distance_atr` (Check 4) is the gap between the close and the SMA 20 as
# a percent of price, divided by ATR(14) as a percent of price — i.e. how
# many ATR%'s fit into the distance. Positive is above the average,
# negative is below it. Expressing it this way makes one threshold mean
# the same thing on a quiet stock and on a volatile one.
#
# Exactly two windows pass:
#   *  0 < d < max_atr_above  — above the average but not stretched. The
#      average is holding the price up; this is the zone we want to buy.
#   *  d < -min_atr_below     — far enough below the average that it acts
#      as a magnet and should pull the price back up toward it.
#
# Everything else is rejected on purpose:
#   *  d >= max_atr_above     — the move already happened. We do not
#      chase a stock that has run away from its average.
#   *  -min_atr_below <= d <= 0 — under the average but not far under it.
#      The average is acting as a ceiling the stock has failed to break.
#
# Both boundaries are exclusive on the passing side, and a close sitting
# exactly ON the average (d == 0) is rejected — it has not broken above.
#
# Check 4 still reports position, distance label, role and breakout, and
# those still reach the CSV and the Telegram message. They are purely
# informational now and take no part in this decision.

def _eval_cat4(
    s2: Stage2Result,
    max_atr_above: float = DEFAULT_SMA20_MAX_ATR_ABOVE,
    min_atr_below: float = DEFAULT_SMA20_MIN_ATR_BELOW,
) -> bool:
    d = s2.sma_position.distance_atr
    if 0 < d < max_atr_above:
        return True
    if d < -min_atr_below:
        return True
    return False


# Category 5 — gaps --------------------------------------------------------

def _eval_cat5(s2: Stage2Result) -> bool:
    g = s2.gaps
    if g.has_gap_above:
        return True
    if g.price_inside_gap:
        return True
    if (not g.has_gap_above) and (not g.has_gap_below):
        return True
    return False


# Category 6 — CCI ---------------------------------------------------------

def _eval_cat6(s2: Stage2Result) -> bool:
    return (
        s2.cci.slope_direction == SLOPE_POSITIVE
        and -120 <= s2.cci.cci_today <= 130
    )


# Category 7 — distance from the 52-week low --------------------------------
# Check 7 reports how far the lowest low of the recent lookback window
# sits above the 52-week low, in percent. A ticker passes only when that
# clearance reaches `min_pct_above`.
#
# The comparison is inclusive — a stock exactly at the threshold passes.
# It is the floor that matters here, not the exact boundary.
#
# A non-finite percentage means Check 7 could not evaluate the ticker: a
# 52-week low that is not a positive price, i.e. a broken feed. That is
# rejected rather than waved through. Everywhere else in this pipeline
# unusable data drops a ticker, and passing one on a bad denominator
# would be the odd one out.

def _eval_cat7(
    s2: Stage2Result,
    min_pct_above: float = DEFAULT_LOW52W_MIN_PCT_ABOVE,
) -> bool:
    pct = s2.low_proximity.pct_above_low
    if not isfinite(pct):
        return False
    return pct >= min_pct_above


# Entry points -------------------------------------------------------------

def evaluate(
    s2: Stage2Result,
    sma20_max_atr_above: float = DEFAULT_SMA20_MAX_ATR_ABOVE,
    sma20_min_atr_below: float = DEFAULT_SMA20_MIN_ATR_BELOW,
    low52w_min_pct_above: float = DEFAULT_LOW52W_MIN_PCT_ABOVE,
) -> FilterDecision:
    # Category 1 is intentionally not evaluated for the pass/reject
    # decision (operator decision — see module docstring). The monthly
    # and weekly trend are still computed by Stage 2 and surfaced in
    # the CSV and Telegram output via `s2.trend`. To re-enable
    # filtering by trend, restore the `cat1 = _eval_cat1(s2)` call and
    # its `failed.append(1)` branch below.
    cat2 = _eval_cat2(s2)
    cat3_passed, cat3_negative, vol_opt = _eval_cat3(s2)
    cat4 = _eval_cat4(s2, sma20_max_atr_above, sma20_min_atr_below)
    cat5 = _eval_cat5(s2)
    cat6 = _eval_cat6(s2)
    cat7 = _eval_cat7(s2, low52w_min_pct_above)

    failed: list[int] = []
    # Category 1 deliberately omitted.
    if not cat2:
        failed.append(2)
    if not cat3_passed:
        failed.append(3)
    if not cat4:
        failed.append(4)
    if not cat5:
        failed.append(5)
    if not cat6:
        failed.append(6)
    if not cat7:
        failed.append(7)

    return FilterDecision(
        s2=s2,
        passed=not failed,
        failed_categories=failed,
        cat3_negative_filter=cat3_negative,
        volume_positive_option=vol_opt,
    )


def run_stage3(
    items: list[Stage2Result],
    sma20_max_atr_above: float = DEFAULT_SMA20_MAX_ATR_ABOVE,
    sma20_min_atr_below: float = DEFAULT_SMA20_MIN_ATR_BELOW,
    low52w_min_pct_above: float = DEFAULT_LOW52W_MIN_PCT_ABOVE,
) -> list[FilterDecision]:
    log = get_logger()
    log_stage_start("STAGE 3 (category filter)")
    log.info(
        f"Stage 3: Category 4 passes when the close is above the SMA 20 by "
        f"less than {sma20_max_atr_above:g} ATR%, or below it by more than "
        f"{sma20_min_atr_below:g} ATR%."
    )
    log.info(
        f"Stage 3: Category 7 rejects a ticker whose recent low sits less "
        f"than {low52w_min_pct_above:g}% above its 52-week low."
    )
    decisions = [
        evaluate(s, sma20_max_atr_above, sma20_min_atr_below,
                 low52w_min_pct_above)
        for s in items
    ]
    passed = sum(1 for d in decisions if d.passed)
    log_stage_summary("STAGE 3", entered=len(items), passed=passed)
    return decisions
