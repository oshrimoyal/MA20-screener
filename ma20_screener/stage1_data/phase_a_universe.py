"""Phase A: build the universe from the S&P 500 and filter by market cap.

Ticker source: the current S&P 500 constituent list, scraped from the
canonical Wikipedia page
    https://en.wikipedia.org/wiki/List_of_S%26P_500_companies
which is a free, no-key, always-up-to-date source maintained by the
community and updated when constituents change.

Why S&P 500 instead of the full NYSE + NASDAQ universe?
  * Operator decision (see project history): the previous broad-universe
    pull triggered a high rate of "market cap unavailable" yfinance
    failures for the long tail of small / illiquid tickers. Restricting
    the universe to S&P 500 names — all of which are large-caps with
    reliable yfinance metadata — eliminates that failure mode while
    still covering the names the operator cares about.
  * The market-cap filter (default $1B) is kept exactly as before. In
    practice every S&P 500 member passes this threshold; the filter is
    retained so the system still rejects any name whose market cap
    turns out to be missing or below threshold on a given run.

After the universe is collected, the market cap AND the listing
exchange of every ticker are read from yfinance.fast_info in a single
call. The exchange code is mapped to the "NASDAQ" / "NYSE" label used
by Stage 4 for the TradingView link.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Optional

import pandas as pd
import requests
import yfinance as yf

from ma20_screener.logger import (
    get_logger,
    log_stage_start,
    log_stage_summary,
    log_ticker_fail,
)
from ma20_screener.utils.concurrency import parallel_map

SP500_WIKIPEDIA_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
_HTTP_HEADERS = {"User-Agent": "MA20-Screener/1.0 (+https://github.com/oshrimoyal/MA20-screener)"}

# yfinance fast_info "exchange" codes -> Stage 4 link label.
# (NMS/NCM/NGM/NGS = NASDAQ tiers; NYQ = NYSE; ASE/PCX = NYSE American / NYSE
# Arca, both NYSE Group venues and labelled "NYSE" for the TradingView link.)
_EXCH_CODE_TO_LABEL = {
    "NMS": "NASDAQ",
    "NCM": "NASDAQ",
    "NGM": "NASDAQ",
    "NGS": "NASDAQ",
    "NAS": "NASDAQ",
    "NASDAQ": "NASDAQ",
    "NYQ": "NYSE",
    "NYS": "NYSE",
    "NYSE": "NYSE",
    "ASE": "NYSE",
    "PCX": "NYSE",
    "ARCA": "NYSE",
}


@dataclass(frozen=True)
class Symbol:
    ticker: str            # yfinance-style ticker (dots replaced with dashes)


@dataclass(frozen=True)
class UniverseEntry:
    ticker: str
    exchange: str
    market_cap: float


def fetch_sp500_tickers() -> list[Symbol]:
    """Download the current S&P 500 constituent list from Wikipedia and
    return it as a list of Symbol records. Tickers are normalised to
    yfinance form (e.g. "BRK.B" -> "BRK-B")."""
    log = get_logger()
    resp = requests.get(SP500_WIKIPEDIA_URL, headers=_HTTP_HEADERS, timeout=30)
    resp.raise_for_status()

    # pandas.read_html requires lxml or html5lib; lxml is in requirements.txt.
    tables = pd.read_html(io.StringIO(resp.text))
    if not tables:
        raise RuntimeError("Wikipedia S&P 500 page returned no parseable tables.")

    # The first table on the page is the constituent list; find the
    # column that holds the ticker symbol (the column has been called
    # "Symbol" historically, occasionally "Ticker symbol").
    constituents = tables[0]
    cols = list(constituents.columns)
    sym_col: Optional[str] = None
    for c in cols:
        name = str(c).strip().lower()
        if name in ("symbol", "ticker", "ticker symbol"):
            sym_col = c
            break
    if sym_col is None:
        raise RuntimeError(
            f"S&P 500 Wikipedia table has unexpected columns: {cols}"
        )

    symbols: list[Symbol] = []
    seen: set[str] = set()
    for raw in constituents[sym_col].astype(str):
        t = raw.strip().upper().replace(".", "-")
        if not t or t in seen:
            continue
        seen.add(t)
        symbols.append(Symbol(ticker=t))
    log.info(f"Phase A: parsed {len(symbols)} S&P 500 tickers from Wikipedia.")
    return symbols


def _get_market_cap_and_exchange(ticker: str) -> tuple[Optional[float], Optional[str]]:
    """Return (market_cap_usd, exchange_code) for `ticker` via
    yfinance.fast_info (single HTTP round-trip). Returns (None, code) if
    market cap is missing but exchange is known, or (None, None) on
    any error.
    """
    try:
        fi = yf.Ticker(ticker).fast_info
        mc = fi.get("marketCap") if hasattr(fi, "get") else None
        ex = fi.get("exchange") if hasattr(fi, "get") else None
        ex_str = str(ex).strip() if ex is not None else None
        if mc is None:
            return None, ex_str
        mc_f = float(mc)
        if mc_f <= 0:
            return None, ex_str
        return mc_f, ex_str
    except Exception:
        return None, None


def _label_for_exchange(code: Optional[str]) -> str:
    """Map an yfinance exchange code to "NASDAQ" or "NYSE" for the
    TradingView link in Stage 4. Unknown codes fall back to "NYSE",
    which is the larger of the two venues by S&P 500 membership and a
    safer default than the opposite."""
    if not code:
        return "NYSE"
    return _EXCH_CODE_TO_LABEL.get(code.upper(), "NYSE")


def run_phase_a(
    workers: int,
    fetch_sleep_ms: int,
    min_market_cap_usd: float,
    test_tickers: list[str] | None = None,
) -> list[UniverseEntry]:
    """Execute Phase A end-to-end:
       1. Fetch the S&P 500 constituent list (or use test_tickers).
       2. Look up market cap + listing exchange for each via yfinance.
       3. Keep only tickers with market cap >= min_market_cap_usd.
    """
    log = get_logger()
    log_stage_start("STAGE 1 — Phase A (S&P 500 + market cap filter)")

    if test_tickers:
        log.info(f"Phase A: using TEST ticker override: {test_tickers}")
        symbols = [Symbol(ticker=t.replace(".", "-")) for t in test_tickers]
    else:
        log.info("Phase A: fetching S&P 500 constituent list from Wikipedia…")
        symbols = fetch_sp500_tickers()

    def _lookup(sym: Symbol) -> tuple[Symbol, Optional[float], Optional[str]]:
        mc, ex = _get_market_cap_and_exchange(sym.ticker)
        return sym, mc, ex

    log.info(
        f"Phase A: looking up market cap + exchange for {len(symbols)} tickers "
        f"(workers={workers}, sleep_ms={fetch_sleep_ms})…"
    )
    triples = parallel_map(_lookup, symbols, workers=workers, sleep_ms=fetch_sleep_ms)

    universe: list[UniverseEntry] = []
    for sym, mc, ex_code in triples:
        if mc is None:
            log_ticker_fail(sym.ticker, "market cap unavailable")
            continue
        if mc < min_market_cap_usd:
            log_ticker_fail(
                sym.ticker,
                f"market cap ${mc:,.0f} below ${min_market_cap_usd:,.0f}",
            )
            continue
        universe.append(
            UniverseEntry(
                ticker=sym.ticker,
                exchange=_label_for_exchange(ex_code),
                market_cap=mc,
            )
        )

    log_stage_summary("STAGE 1 — Phase A", entered=len(symbols), passed=len(universe))
    return universe
