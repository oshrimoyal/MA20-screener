"""Stage 4 part A — write the per-run CSV containing all stocks that
reached Stage 3 (both passed and rejected).

File name: MA20_Stocks_DD-MM-YYYY.csv (run date).
Columns are exactly the 20 listed in the document.
"""
from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

from ma20_screener.logger import get_logger
from ma20_screener.stage3_filter.filter import FilterDecision

CSV_COLUMNS = [
    "Ticker",
    "Status",
    "Reason",
    "Trend_Month",
    "Trend_Week",
    "Candle_Color",
    "Candle_Patterns",
    "Volume_Today",
    "Volume_Color_Today",
    "SMA20_Position",
    "SMA20_Distance",
    "SMA20_Role",
    "SMA20_Breakout",
    "Gap_Above",
    "Gap_Below",
    "Gap_Inside",
    "CCI_Value",
    "CCI_Slope",
    "CCI_Zone",
    "Chart_Link",
]


def _format_reason(d: FilterDecision) -> str:
    if d.passed:
        return "Passed all 6 categories"
    if d.failed_categories == [3] and d.cat3_negative_filter:
        return "Failed in category 3 (negative filter)"
    cats = ", ".join(str(c) for c in d.failed_categories)
    plural = "categories" if len(d.failed_categories) > 1 else "category"
    if d.cat3_negative_filter:
        return f"Failed in {plural} {cats} (category 3: negative filter)"
    return f"Failed in {plural} {cats}"


def _chart_link(exchange: str, ticker: str) -> str:
    # yfinance-style tickers use '-' where TradingView still uses '.'.
    tv_ticker = ticker.replace("-", ".")
    return f"https://www.tradingview.com/chart/?symbol={exchange}:{tv_ticker}"


def _row(d: FilterDecision) -> dict[str, object]:
    s = d.s2
    day0 = next(v for v in s.volume.days if v.day == 0)
    patterns = ", ".join(s.candle.formations)
    return {
        "Ticker": s.ticker,
        "Status": "Passed" if d.passed else "Rejected",
        "Reason": _format_reason(d),
        "Trend_Month": s.trend.monthly,
        "Trend_Week": s.trend.weekly,
        "Candle_Color": s.candle.color,
        "Candle_Patterns": patterns,
        "Volume_Today": day0.volume,
        "Volume_Color_Today": day0.color,
        "SMA20_Position": s.sma_position.position,
        "SMA20_Distance": s.sma_position.distance_label,
        "SMA20_Role": s.sma_position.role,
        "SMA20_Breakout": s.sma_position.breakout,
        "Gap_Above": "Yes" if s.gaps.has_gap_above else "No",
        "Gap_Below": "Yes" if s.gaps.has_gap_below else "No",
        "Gap_Inside": "Yes" if s.gaps.price_inside_gap else "No",
        "CCI_Value": f"{s.cci.cci_today:.2f}",
        "CCI_Slope": s.cci.slope_direction,
        "CCI_Zone": s.cci.zone,
        "Chart_Link": _chart_link(s.exchange, s.ticker),
    }


def write_csv(
    decisions: list[FilterDecision],
    csv_dir: Path,
    run_date: date,
) -> Path:
    """Write the per-run CSV and return its path."""
    csv_dir.mkdir(parents=True, exist_ok=True)
    filename = f"MA20_Stocks_{run_date.strftime('%d-%m-%Y')}.csv"
    path = csv_dir / filename

    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for d in decisions:
            writer.writerow(_row(d))

    get_logger().info(f"Stage 4: wrote CSV with {len(decisions)} rows -> {path}")
    return path
