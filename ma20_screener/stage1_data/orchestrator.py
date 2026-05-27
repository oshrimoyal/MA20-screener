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

    # Phase A: build the universe and enrich with SEC CIK numbers.
    # No marketCap fetch here — that has moved to Phase B.
    universe = run_phase_a(
        sec_user_agent=cfg.runtime.sec_user_agent,
        test_tickers=cfg.runtime.test_tickers or None,
    )

    # Phase B: per-ticker OHLCV from Stooq + bulk shares from SEC EDGAR
    # XBRL Frames + computed marketCap + >=$1B filter.
    histories = run_phase_b(
        universe=universe,
        history_trading_days=cfg.runtime.history_trading_days,
        workers=cfg.runtime.history_workers,
        fetch_sleep_ms=cfg.runtime.history_sleep_ms,
        min_market_cap_usd=cfg.runtime.min_market_cap_usd,
        sec_user_agent=cfg.runtime.sec_user_agent,
        stooq_user_agent=cfg.runtime.stooq_user_agent,
        retries=cfg.runtime.history_retries,
        retry_delay_s=cfg.runtime.history_retry_delay_s,
    )

    final = run_phase_c(histories)
    log.info(f"STAGE 1 final: {len(final)} tickers ready for Stage 2.")
    return final
