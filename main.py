"""MA20 Screener — entry point.

Stage B (current scope): runs Stage 1 only and prints a summary of the
final tickers ready for Stage 2. Stages 2-4 will be wired in here in
subsequent iterations.

Usage:
    python main.py               # full run using config.yaml
    python main.py --config X    # use a different config file
"""
from __future__ import annotations

import argparse
import time
from datetime import datetime

from ma20_screener.config_loader import load_config
from ma20_screener.logger import get_logger, setup_logger
from ma20_screener.stage1_data.orchestrator import run_stage1
from ma20_screener.stage2_checks.orchestrator import run_stage2


def main() -> int:
    parser = argparse.ArgumentParser(description="MA20 Stock Screener")
    parser.add_argument("--config", default="config.yaml", help="Path to YAML config file.")
    args = parser.parse_args()

    cfg = load_config(args.config)
    log_path = setup_logger(cfg.paths.log_dir)
    log = get_logger()

    log.info(f"MA20 Screener — run starting {datetime.now().isoformat(timespec='seconds')}")
    log.info(f"Log file: {log_path}")
    log.info(
        f"Config: workers={cfg.runtime.workers} sleep_ms={cfg.runtime.fetch_sleep_ms} "
        f"min_mcap=${cfg.runtime.min_market_cap_usd:,.0f} "
        f"history_days={cfg.runtime.history_trading_days} "
        f"test_tickers={cfg.runtime.test_tickers or '(none)'}"
    )

    t0 = time.time()
    stage1_results = run_stage1(cfg)
    stage2_results = run_stage2(stage1_results)
    elapsed = time.time() - t0

    log.info("-" * 60)
    log.info(
        f"STAGE 1 → STAGE 2 complete: stage1={len(stage1_results)} stage2={len(stage2_results)}"
    )
    for s in stage2_results[:10]:
        log.info(
            f"  {s.ticker} ({s.exchange}) "
            f"month={s.trend.monthly} week={s.trend.weekly} "
            f"candle={s.candle.color}({','.join(s.candle.formations) or '-'}) "
            f"SMA20={s.sma_position.position}/{s.sma_position.distance_label} "
            f"gaps(↑{s.gaps.has_gap_above}↓{s.gaps.has_gap_below}=){s.gaps.price_inside_gap} "
            f"CCI={s.cci.cci_today:.1f}({s.cci.slope_direction})"
        )
    if len(stage2_results) > 10:
        log.info(f"  … and {len(stage2_results) - 10} more")

    log.info(f"Total runtime: {elapsed:.1f} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
