"""Phase A: build the universe of NYSE + NASDAQ tickers and filter by market cap.

Ticker source: NASDAQ Trader Symbol Directory (free, official).
  - nasdaqlisted.txt -> NASDAQ-listed securities
  - otherlisted.txt  -> NYSE / NYSE MKT / NYSE Arca / BATS securities

Per the document the system covers NYSE + NASDAQ exchanges only and the
universe must include Common Stocks, ETFs and ADRs. NYSE Arca (P) and
NYSE American/AMEX (A) are subsidiaries of NYSE and host most U.S. ETFs;
they are therefore included under the "NYSE" label, which is also what
the document requires for the TradingView link in Stage 4.

After the universe is collected, the market cap of every ticker is fetched
via yfinance.fast_info and filtered to >= configured threshold (default
$1B).
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Optional

import requests
import yfinance as yf

from ma20_screener.logger import (
    get_logger,
    log_stage_start,
    log_stage_summary,
    log_ticker_fail,
)
from ma20_screener.utils.concurrency import parallel_map

NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/symdir/otherlisted.txt"

# Exchange codes that map to "NYSE" in otherlisted.txt:
#   N = NYSE, A = NYSE American (AMEX), P = NYSE Arca
# BATS (Z) is a separate venue and is excluded.
_NYSE_GROUP_CODES = {"N", "A", "P"}


@dataclass(frozen=True)
class Symbol:
    ticker: str            # yfinance-style ticker (dots replaced with dashes)
    exchange: str          # "NYSE" or "NASDAQ" (used for the TradingView link)


@dataclass(frozen=True)
class UniverseEntry:
    ticker: str
    exchange: str
    market_cap: float


def _fetch_text(url: str, timeout_s: int = 30) -> str:
    resp = requests.get(url, timeout=timeout_s)
    resp.raise_for_status()
    return resp.text


def _parse_nasdaq_listed(text: str) -> list[Symbol]:
    out: list[Symbol] = []
    lines = text.splitlines()
    if not lines:
        return out
    header = lines[0].split("|")
    # Expected: Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares
    try:
        i_sym = header.index("Symbol")
        i_test = header.index("Test Issue")
    except ValueError as e:
        raise RuntimeError(f"Unexpected nasdaqlisted.txt header: {header}") from e

    for line in lines[1:]:
        if not line or line.startswith("File Creation Time"):
            continue
        cols = line.split("|")
        if len(cols) <= max(i_sym, i_test):
            continue
        symbol = cols[i_sym].strip()
        if not symbol:
            continue
        if cols[i_test].strip() != "N":
            continue
        out.append(Symbol(ticker=symbol.replace(".", "-"), exchange="NASDAQ"))
    return out


def _parse_other_listed(text: str) -> list[Symbol]:
    out: list[Symbol] = []
    lines = text.splitlines()
    if not lines:
        return out
    header = lines[0].split("|")
    # Expected: ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol
    try:
        i_sym = header.index("ACT Symbol")
        i_exch = header.index("Exchange")
        i_test = header.index("Test Issue")
    except ValueError as e:
        raise RuntimeError(f"Unexpected otherlisted.txt header: {header}") from e

    for line in lines[1:]:
        if not line or line.startswith("File Creation Time"):
            continue
        cols = line.split("|")
        if len(cols) <= max(i_sym, i_exch, i_test):
            continue
        symbol = cols[i_sym].strip()
        exch = cols[i_exch].strip()
        test = cols[i_test].strip()
        if not symbol or test != "N":
            continue
        if exch not in _NYSE_GROUP_CODES:
            continue
        out.append(Symbol(ticker=symbol.replace(".", "-"), exchange="NYSE"))
    return out


def fetch_symbol_directory() -> list[Symbol]:
    """Download both NASDAQ Trader files and return the merged ticker list,
    test issues already filtered out."""
    nasdaq_text = _fetch_text(NASDAQ_LISTED_URL)
    other_text = _fetch_text(OTHER_LISTED_URL)
    nasdaq_syms = _parse_nasdaq_listed(nasdaq_text)
    other_syms = _parse_other_listed(other_text)
    # Deduplicate by ticker (NASDAQ wins on conflict, which is expected).
    seen: dict[str, Symbol] = {}
    for s in nasdaq_syms + other_syms:
        seen.setdefault(s.ticker, s)
    return list(seen.values())


def _get_market_cap(ticker: str) -> Optional[float]:
    """Return market cap in USD for `ticker`, or None if unavailable.

    yfinance.fast_info exposes the field as `marketCap` (camelCase); the
    snake_case alias `market_cap` exists but its underlying attribute
    chain triggers a full info fetch that is fragile in production. We
    therefore query `marketCap` directly.
    """
    try:
        fi = yf.Ticker(ticker).fast_info
        mc = fi.get("marketCap") if hasattr(fi, "get") else None
        if mc is None:
            return None
        mc = float(mc)
        if mc <= 0:
            return None
        return mc
    except Exception:
        return None


def run_phase_a(
    workers: int,
    fetch_sleep_ms: int,
    min_market_cap_usd: float,
    test_tickers: list[str] | None = None,
) -> list[UniverseEntry]:
    """Execute Phase A end-to-end:
       1. Fetch the NYSE+NASDAQ symbol directory (or use test_tickers).
       2. Look up market cap for each.
       3. Keep only tickers with market cap >= min_market_cap_usd.
    """
    log = get_logger()
    log_stage_start("STAGE 1 — Phase A (universe + market cap filter)")

    if test_tickers:
        log.info(f"Phase A: using TEST ticker override: {test_tickers}")
        symbols = [Symbol(ticker=t.replace(".", "-"), exchange="NASDAQ") for t in test_tickers]
    else:
        log.info("Phase A: downloading NASDAQ Trader symbol directory…")
        symbols = fetch_symbol_directory()
        log.info(f"Phase A: {len(symbols)} symbols fetched from NASDAQ Trader (after test-issue filter).")

    def _lookup(sym: Symbol) -> tuple[Symbol, Optional[float]]:
        return sym, _get_market_cap(sym.ticker)

    log.info(
        f"Phase A: looking up market cap for {len(symbols)} tickers "
        f"(workers={workers}, sleep_ms={fetch_sleep_ms})…"
    )
    pairs = parallel_map(_lookup, symbols, workers=workers, sleep_ms=fetch_sleep_ms)

    universe: list[UniverseEntry] = []
    for sym, mc in pairs:
        if mc is None:
            log_ticker_fail(sym.ticker, "market cap unavailable")
            continue
        if mc < min_market_cap_usd:
            log_ticker_fail(sym.ticker, f"market cap ${mc:,.0f} below ${min_market_cap_usd:,.0f}")
            continue
        universe.append(UniverseEntry(ticker=sym.ticker, exchange=sym.exchange, market_cap=mc))

    log_stage_summary("STAGE 1 — Phase A", entered=len(symbols), passed=len(universe))
    return universe
