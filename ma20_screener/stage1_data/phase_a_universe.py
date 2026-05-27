"""Phase A: build the universe from five complementary sources, enrich
each ticker with its authoritative listing exchange, and pass on to
Phase B.

Universe sources (free, anonymous HTTP — no API key, no signup):

  1. S&P 500 constituent list — Wikipedia:
         https://en.wikipedia.org/wiki/List_of_S%26P_500_companies
  2. NYSE-proper common stocks — NASDAQ Trader otherlisted.txt with
     Exchange="N".
  3. NASDAQ Global Select Market common stocks — NASDAQ Trader
     nasdaqlisted.txt with Market Category="Q".
  4. NYSE American (formerly AMEX) common stocks — NASDAQ Trader
     otherlisted.txt with Exchange="A".
  5. NYSE Arca common stocks — NASDAQ Trader otherlisted.txt with
     Exchange="P" (Arca hosts mostly ETFs; the ETF=N filter keeps only
     the small number of common stocks listed on Arca).

All sources additionally drop three classes of non-common-stock symbols
at parse time:
  a) Symbols containing "$" at any position (preferred-share series).
  b) Symbols ending in a warrant / unit / right suffix
     (-W, -WS, -WT, -WI, -WD, -U, -UN, -R, -RT, -RW).
  c) Entries whose Security Name clearly identifies a preferred /
     debenture / subordinated-note / trust-preferred / convertible-
     preferred instrument (see `_NON_COMMON_STOCK_NAME_PATTERNS`).

After the universe is merged and deduped, every ticker is enriched
with the authoritative SEC EDGAR exchange label from
`company_tickers_exchange.json`. This is a single static-file download
and adds no per-ticker round-trips. Tickers SEC does not list (e.g.
some ADRs filing 20-F) keep their source-known exchange label or fall
back to "NYSE"; they are NOT dropped here — Finnhub decides per
ticker in Phase B.

**The marketCap >= $1B filter lives in Phase B**, where it is read
directly from Finnhub's `/stock/profile2.marketCapitalization`.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Optional

import pandas as pd
import requests

from ma20_screener.logger import (
    get_logger,
    log_stage_start,
    log_stage_summary,
)
from ma20_screener.utils import sec_edgar

SP500_WIKIPEDIA_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/symdir/otherlisted.txt"
NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt"
_HTTP_HEADERS = {"User-Agent": "MA20-Screener/1.0 (+https://github.com/oshrimoyal/MA20-screener)"}

# Suffix list — preferred-share-series / warrant / unit / right markers.
_NON_COMMON_STOCK_SUFFIXES = (
    "-W", "-WS", "-WT", "-WI", "-WD",     # warrants
    "-U", "-UN",                           # units
    "-R", "-RT", "-RW",                    # rights
)


def _looks_like_non_common_stock(ticker: str) -> bool:
    """True if `ticker` ends with a warrant/unit/right suffix."""
    return any(ticker.endswith(sfx) for sfx in _NON_COMMON_STOCK_SUFFIXES)


# Security-Name substrings that mark a non-common-stock instrument.
_NON_COMMON_STOCK_NAME_PATTERNS = (
    "preferred stock",
    "preferred share",
    "preferred limited",
    "% preferred",
    "subordinated",
    "debenture",
    "senior note",
    "trust preferred",
    "convertible preferred",
)


def _looks_like_non_common_stock_by_name(security_name: str) -> bool:
    """True if the Security Name clearly identifies a non-common-stock
    instrument (preferred / debenture / note / trust preferred)."""
    if not security_name:
        return False
    lower = security_name.lower()
    return any(pat in lower for pat in _NON_COMMON_STOCK_NAME_PATTERNS)


@dataclass(frozen=True)
class Symbol:
    """A ticker as discovered by one of the source parsers.

    `source_exchange` records what we know about the listing exchange
    from the source itself ("NASDAQ" / "NYSE" / None for sources like
    the S&P 500 Wikipedia table that don't include the exchange).
    """
    ticker: str
    source_exchange: Optional[str] = None


@dataclass(frozen=True)
class UniverseEntry:
    """A ticker that completed Phase A: name and exchange label for
    the Stage-4 TradingView link. The exchange label is one of
    "NASDAQ" or "NYSE"."""
    ticker: str
    exchange: str


# ----- Source 1: S&P 500 from Wikipedia ------------------------------------

def fetch_sp500_tickers() -> list[Symbol]:
    """Download the current S&P 500 constituent list from Wikipedia and
    return it as Symbol records. Tickers are normalised to internal
    form (e.g. "BRK.B" -> "BRK-B"). Source exchange is None (we
    determine it from SEC EDGAR later)."""
    log = get_logger()
    resp = requests.get(SP500_WIKIPEDIA_URL, headers=_HTTP_HEADERS, timeout=30)
    resp.raise_for_status()

    tables = pd.read_html(io.StringIO(resp.text))
    if not tables:
        raise RuntimeError("Wikipedia S&P 500 page returned no parseable tables.")
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
        if "$" in t:
            skipped_dollar += 1
            continue
        seen.add(t)
        symbols.append(Symbol(ticker=t, source_exchange=None))
    if skipped_dollar:
        log.info(
            f"Phase A: dropped {skipped_dollar} '$'-bearing tickers from "
            f"S&P 500 list (preferred shares)."
        )
    log.info(f"Phase A: parsed {len(symbols)} S&P 500 tickers from Wikipedia.")
    return symbols


# ----- Sources 2, 4, 5: NYSE-proper / NYSE American / NYSE Arca ------------

def _parse_otherlisted(
    text: str,
    exchange_code: str,
    label: str,
    source_exchange: str,
) -> list[Symbol]:
    """Parse otherlisted.txt for one specific Exchange code and apply
    the standard filters. `source_exchange` is the label attached to
    each emitted Symbol (e.g. "NYSE") and `label` is only for the log
    line (e.g. "NYSE-proper")."""
    log = get_logger()
    lines = text.splitlines()
    if not lines:
        raise RuntimeError("otherlisted.txt is empty.")
    header = lines[0].split("|")
    try:
        i_sym = header.index("ACT Symbol")
        i_name = header.index("Security Name")
        i_exch = header.index("Exchange")
        i_etf = header.index("ETF")
        i_test = header.index("Test Issue")
    except ValueError as e:
        raise RuntimeError(f"Unexpected otherlisted.txt header: {header}") from e

    out: list[Symbol] = []
    seen: set[str] = set()
    skipped_dollar = 0
    skipped_suffix = 0
    skipped_name = 0
    max_idx = max(i_sym, i_name, i_exch, i_etf, i_test)
    for line in lines[1:]:
        if not line or line.startswith("File Creation Time"):
            continue
        cols = line.split("|")
        if len(cols) <= max_idx:
            continue
        symbol = cols[i_sym].strip()
        sec_name = cols[i_name].strip()
        exchange = cols[i_exch].strip()
        etf = cols[i_etf].strip()
        test = cols[i_test].strip()
        if not symbol:
            continue
        if test != "N":
            continue
        if exchange != exchange_code:
            continue
        if etf != "N":
            continue
        if "$" in symbol:
            skipped_dollar += 1
            continue
        ticker = symbol.replace(".", "-").upper()
        if _looks_like_non_common_stock(ticker):
            skipped_suffix += 1
            continue
        if _looks_like_non_common_stock_by_name(sec_name):
            skipped_name += 1
            continue
        if ticker in seen:
            continue
        seen.add(ticker)
        out.append(Symbol(ticker=ticker, source_exchange=source_exchange))
    if skipped_dollar:
        log.info(
            f"Phase A: dropped {skipped_dollar} '$'-bearing tickers from "
            f"{label} list (preferred-share series)."
        )
    if skipped_suffix:
        log.info(
            f"Phase A: dropped {skipped_suffix} warrant/unit/right tickers "
            f"from {label} list (suffix -W/-U/-R variants)."
        )
    if skipped_name:
        log.info(
            f"Phase A: dropped {skipped_name} preferred/debenture/note entries "
            f"from {label} list (matched Security Name pattern)."
        )
    log.info(
        f"Phase A: parsed {len(out)} {label} stock tickers from NASDAQ Trader "
        f"(Exchange={exchange_code}, Test=N, ETF=N, no '$', no -W/-U/-R suffix, "
        f"no preferred/debenture/note name)."
    )
    return out


def _fetch_otherlisted_text() -> str:
    """Download otherlisted.txt and return the raw text."""
    resp = requests.get(OTHER_LISTED_URL, headers=_HTTP_HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def fetch_nyse_stocks() -> list[Symbol]:
    """NYSE proper (Exchange='N') common stocks."""
    return _parse_otherlisted(
        _fetch_otherlisted_text(), "N", "NYSE-proper", source_exchange="NYSE"
    )


def fetch_nyse_american_stocks() -> list[Symbol]:
    """NYSE American (Exchange='A', formerly AMEX) common stocks."""
    return _parse_otherlisted(
        _fetch_otherlisted_text(), "A", "NYSE American", source_exchange="NYSE"
    )


def fetch_nyse_arca_stocks() -> list[Symbol]:
    """NYSE Arca (Exchange='P') common stocks. Arca hosts mostly ETFs;
    the ETF=N filter keeps only the small number of common stocks
    listed on Arca."""
    return _parse_otherlisted(
        _fetch_otherlisted_text(), "P", "NYSE Arca", source_exchange="NYSE"
    )


# ----- Source 3: NASDAQ Global Select stocks from NASDAQ Trader ------------

def fetch_nasdaq_q_stocks() -> list[Symbol]:
    """Download NASDAQ Global Select Market common stocks from NASDAQ
    Trader's nasdaqlisted.txt. Filters: Market Category="Q", Test=N,
    ETF=N, no "$", no -W/-U/-R suffix, no preferred/debenture/note
    Security Name."""
    log = get_logger()
    resp = requests.get(NASDAQ_LISTED_URL, headers=_HTTP_HEADERS, timeout=30)
    resp.raise_for_status()
    text = resp.text

    lines = text.splitlines()
    if not lines:
        raise RuntimeError("nasdaqlisted.txt is empty.")
    header = lines[0].split("|")
    try:
        i_sym = header.index("Symbol")
        i_name = header.index("Security Name")
        i_mkt = header.index("Market Category")
        i_test = header.index("Test Issue")
        i_etf = header.index("ETF")
    except ValueError as e:
        raise RuntimeError(f"Unexpected nasdaqlisted.txt header: {header}") from e

    out: list[Symbol] = []
    seen: set[str] = set()
    skipped_dollar = 0
    skipped_suffix = 0
    skipped_name = 0
    max_idx = max(i_sym, i_name, i_mkt, i_test, i_etf)
    for line in lines[1:]:
        if not line or line.startswith("File Creation Time"):
            continue
        cols = line.split("|")
        if len(cols) <= max_idx:
            continue
        symbol = cols[i_sym].strip()
        sec_name = cols[i_name].strip()
        market_cat = cols[i_mkt].strip()
        test = cols[i_test].strip()
        etf = cols[i_etf].strip()
        if not symbol:
            continue
        if test != "N":
            continue
        if market_cat != "Q":
            continue
        if etf != "N":
            continue
        if "$" in symbol:
            skipped_dollar += 1
            continue
        ticker = symbol.replace(".", "-").upper()
        if _looks_like_non_common_stock(ticker):
            skipped_suffix += 1
            continue
        if _looks_like_non_common_stock_by_name(sec_name):
            skipped_name += 1
            continue
        if ticker in seen:
            continue
        seen.add(ticker)
        out.append(Symbol(ticker=ticker, source_exchange="NASDAQ"))
    if skipped_dollar:
        log.info(
            f"Phase A: dropped {skipped_dollar} '$'-bearing tickers from "
            f"NASDAQ list (preferred-share series)."
        )
    if skipped_suffix:
        log.info(
            f"Phase A: dropped {skipped_suffix} warrant/unit/right tickers "
            f"from NASDAQ list (suffix -W/-U/-R variants)."
        )
    if skipped_name:
        log.info(
            f"Phase A: dropped {skipped_name} preferred/debenture/note entries "
            f"from NASDAQ list (matched Security Name pattern)."
        )
    log.info(
        f"Phase A: parsed {len(out)} NASDAQ Global Select stock tickers "
        f"from NASDAQ Trader (Market Cat=Q, Test=N, ETF=N, no '$', "
        f"no -W/-U/-R suffix, no preferred/debenture/note name)."
    )
    return out


# ----- Universe orchestration ----------------------------------------------

def _merge(seen: dict[str, Symbol], new: list[Symbol]) -> int:
    """Add new symbols to `seen`. Returns the count of entries already
    in `seen` (the overlap). When a ticker is already present without
    a source_exchange (typically the Wikipedia/S&P 500 entry that
    didn't know the exchange) we upgrade to the new Symbol that does
    carry one."""
    overlap = 0
    for s in new:
        existing = seen.get(s.ticker)
        if existing is None:
            seen[s.ticker] = s
        else:
            overlap += 1
            if existing.source_exchange is None and s.source_exchange is not None:
                seen[s.ticker] = s
    return overlap


def fetch_universe() -> list[Symbol]:
    """Fetch S&P 500 + NYSE-proper + NASDAQ-Global-Select + NYSE-American
    + NYSE-Arca, merged and deduplicated. otherlisted.txt is downloaded
    once and parsed three times (Exchange=N, A, P)."""
    log = get_logger()
    sp500 = fetch_sp500_tickers()
    otherlisted_text = _fetch_otherlisted_text()
    nyse = _parse_otherlisted(otherlisted_text, "N", "NYSE-proper", "NYSE")
    nasdaq_q = fetch_nasdaq_q_stocks()
    nyse_american = _parse_otherlisted(otherlisted_text, "A", "NYSE American", "NYSE")
    nyse_arca = _parse_otherlisted(otherlisted_text, "P", "NYSE Arca", "NYSE")

    seen: dict[str, Symbol] = {}
    _merge(seen, sp500)
    nyse_overlap = _merge(seen, nyse)
    nasdaq_overlap = _merge(seen, nasdaq_q)
    amex_overlap = _merge(seen, nyse_american)
    arca_overlap = _merge(seen, nyse_arca)

    out = list(seen.values())
    log.info(
        f"Phase A: merged universe — S&P 500 ({len(sp500)}) + NYSE ({len(nyse)}) "
        f"+ NASDAQ-Q ({len(nasdaq_q)}) + NYSE-American ({len(nyse_american)}) "
        f"+ NYSE-Arca ({len(nyse_arca)}); overlaps with prior set: "
        f"NYSE={nyse_overlap}, NASDAQ-Q={nasdaq_overlap}, "
        f"NYSE-American={amex_overlap}, NYSE-Arca={arca_overlap}; "
        f"final unique tickers = {len(out)}."
    )
    return out


# ----- Phase A entry point -------------------------------------------------

def run_phase_a(
    sec_user_agent: str,
    test_tickers: Optional[list[str]] = None,
) -> list[UniverseEntry]:
    """Execute Phase A end-to-end:

       1. Build the universe = union of the five source lists (or use
          `test_tickers` for a fast smoke run).
       2. Download SEC EDGAR's company_tickers_exchange.json (one
          static file) and enrich each ticker with the authoritative
          exchange label.
       3. Return UniverseEntry list. The marketCap >=$1B filter lives
          in Phase B (read directly from Finnhub's /stock/profile2).

    Tickers SEC does not list keep their source-known exchange label
    (or fall back to "NYSE") and are still forwarded to Phase B;
    Finnhub decides per ticker whether to return data.
    """
    log = get_logger()
    log_stage_start(
        "STAGE 1 — Phase A (universe build + SEC exchange enrichment)"
    )

    if test_tickers:
        log.info(f"Phase A: using TEST ticker override: {test_tickers}")
        symbols = [
            Symbol(ticker=t.replace(".", "-").upper(), source_exchange=None)
            for t in test_tickers
        ]
    else:
        symbols = fetch_universe()

    log.info("Phase A: downloading SEC EDGAR company_tickers_exchange.json …")
    sec_map = sec_edgar.fetch_company_tickers(sec_user_agent)
    log.info(f"Phase A: SEC EDGAR returned exchange labels for {len(sec_map)} tickers.")

    universe: list[UniverseEntry] = []
    matched_count = 0
    unmatched_count = 0
    for sym in symbols:
        sec_entry = sec_map.get(sym.ticker)
        if sec_entry is not None:
            _cik, sec_ex_str = sec_entry
            matched_count += 1
        else:
            sec_ex_str = ""
            unmatched_count += 1
        # Choose the exchange label in priority order:
        # 1) authoritative SEC value, if recognised
        # 2) source_exchange (NASDAQ Trader / nasdaqlisted)
        # 3) default fallback "NYSE"
        sec_label = sec_edgar.normalise_sec_exchange(sec_ex_str)
        if sec_label:
            exchange = sec_label
        elif sym.source_exchange:
            exchange = sym.source_exchange
        else:
            exchange = "NYSE"
        universe.append(UniverseEntry(ticker=sym.ticker, exchange=exchange))

    log.info(
        f"Phase A: matched {matched_count} tickers to SEC exchange labels; "
        f"{unmatched_count} unmatched (mostly ADRs / foreign issuers — "
        f"these rely on source_exchange or the 'NYSE' default for their "
        f"exchange label, and are still passed to Finnhub in Phase B)."
    )
    log_stage_summary("STAGE 1 — Phase A", entered=len(symbols), passed=len(universe))
    return universe
