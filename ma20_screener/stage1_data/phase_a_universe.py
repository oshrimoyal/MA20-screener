"""Phase A: build the universe from the S&P 500 constituent list,
enrich each ticker with its authoritative listing exchange, and pass
on to Phase B.

Universe source (free, anonymous HTTP — no API key, no signup):

    https://en.wikipedia.org/wiki/List_of_S%26P_500_companies

The first table on the Wikipedia page is the constituents list (~500
tickers). The screener's scope is intentionally limited to this set
per operator decision; NYSE-proper / NASDAQ Global Select / NYSE
American / NYSE Arca are no longer pulled.

After the Wikipedia parse, every ticker is enriched with the
authoritative SEC EDGAR exchange label from
`company_tickers_exchange.json`. This is a single static-file download
and adds no per-ticker round-trips. The exchange label feeds two
things downstream: the Stage-4 TradingView link, and Phase B's
same-exchange grouping (FMP's /historical-price-full requires symbols
in a single batch to share an exchange).

The marketCap >=$1B filter lives in Phase B (read directly from FMP's
/quote.marketCap).
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
_HTTP_HEADERS = {"User-Agent": "MA20-Screener/1.0 (+https://github.com/oshrimoyal/MA20-screener)"}


@dataclass(frozen=True)
class Symbol:
    """A ticker as discovered by the source parser. `source_exchange`
    is None for the Wikipedia S&P 500 source (Wikipedia does not
    publish the listing exchange per row); the SEC EDGAR enrichment
    step fills it in."""
    ticker: str
    source_exchange: Optional[str] = None


@dataclass(frozen=True)
class UniverseEntry:
    """A ticker that completed Phase A: name and exchange label for
    the Stage-4 TradingView link. The exchange label is one of
    "NASDAQ" or "NYSE"."""
    ticker: str
    exchange: str


# ----- Source: S&P 500 from Wikipedia ------------------------------------

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


def fetch_universe() -> list[Symbol]:
    """The screener's universe is the S&P 500. This wrapper exists so
    Phase A's entry point and tests can substitute alternative source
    lists without touching `run_phase_a`."""
    return fetch_sp500_tickers()


# ----- Phase A entry point -------------------------------------------------

def run_phase_a(
    sec_user_agent: str,
    test_tickers: Optional[list[str]] = None,
) -> list[UniverseEntry]:
    """Execute Phase A end-to-end:

       1. Build the universe = the S&P 500 (or `test_tickers` for a
          fast smoke run).
       2. Download SEC EDGAR's company_tickers_exchange.json (one
          static file) and enrich each ticker with the authoritative
          exchange label.
       3. Return UniverseEntry list. The marketCap >=$1B filter lives
          in Phase B (read directly from FMP's /quote.marketCap).

    Tickers SEC does not list (rare in the S&P 500 — typically a few
    foreign issuers) keep their source-known exchange label or fall
    back to "NYSE" and are still forwarded to Phase B; FMP decides per
    ticker whether to return data.
    """
    log = get_logger()
    log_stage_start(
        "STAGE 1 — Phase A (S&P 500 + SEC exchange enrichment)"
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
        # 2) source_exchange (None for the Wikipedia source)
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
        f"Phase A: matched {matched_count} S&P 500 tickers to SEC exchange "
        f"labels; {unmatched_count} unmatched (default to 'NYSE')."
    )
    log_stage_summary("STAGE 1 — Phase A", entered=len(symbols), passed=len(universe))
    return universe
