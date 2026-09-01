"""Explain, per ticker, what the two distance filters see.

    python explain.py --config config.local.yaml

Runs the real Stage 1 and Stage 2 against your config, then prints the
numbers behind Category 4 (distance from the SMA 20) and Category 7
(distance from the 52-week low), with the verdict and the reason for it.

Reads only. Writes no CSV and sends nothing to Telegram.
"""
from __future__ import annotations

import argparse
from math import isfinite

from ma20_screener.config_loader import load_config
from ma20_screener.logger import setup_logger
from ma20_screener.stage1_data.orchestrator import run_stage1
from ma20_screener.stage2_checks.orchestrator import run_stage2
from ma20_screener.stage3_filter.filter import (
    _eval_cat4, _eval_cat5, _eval_cat7, evaluate)


def cat4_why(mult: float, max_above: float, min_below: float) -> str:
    if mult >= max_above:
        return "too far above - not chasing"
    if mult > 0:
        return "healthy above the average"
    if mult == 0:
        return "exactly on the average"
    if mult > -min_below:
        return "stuck under the ceiling"
    return "far below - magnet"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    cfg = load_config(ap.parse_args().config)
    setup_logger(cfg.paths.log_dir)
    rt = cfg.runtime

    tickers = run_stage1(cfg)
    results = run_stage2(tickers, low52w_lookback_sessions=rt.low52w_lookback_sessions)

    print()
    print(f"Category 4  PASS when the SMA-20 multiplier is between 0 and "
          f"{rt.sma20_max_atr_above:g} (exclusive), or below -{rt.sma20_min_atr_below:g}")
    print(f"Category 7  PASS when the lowest low of the last "
          f"{rt.low52w_lookback_sessions} sessions is at least "
          f"{rt.low52w_min_pct_above:g}% above the 52-week low")
    print()

    for s2 in results:
        d = evaluate(s2, rt.sma20_max_atr_above, rt.sma20_min_atr_below,
                     rt.low52w_min_pct_above)
        lp = s2.low_proximity
        mult = s2.sma_position.distance_atr
        close = float(s2.ohlcv["Close"].iloc[-1])
        pct = lp.pct_above_low

        print("=" * 66)
        print(f"{s2.ticker}   close {close:,.2f}")
        print("-" * 66)
        print(f"  CATEGORY 4   SMA20 {s2.sma_20:,.2f}   ATR% {s2.atr_14_pct:.2f}")
        print(f"               multiplier {mult:+.2f}   "
              f"{'PASS' if _eval_cat4(s2, rt.sma20_max_atr_above, rt.sma20_min_atr_below) else 'REJECT'}"
              f"  ({cat4_why(mult, rt.sma20_max_atr_above, rt.sma20_min_atr_below)})")
        gp = s2.gaps
        if gp.price_inside_gap:
            gtxt = "price is inside a gap"
        elif not gp.gaps:
            gtxt = "no open gaps"
        else:
            gtxt = (f"nearest gap {gp.nearest_position} at "
                    f"{gp.nearest_distance_atr:.2f} ATR"
                    f"  (above={gp.has_gap_above} below={gp.has_gap_below})")
        print(f"  CATEGORY 5   {gtxt}")
        print(f"               {'PASS' if _eval_cat5(s2) else 'REJECT'}"
              f"  (the nearest gap decides)")
        print(f"  CATEGORY 7   52w low {lp.low_52w:,.2f} "
              f"(from {lp.low_52w_sessions} sessions)")
        print(f"               lowest low of last {lp.lookback_sessions} "
              f"sessions: {lp.recent_low:,.2f}")
        if isfinite(pct):
            print(f"               {pct:.2f}% above the 52-week low   "
                  f"{'PASS' if _eval_cat7(s2, rt.low52w_min_pct_above) else 'REJECT'}")
        else:
            print(f"               52-week low unusable   REJECT")
        print("-" * 66)
        if d.passed:
            print("  VERDICT      PASSED all categories -> would be sent to Telegram")
        else:
            print(f"  VERDICT      REJECTED - failed "
                  f"{'categories' if len(d.failed_categories) > 1 else 'category'} "
                  f"{', '.join(str(c) for c in d.failed_categories)}")

    print("=" * 66)
    print(f"{len(results)} tickers checked, "
          f"{sum(1 for s in results if evaluate(s, rt.sma20_max_atr_above, rt.sma20_min_atr_below, rt.low52w_min_pct_above).passed)} would reach Telegram.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
