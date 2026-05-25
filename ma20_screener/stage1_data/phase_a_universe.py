"""Phase A: build the universe from two complementary sources and filter
by market cap.

Universe sources:
  1. The current S&P 500 constituent list, from the canonical
     Wikipedia page:
         https://en.wikipedia.org/wiki/List_of_S%26P_500_companies
     The S&P 500 captures most of the large-cap NASDAQ-listed names
     (AAPL, MSFT, NVDA, GOOGL, AMZN, etc.) plus the largest NYSE-listed
     blue chips.
  2. All NYSE-proper common stocks, from NASDAQ Trader's free symbol
     directory:
         ftp.nasdaqtrader.com/SymbolDirectory/otherlisted.txt
     Filters applied while parsing:
         Exchange     == "N"   (NYSE proper only — excludes NYSE
                                American "A", NYSE Arca "P", Cboe BZX
                                "Z" per operator decision)
         Test Issue   == "N"   (drop test issues)
         ETF          == "N"   (operator asked for stocks only)
  Both sources additionally drop any symbol containing the "$"
  character. In NASDAQ Trader / yfinance notation "$" inside a symbol
  marks a preferred-share series (e.g. "ABR$D", "AGM$E"); these are
  not common stocks and are out of scope. Removing them at parse
  time also saves the wasted yfinance round-trips that those symbols
  would otherwise consume in the marketCap lookup.

  The two lists are merged and deduplicated by ticker; an entry that
  appears in both sources is included exactly once.

Why these two together?
  The S&P 500 covers the highly-liquid NASDAQ large caps (which are NOT
  in otherlisted.txt) and the otherlisted.txt covers every other
  NYSE-proper stock that may not be in the index — together they form
  the universe of interest for this screener.

After the universe is collected, the market cap AND the listing
exchange of every ticker are read from yfinance.fast_info in a single
call. Tickers with market cap below the configured threshold (default
$1B) are dropped. The exchange code is mapped to "NASDAQ" / "NYSE" for
the TradingView link used in Stage 4.
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
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/symdir/otherlisted.txt"
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


# ----- Source 1: S&P 500 from Wikipedia ------------------------------------

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
    # column that holds the ticker symbol (historically "Symbol",
    # occasionally "Ticker" / "Ticker symbol").
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
    skipped_dollar = 0
    for raw in constituents[sym_col].astype(str):
        t = raw.strip().upper().replace(".", "-")
        if not t or t in seen:
            continue
        # Operator decision: drop any ticker that contains "$".
        # In NASDAQ Trader / yfinance notation, "$" inside a symbol
        # marks a preferred-share series (e.g. ABR$D, AGM$E) — these
        # are not common stocks and are out of scope for this screener.
        # This branch is defensive for Wikipedia (the S&P 500 list
        # does not contain preferred shares in practice).
        if "$" in t:
            skipped_dollar += 1
            continue
        seen.add(t)
        symbols.append(Symbol(ticker=t))
    if skipped_dollar:
        log.info(
            f"Phase A: dropped {skipped_dollar} '$'-bearing tickers from "
            f"S&P 500 list (preferred shares)."
        )
    log.info(f"Phase A: parsed {len(symbols)} S&P 500 tickers from Wikipedia.")
    return symbols


# ----- Source 2: NYSE-proper stocks from NASDAQ Trader ---------------------

def fetch_nyse_stocks() -> list[Symbol]:
    """Download NYSE-proper common stocks from NASDAQ Trader's
    otherlisted.txt. Returns yfinance-style tickers.

    Filters: Exchange="N" (NYSE proper), Test Issue="N", ETF="N".
    """
    log = get_logger()
    resp = requests.get(OTHER_LISTED_URL, headers=_HTTP_HEADERS, timeout=30)
    resp.raise_for_status()
    text = resp.text

    lines = text.splitlines()
    if not lines:
        raise RuntimeError("otherlisted.txt is empty.")
    header = lines[0].split("|")
    # Expected columns:
    # ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol
    try:
        i_sym = header.index("ACT Symbol")
        i_exch = header.index("Exchange")
        i_etf = header.index("ETF")
        i_test = header.index("Test Issue")
    except ValueError as e:
        raise RuntimeError(f"Unexpected otherlisted.txt header: {header}") from e

    out: list[Symbol] = []
    seen: set[str] = set()
    skipped_dollar = 0
    max_idx = max(i_sym, i_exch, i_etf, i_test)
    for line in lines[1:]:
        if not line or line.startswith("File Creation Time"):
            continue
        cols = line.split("|")
        if len(cols) <= max_idx:
            continue
        symbol = cols[i_sym].strip()
        exchange = cols[i_exch].strip()
        etf = cols[i_etf].strip()
        test = cols[i_test].strip()
        if not symbol:
            continue
        if test != "N":
            continue
        if exchange != "N":   # strict NYSE proper only
            continue
        if etf != "N":        # exclude ETFs
            continue
        # Operator decision: drop any ticker that contains "$".
        # NASDAQ Trader uses "$" as the preferred-series separator
        # (e.g. "ABR$D", "AGM$E") — these are preferred shares, not
        # common stock, and out of scope for this screener. Removing
        # them here saves ~475 wasted yfinance round-trips per run
        # observed previously and keeps the log clean.
        if "$" in symbol:
            skipped_dollar += 1
            continue
        ticker = symbol.replace(".", "-").upper()
        if ticker in seen:
            continue
        seen.add(ticker)
        out.append(Symbol(ticker=ticker))
    if skipped_dollar:
        log.info(
            f"Phase A: dropped {skipped_dollar} '$'-bearing tickers from "
            f"NYSE list (preferred-share series)."
        )
    log.info(
        f"Phase A: parsed {len(out)} NYSE-proper stock tickers "
        f"from NASDAQ Trader (Exchange=N, Test=N, ETF=N, no '$')."
    )
    return out


# ----- yfinance lookup -----------------------------------------------------

def _get_market_cap_and_exchange(ticker: str) -> tuple[Optional[float], Optional[str]]:
    """Return (market_cap_usd, exchange_code) for `ticker` via
    yfinance.fast_info (single HTTP round-trip). Returns (None, code) if
    market cap is missing but exchange is known, or (None, None) on any
    error."""
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
    TradingView link in Stage 4. Unknown codes fall back to "NYSE"
    (the larger of the two venues by membership in this universe)."""
    if not code:
        return "NYSE"
    return _EXCH_CODE_TO_LABEL.get(code.upper(), "NYSE")


