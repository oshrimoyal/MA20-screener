"""MA20 Screener — entry point.

Orchestrates the four stages:
    Stage 1 (data infrastructure) -> Stage 2 (raw checks)
    -> Stage 3 (six-category filter) -> Stage 4 (CSV + Telegram).

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
from ma20_screener.stage3_filter.filter import run_stage3
from ma20_screener.stage4_output.csv_writer import write_csv
from ma20_screener.stage4_output.telegram_sender import send_candidates
from ma20_screener.utils.market_time import get_last_closed_trading_day


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
        f"Config: history_workers={cfg.runtime.history_workers} "
        f"history_sleep_ms={cfg.runtime.history_sleep_ms} "
        f"history_rate_per_min={cfg.runtime.history_rate_per_min:,.0f} "
        f"history_retries={cfg.runtime.history_retries} "
        f"history_retry_delay_s={cfg.runtime.history_retry_delay_s} "
        f"min_mcap=${cfg.runtime.min_market_cap_usd:,.0f} "
        f"min_volume_ma={cfg.runtime.min_volume_ma:,.0f} "
        f"history_days={cfg.runtime.history_trading_days} "
        f"history_long_days={cfg.runtime.history_long_trading_days} "
        f"low52w_lookback={cfg.runtime.low52w_lookback_sessions} "
        f"low52w_min_pct_above={cfg.runtime.low52w_min_pct_above:g} "
        f"fmp_api_key={'<set>' if cfg.runtime.fmp_api_key else '<missing>'} "
        f"test_tickers={cfg.runtime.test_tickers or '(none)'}"
    )

    t0 = time.time()
    stage1_results = run_stage1(cfg)
    stage2_results = run_stage2(
        stage1_results,
        low52w_lookback_sessions=cfg.runtime.low52w_lookback_sessions,
    )
    decisions = run_stage3(stage2_results)

    # The "run date" used for the CSV file name and Telegram header is the
    # date of the last closed trading day (i.e. the candle we analysed).
    run_date = get_last_closed_trading_day().date()
    csv_path = write_csv(decisions, cfg.paths.csv_dir, run_date)
    passed = [d for d in decisions if d.passed]
    send_candidates(passed, cfg.telegram.token, cfg.telegram.chat_id, run_date)

    elapsed = time.time() - t0
    log.info("-" * 60)
    log.info(
        f"DONE: stage1={len(stage1_results)} stage2={len(stage2_results)} "
        f"stage3_passed={len(passed)} csv={csv_path}"
    )
    log.info(f"Total runtime: {elapsed:.1f} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
