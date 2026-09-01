"""Show the SMA 20 distance for every ticker, so you can see the new
Category 4 rule with your own eyes.

    python check_sma20.py --config config.local.yaml

Prints one line per ticker: the close, the SMA 20, the distance in
percent, ATR in percent, and the multiplier the filter actually uses.
"""
from __future__ import annotations

import argparse

from ma20_screener.config_loader import load_config
from ma20_screener.logger import setup_logger
from ma20_screener.stage1_data.orchestrator import run_stage1
from ma20_screener.stage2_checks.check4_sma_position import run_check_4


def verdict(mult: float, max_above: float, min_below: float) -> tuple[str, str]:
    if mult >= max_above:
        return "REJECT", "too far above - not chasing"
    if mult > 0:
        return "PASS  ", "healthy above the average"
    if mult == 0:
        return "REJECT", "exactly on the average"
    if mult > -min_below:
        return "REJECT", "stuck under the ceiling"
    return "PASS  ", "far below - magnet"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()

    cfg = load_config(args.config)
    setup_logger(cfg.paths.log_dir)
    max_above = cfg.runtime.sma20_max_atr_above
    min_below = cfg.runtime.sma20_min_atr_below

    tickers = run_stage1(cfg)

    print()
    print(f"Category 4 rule: PASS if the multiplier is between 0 and "
          f"{max_above:g} (exclusive), or below -{min_below:g}.")
    print()
    print(f"{'ticker':<8}{'close':>10}{'SMA20':>10}{'dist %':>9}"
          f"{'ATR %':>8}{'MULT':>9}   {'cat 4':<8}why")
    print("-" * 82)

    rows = []
    for td in tickers:
        p = run_check_4(td)
        close = float(td.ohlcv["Close"].iloc[-1])
        dist_pct = (close - td.sma_20) / close * 100.0
        rows.append((p.distance_atr, td.ticker, close, td.sma_20,
                     dist_pct, td.atr_14_pct))

    for mult, tkr, close, sma, dist_pct, atr_pct in sorted(rows, reverse=True):
        state, why = verdict(mult, max_above, min_below)
        print(f"{tkr:<8}{close:>10.2f}{sma:>10.2f}{dist_pct:>+9.2f}"
              f"{atr_pct:>8.2f}{mult:>+9.2f}   {state:<8}{why}")

    passed = sum(1 for r in rows if verdict(r[0], max_above, min_below)[0].strip() == "PASS")
    print("-" * 82)
    print(f"{len(rows)} tickers checked - {passed} pass Category 4, "
          f"{len(rows) - passed} rejected.")
    print("(Category 4 only. A ticker still needs 2, 3, 5 and 6 to reach Telegram.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
