"""Explain every category's verdict, per ticker, with the raw numbers.

    python explain.py --config config.local.yaml

Runs the real Stage 1 and Stage 2 against your config and prints what
each of the seven categories saw and decided. Use it to compare a
ticker against its chart when a verdict looks wrong.

Reads only. Writes no CSV and sends nothing to Telegram.
"""
from __future__ import annotations

import argparse
from math import isfinite

from ma20_screener.config_loader import load_config
from ma20_screener.logger import get_logger, setup_logger
from ma20_screener.stage1_data.orchestrator import run_stage1
from ma20_screener.stage2_checks.orchestrator import run_stage2
from ma20_screener.stage3_filter.filter import (
    _eval_cat1, _eval_cat2, _eval_cat3, _eval_cat4, _eval_cat5, _eval_cat6,
    _eval_cat7, evaluate,
)

M = lambda ok: "PASS  " if ok else "REJECT"


def cat4_why(m: float, hi: float, lo: float) -> str:
    if m >= hi:  return "too far above - not chasing"
    if m > 0:    return "healthy above the average"
    if m == 0:   return "exactly on the average"
    if m > -lo:  return "stuck under the ceiling"
    return "far below - magnet"


def report(s2, rt) -> bool:
    d = evaluate(s2, rt.sma20_max_atr_above, rt.sma20_min_atr_below,
                 rt.low52w_min_pct_above)
    close = float(s2.ohlcv["Close"].iloc[-1])
    gp, lp, sp = s2.gaps, s2.low_proximity, s2.sma_position

    print("=" * 72)
    print(f"{s2.ticker}   close {close:,.2f}   SMA20 {s2.sma_20:,.2f}   "
          f"ATR% {s2.atr_14_pct:.2f}")
    print("=" * 72)

    print(f"  1 TREND    {'(disabled - never rejects)':<30} "
          f"month={s2.trend.monthly} week={s2.trend.weekly}")
    print(f"              would have been {M(_eval_cat1(s2)).strip()}")

    print(f"  2 CANDLE   {M(_eval_cat2(s2))}  color={s2.candle.color}  "
          f"formations={', '.join(s2.candle.formations) or 'none'}")

    ok3, neg3, opt3 = _eval_cat3(s2)
    days = {v.day: v for v in s2.volume.days}
    seq = "  ".join(f"{k:+d}:{days[k].color[0]}/{days[k].volume/1e6:.1f}M"
                    for k in (-4, -3, -2, -1, 0) if k in days)
    print(f"  3 VOLUME   {M(ok3)}  option={opt3 or 'none held'}"
          f"{'  (blocked by the negative filter)' if neg3 else ''}")
    print(f"              {seq}")

    m = sp.distance_atr
    print(f"  4 SMA20    {M(_eval_cat4(s2, rt.sma20_max_atr_above, rt.sma20_min_atr_below))}"
          f"  multiplier {m:+.2f}   {cat4_why(m, rt.sma20_max_atr_above, rt.sma20_min_atr_below)}")
    print(f"              passes when 0 < m < {rt.sma20_max_atr_above:g} "
          f"or m < -{rt.sma20_min_atr_below:g}")

    print(f"  5 GAPS     {M(_eval_cat5(s2))}  {len(gp.gaps)} open gap(s) "
          f"in the {rt.history_trading_days}-session window")
    if gp.price_inside_gap:
        print("              price is INSIDE a gap -> passes")
    elif not gp.gaps:
        print("              none at all -> passes")
    else:
        print(f"              nearest is {gp.nearest_position} at "
              f"{gp.nearest_distance_atr:.2f} ATR -> "
              f"{'passes' if gp.nearest_position == 'above' else 'REJECTS'}")
        for g in sorted(gp.gaps, key=lambda x: x.distance)[:6]:
            edge = g.bottom if g.position == "above" else g.top
            print(f"                {g.position:<6} {g.bottom:9,.2f}-{g.top:<9,.2f} "
                  f"edge {edge:9,.2f}  {g.distance_atr:5.2f} ATR  "
                  f"size {g.size_pct:.2f}%  formed {g.date.date()}")

    print(f"  6 CCI      {M(_eval_cat6(s2))}  {s2.cci.cci_today:+.1f} "
          f"(yesterday {s2.cci.cci_yesterday:+.1f}), slope {s2.cci.slope_direction}")
    print(f"              passes when rising and -120 <= cci <= 130")

    pct = lp.pct_above_low
    shown = f"{pct:.2f}%" if isfinite(pct) else "unavailable"
    print(f"  7 52W LOW  {M(_eval_cat7(s2, rt.low52w_min_pct_above))}  {shown} above")
    print(f"              52w low {lp.low_52w:,.2f} from {lp.low_52w_sessions} sessions; "
          f"lowest low of last {lp.lookback_sessions} = {lp.recent_low:,.2f}")
    print(f"              passes when >= {rt.low52w_min_pct_above:g}%")

    print("-" * 72)
    if d.passed:
        print("  VERDICT    PASSED -> would be sent to Telegram")
    else:
        cats = ", ".join(str(c) for c in d.failed_categories)
        print(f"  VERDICT    REJECTED - failed "
              f"{'categories' if len(d.failed_categories) > 1 else 'category'} {cats}")
    print()
    return d.passed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--only", default="",
                    help="comma-separated tickers to report (default: all)")
    args = ap.parse_args()
    cfg = load_config(args.config)
    setup_logger(cfg.paths.log_dir)
    rt = cfg.runtime

    wanted = {t.strip().upper() for t in args.only.split(",") if t.strip()}
    results = run_stage2(run_stage1(cfg),
                         low52w_lookback_sessions=rt.low52w_lookback_sessions)
    if wanted:
        results = [s for s in results if s.ticker in wanted]
        missing = wanted - {s.ticker for s in results}
        if missing:
            print(f"\nnot reaching Stage 2 (dropped in Stage 1 - see the log "
                  f"for the reason): {', '.join(sorted(missing))}\n")

    print(f"\nthresholds in effect: SMA20 {rt.sma20_max_atr_above:g}/"
          f"{rt.sma20_min_atr_below:g}   52w low {rt.low52w_min_pct_above:g}% "
          f"over {rt.low52w_lookback_sessions} sessions\n")
    passed = sum(report(s, rt) for s in results)
    print("=" * 72)
    print(f"{len(results)} reported, {passed} would reach Telegram.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
