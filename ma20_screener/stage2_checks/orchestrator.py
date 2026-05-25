"""Stage 2 orchestrator: run the six raw checks for every ticker."""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from ma20_screener.logger import (
    get_logger,
    log_stage_start,
    log_stage_summary,
    log_ticker_fail,
)
from ma20_screener.stage1_data.phase_c_indicators import TickerData
from ma20_screener.stage2_checks.check1_trend import TrendResult, run_check_1
from ma20_screener.stage2_checks.check2_candle import CandleResult, run_check_2
from ma20_screener.stage2_checks.check3_volume import VolumeResult, run_check_3
from ma20_screener.stage2_checks.check4_sma_position import (
    SMAPositionResult,
    run_check_4,
)
from ma20_screener.stage2_checks.check5_gaps import GapsResult, run_check_5
from ma20_screener.stage2_checks.check6_cci import CCIResult, run_check_6


@dataclass(frozen=True)
class Stage2Result:
    ticker: str
    exchange: str
    ohlcv: pd.DataFrame
    sma_20: float
    atr_14_pct: float
    trend: TrendResult
    candle: CandleResult
    volume: VolumeResult
    sma_position: SMAPositionResult
    gaps: GapsResult
    cci: CCIResult


def run_stage2(tickers: list[TickerData]) -> list[Stage2Result]:
    log = get_logger()
    log_stage_start("STAGE 2 (raw checks)")
    log.info(f"Stage 2: running 6 checks for {len(tickers)} tickers…")

    out: list[Stage2Result] = []
    for td in tickers:
        try:
            trend = run_check_1(td)
            candle = run_check_2(td)
            volume = run_check_3(td)
            sma_pos = run_check_4(td)
            gaps = run_check_5(td)
            cci = run_check_6(td)
        except Exception as e:
            log_ticker_fail(td.ticker, f"Stage 2 error: {e}")
            continue
        out.append(
            Stage2Result(
                ticker=td.ticker,
                exchange=td.exchange,
                ohlcv=td.ohlcv,
                sma_20=td.sma_20,
                atr_14_pct=td.atr_14_pct,
                trend=trend,
                candle=candle,
                volume=volume,
                sma_position=sma_pos,
                gaps=gaps,
                cci=cci,
            )
        )
    log_stage_summary("STAGE 2", entered=len(tickers), passed=len(out))
    return out
