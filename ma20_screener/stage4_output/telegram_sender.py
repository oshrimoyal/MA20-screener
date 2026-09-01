"""Stage 4 part B — send passing candidates to Telegram.

Format and behaviour follow the document exactly:
  * Header message:    📊 MA20 Stocks | DD/MM/YYYY | X candidates
  * Subsequent messages: groups of 5 stocks each.
  * Zero candidates: a single message "MA20 Stocks | DD/MM/YYYY | 0 candidates today".
  * On a sendMessage failure: log it, wait 5 seconds, retry once. If the
    second attempt also fails: log and continue. The CSV is independent.
"""
from __future__ import annotations

import time
from datetime import date
from math import isfinite
from typing import Iterable

import requests

from ma20_screener.logger import get_logger
from ma20_screener.stage2_checks.check5_gaps import POS_ABOVE, POS_BELOW, POS_INSIDE
from ma20_screener.stage3_filter.filter import FilterDecision

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
GROUP_SIZE = 5
RETRY_DELAY_S = 5

_VOLUME_TEXTS = {
    1: "green, higher than yesterday",
    2: "green, equal to yesterday",
    3: "green after 2 declining red days",
    4: "green after 4+ red days",
}


def _gap_text(d: FilterDecision) -> str:
    g = d.s2.gaps
    if g.price_inside_gap:
        return "price inside a gap"
    if g.has_gap_above and g.has_gap_below:
        return "open above and below"
    if g.has_gap_above:
        return "open above"
    if g.has_gap_below:
        return "open below"
    return "no open gaps"


def _candle_text(d: FilterDecision) -> str:
    color = d.s2.candle.color
    if d.s2.candle.formations:
        return f"{color} + {', '.join(d.s2.candle.formations).lower()}"
    return color.capitalize()


def _sma_text(d: FilterDecision) -> str:
    p = d.s2.sma_position
    base = f"{p.position.lower()}, {p.distance_label.lower()}"
    if p.position.lower() == "on the moving average":
        # The position label already conveys distance; drop the duplicate.
        base = p.position.lower()
    extras: list[str] = []
    if p.role == "Served as support":
        extras.append("support")
    if p.breakout != "No breakout":
        extras.append("breakout")
    if extras:
        base = base + " " + " ".join(f"({e})" for e in extras)
    return base


def _volume_text(d: FilterDecision) -> str:
    return _VOLUME_TEXTS.get(d.volume_positive_option, "green")


def _low_text(d: FilterDecision) -> str:
    """Every other line in the block maps to one category; Category 7
    gets one too. A ticker only reaches Telegram once it cleared the
    threshold, so this is context for sizing the trade, not a verdict."""
    lp = d.s2.low_proximity
    if not isfinite(lp.pct_above_low):
        return "52w low unavailable"
    return f"{lp.pct_above_low:.0f}% above the 52-week low ({lp.low_52w:,.2f})"


def _chart_link(exchange: str, ticker: str) -> str:
    tv_ticker = ticker.replace("-", ".")
    return f"https://www.tradingview.com/chart/?symbol={exchange}:{tv_ticker}"


def _format_stock_block(d: FilterDecision) -> str:
    s = d.s2
    return (
        "━━━━━━━━━━━━━━━━━\n\n"
        f"🎯 ${s.ticker}\n\n"
        f"📈 Month: {s.trend.monthly} | Week: {s.trend.weekly}\n\n"
        f"🕯 Candle: {_candle_text(d)}\n\n"
        f"📊 Volume: {_volume_text(d)}\n\n"
        f"📏 SMA20: {_sma_text(d)}\n\n"
        f"⚡ Gap: {_gap_text(d)}\n\n"
        f"🌡 CCI: {s.cci.cci_today:.0f}, slope {s.cci.slope_direction.lower()}\n\n"
        f"🛡 Low: {_low_text(d)}\n\n"
        f"🔗 {_chart_link(s.exchange, s.ticker)}"
    )


def _format_group(group: Iterable[FilterDecision]) -> str:
    blocks = [_format_stock_block(d) for d in group]
    return "\n\n".join(blocks)


def _send_one(token: str, chat_id: str, text: str) -> bool:
    """Send a single message. One retry after RETRY_DELAY_S on failure.
    Returns True iff one of the two attempts succeeded."""
    log = get_logger()
    url = TELEGRAM_API.format(token=token)
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    for attempt in (1, 2):
        try:
            resp = requests.post(url, json=payload, timeout=20)
            if resp.status_code == 200 and resp.json().get("ok") is True:
                return True
            log.warning(
                f"Telegram sendMessage failed (attempt {attempt}): "
                f"HTTP {resp.status_code} body={resp.text[:200]}"
            )
        except Exception as e:
            log.warning(f"Telegram sendMessage error (attempt {attempt}): {e}")
        if attempt == 1:
            time.sleep(RETRY_DELAY_S)
    log.error("Telegram sendMessage failed twice; continuing without it.")
    return False


def send_candidates(
    passed: list[FilterDecision],
    token: str,
    chat_id: str,
    run_date: date,
) -> None:
    log = get_logger()
    date_str = run_date.strftime("%d/%m/%Y")

    if not passed:
        empty_text = f"MA20 Stocks | {date_str} | 0 candidates today"
        log.info("Stage 4: 0 candidates — sending empty-day message.")
        _send_one(token, chat_id, empty_text)
        return

    header = f"📊 MA20 Stocks | {date_str} | {len(passed)} candidates"
    log.info(f"Stage 4: sending Telegram header for {len(passed)} candidates.")
    _send_one(token, chat_id, header)

    for i in range(0, len(passed), GROUP_SIZE):
        group = passed[i : i + GROUP_SIZE]
        log.info(
            f"Stage 4: sending Telegram group {i // GROUP_SIZE + 1} "
            f"({len(group)} stock{'s' if len(group) != 1 else ''})."
        )
        _send_one(token, chat_id, _format_group(group))
