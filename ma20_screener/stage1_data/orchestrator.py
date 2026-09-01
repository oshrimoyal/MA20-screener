"""Stage 1 orchestrator: Phase A -> Phase B -> Phase C. Returns the list of
tickers ready for Stage 2 (Phase D in the document)."""
from __future__ import annotations

from ma20_screener.config_loader import AppConfig
from ma20_screener.logger import get_logger, log_stage_start
from ma20_screener.stage1_data.phase_a_universe import run_phase_a
from ma20_screener.stage1_data.phase_b_history import run_phase_b
from ma20_screener.stage1_data.phase_c_indicators import TickerData, run_phase_c


def run_stage1(cfg: AppConfig) -> list[TickerData]:
    log = get_logger()
    log_stage_start("STAGE 1")

    # Phase A: build the universe via FMP /stable/company-screener
    # (NASDAQ + NYSE common stocks >= $1B). The screener response
    # also includes marketCap, which is forwarded to Phase B via the
    # UniverseEntry — so Phase B does not need a separate /stable/quote
    # call per ticker.
    universe = run_phase_a(
        fmp_api_key=cfg.runtime.fmp_api_key,
        test_tickers=cfg.runtime.test_tickers or None,
    )

    # Phase B: per-ticker OHLCV from FMP /stable/historical-price-eod/full,
    # then the liquidity gate (marketCap >= $1B AND 14-day avg volume > 1M).
    histories = run_phase_b(
        universe=universe,
        history_trading_days=cfg.runtime.history_trading_days,
        long_trading_days=cfg.runtime.history_long_trading_days,
        workers=cfg.runtime.history_workers,
        fetch_sleep_ms=cfg.runtime.history_sleep_ms,
        min_market_cap_usd=cfg.runtime.min_market_cap_usd,
        min_volume_ma=cfg.runtime.min_volume_ma,
        fmp_api_key=cfg.runtime.fmp_api_key,
        retries=cfg.runtime.history_retries,
        retry_delay_s=cfg.runtime.history_retry_delay_s,
        rate_per_min=cfg.runtime.history_rate_per_min,
    )

    final = run_phase_c(histories)
    log.info(f"STAGE 1 final: {len(final)} tickers ready for Stage 2.")
    return final
