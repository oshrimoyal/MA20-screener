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

    universe = run_phase_a(
        workers=cfg.runtime.workers,
        fetch_sleep_ms=cfg.runtime.fetch_sleep_ms,
        min_market_cap_usd=cfg.runtime.min_market_cap_usd,
        test_tickers=cfg.runtime.test_tickers or None,
    )

    histories = run_phase_b(
        universe=universe,
        history_trading_days=cfg.runtime.history_trading_days,
        workers=cfg.runtime.workers,
        fetch_sleep_ms=cfg.runtime.fetch_sleep_ms,
    )

    final = run_phase_c(histories)
    log.info(f"STAGE 1 final: {len(final)} tickers ready for Stage 2.")
    return final