# ----- Phase A entry point -------------------------------------------------

def fetch_universe() -> list[Symbol]:
    """Fetch S&P 500 + NYSE-proper universe, merged and deduplicated.
    Raises on any source failure (so the operator sees the problem
    rather than running on a partial universe)."""
    log = get_logger()
    sp500 = fetch_sp500_tickers()
    nyse = fetch_nyse_stocks()
    merged: dict[str, Symbol] = {}
    for s in sp500:
        merged.setdefault(s.ticker, s)
    overlap = 0
    for s in nyse:
        if s.ticker in merged:
            overlap += 1
        else:
            merged[s.ticker] = s
    out = list(merged.values())
    log.info(
        f"Phase A: merged universe — S&P 500 ({len(sp500)}) + NYSE ({len(nyse)}); "
        f"overlap={overlap}; final unique tickers = {len(out)}."
    )
    return out


def run_phase_a(
    workers: int,
    fetch_sleep_ms: int,
    min_market_cap_usd: float,
    test_tickers: list[str] | None = None,
) -> list[UniverseEntry]:
    """Execute Phase A end-to-end:
       1. Build the universe = S&P 500 ∪ NYSE-proper stocks (or use
          test_tickers).
       2. Look up market cap + listing exchange for each via yfinance.
       3. Keep only tickers with market cap >= min_market_cap_usd.
    """
    log = get_logger()
    log_stage_start("STAGE 1 — Phase A (S&P 500 + NYSE + market cap filter)")

    if test_tickers:
        log.info(f"Phase A: using TEST ticker override: {test_tickers}")
        symbols = [Symbol(ticker=t.replace(".", "-").upper()) for t in test_tickers]
    else:
        symbols = fetch_universe()

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
