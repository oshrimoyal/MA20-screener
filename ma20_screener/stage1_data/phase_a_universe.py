"""Phase A: build the universe via FMP /stable/company-screener.

The universe is EVERY symbol listed on NASDAQ or NYSE, minus exactly
four exclusions. Nothing else is filtered out — ETFs, closed-end
funds and foreign issuers (ADRs) are all in scope.

The four exclusions, and where each is enforced:

  1. marketCap < $1B          — screener, server-side (re-checked here)
  2. ticker contains `$`      — here (preferred-share series)
  3. ticker contains `-`      — here. After the dot-to-dash
     normalisation, class-share variants such as `BRK.B`/`BF.B`
     arrive as `BRK-B`/`BF-B`. FMP gates these to a higher tier on
     `/stable/historical-price-eod/full` anyway, so dropping them in
     Phase A saves a wasted call per ticker in Phase B.
  4. last-day volume <= 1M    — Phase B, once OHLCV is in hand

`isActivelyTrading=true` is also sent, but it is a hygiene flag rather
than a selection criterion: a halted or delisted symbol has no volume
and would fail exclusion 4 regardless, so keeping it only avoids
paying for a doomed FMP call. `isEtf`, `isFund` and `country` are
deliberately left unconstrained.

Normal mode: two screener calls (NASDAQ + NYSE). The response already
includes `marketCap` and `exchangeShortName`, so Phase B does NOT need
a separate quote pass.

Test mode: when `test_tickers` is set in config.yaml, Phase A skips
the screener and instead makes one /stable/quote call per requested
symbol, keeping smoke runs cheap.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ma20_screener.logger import (
    get_logger,
    log_stage_start,
    log_stage_summary,
    log_ticker_fail,
)
from ma20_screener.utils import fmp


MARKET_CAP_FLOOR_USD = 1_000_000_000.0
_SCREENER_EXCHANGES = ("NASDAQ", "NYSE")


@dataclass(frozen=True)
class UniverseEntry:
    """A ticker that completed Phase A: name, exchange label for the
    Stage-4 TradingView link, and the market cap that already passed
    the >=$1B filter on the FMP screener side."""
    ticker: str        # internal form, '-' for class shares
    exchange: str      # "NASDAQ" or "NYSE"
    market_cap: float  # USD; >= MARKET_CAP_FLOOR_USD


def _normalise_ticker(symbol: str) -> str:
    """FMP 'BRK.B' or 'BRK-B' -> internal 'BRK-B'."""
    return str(symbol).upper().strip().replace(".", "-")


def _normalise_exchange(exchange_short_name: str) -> Optional[str]:
    """Map FMP `exchangeShortName` to the screener's two labels.
    Returns None for anything outside NASDAQ / NYSE."""
    s = (exchange_short_name or "").strip().upper()
    if s == "NASDAQ":
        return "NASDAQ"
    if s == "NYSE":
        return "NYSE"
    return None


def _build_from_screener(fmp_api_key: str) -> list[UniverseEntry]:
    """Two screener calls (NASDAQ + NYSE) -> merged, deduped universe."""
    log = get_logger()
    seen: dict[str, UniverseEntry] = {}
    skipped_dollar = 0
    skipped_dash = 0
    skipped_unknown_exchange = 0
    skipped_below_floor = 0
    skipped_dup = 0
    for exchange in _SCREENER_EXCHANGES:
        log.info(
            f"Phase A: FMP company-screener exchange={exchange} "
            f"(marketCap >= ${MARKET_CAP_FLOOR_USD:,.0f}, "
            f"isActivelyTrading=true; ETFs, funds and non-US issuers "
            f"are NOT excluded)…"
        )
        rows = fmp.fetch_company_screener(
            api_key=fmp_api_key,
            exchange=exchange,
            market_cap_more_than=MARKET_CAP_FLOOR_USD,
            # isEtf / isFund / country are deliberately NOT constrained:
            # the universe is every NASDAQ- or NYSE-listed symbol above
            # the market-cap floor, ETFs, closed-end funds and foreign
            # issuers (ADRs) included. isActivelyTrading stays on — it
            # excludes nothing tradeable (a halted or delisted symbol has
            # no volume and would fail the last-day volume gate anyway)
            # and saves one wasted FMP call per dead ticker.
            is_actively_trading=True,
        )
        added = 0
        for row in rows:
            symbol = row.get("symbol")
            if not symbol:
                continue
            ticker = _normalise_ticker(symbol)
            if not ticker:
                continue
            if "$" in ticker:
                skipped_dollar += 1
                continue
            if "-" in ticker:
                # Drops class-B-style tickers (BRK.B -> BRK-B after
                # the dot->dash normalisation) which FMP gates to a
                # higher tier on /stable/historical-price-eod/full
                # anyway.
                skipped_dash += 1
                continue
            normalised_ex = _normalise_exchange(row.get("exchangeShortName") or "")
            if normalised_ex is None:
                skipped_unknown_exchange += 1
                continue
            try:
                market_cap = float(row.get("marketCap") or 0.0)
            except (TypeError, ValueError):
                skipped_below_floor += 1
                continue
            if market_cap < MARKET_CAP_FLOOR_USD:
                skipped_below_floor += 1
                continue
            if ticker in seen:
                skipped_dup += 1
                continue
            seen[ticker] = UniverseEntry(
                ticker=ticker, exchange=normalised_ex, market_cap=market_cap,
            )
            added += 1
        log.info(
            f"Phase A: screener returned {len(rows)} rows for "
            f"exchange={exchange}; added {added} new unique tickers."
        )
    if skipped_dollar:
        log.info(
            f"Phase A: dropped {skipped_dollar} '$'-bearing tickers "
            f"(preferred-share series)."
        )
    if skipped_dash:
        log.info(
            f"Phase A: dropped {skipped_dash} '-'-bearing tickers "
            f"(class-share variants like BRK.B / BF.B that FMP gates "
            f"to a higher tier on /historical-price-eod/full)."
        )
    if skipped_unknown_exchange:
        log.info(
            f"Phase A: dropped {skipped_unknown_exchange} tickers with "
            f"non-NASDAQ/NYSE exchangeShortName."
        )
    if skipped_below_floor:
        log.info(
            f"Phase A: dropped {skipped_below_floor} tickers below "
            f"the marketCap floor (defensive — screener should pre-filter)."
        )
    if skipped_dup:
        log.info(
            f"Phase A: skipped {skipped_dup} duplicate tickers across "
            f"NASDAQ + NYSE screener responses."
        )
    return list(seen.values())


def _build_from_test_tickers(
    fmp_api_key: str, test_tickers: list[str],
) -> list[UniverseEntry]:
    """Test mode: per-ticker /stable/quote to derive exchange +
    marketCap for each operator-supplied symbol. Bypasses the
    screener so smoke runs stay cheap (N calls, not 2 + N)."""
    log = get_logger()
    log.info(f"Phase A: using TEST ticker override: {test_tickers}")
    universe: list[UniverseEntry] = []
    for raw in test_tickers:
        ticker = _normalise_ticker(raw)
        if not ticker:
            continue
        if "$" in ticker:
            log_ticker_fail(ticker, "ticker contains '$'")
            continue
        if "-" in ticker:
            log_ticker_fail(ticker, "ticker contains '-' (class-share variant)")
            continue
        try:
            quote = fmp.fetch_quote(ticker, fmp_api_key)
        except fmp.FMPNotFound as e:
            log_ticker_fail(ticker, f"fmp quote: {e}")
            continue
        except Exception as e:
            log_ticker_fail(
                ticker, f"fmp quote error: {type(e).__name__}: {e}"
            )
            continue
        normalised_ex = _normalise_exchange(quote.get("exchange") or "")
        if normalised_ex is None:
            normalised_ex = "NYSE"  # lenient fallback in test mode
        try:
            market_cap = float(quote.get("marketCap") or 0.0)
        except (TypeError, ValueError):
            market_cap = 0.0
        universe.append(UniverseEntry(
            ticker=ticker, exchange=normalised_ex, market_cap=market_cap,
        ))
    return universe


def run_phase_a(
    fmp_api_key: str,
    test_tickers: Optional[list[str]] = None,
) -> list[UniverseEntry]:
    """Execute Phase A end-to-end:

      * Test mode (`test_tickers` non-empty): per-ticker /stable/quote.
      * Normal mode: two /stable/company-screener calls (NASDAQ +
        NYSE), merged + deduped, with the `$`-in-ticker filter
        applied on top of FMP's server-side filters.

    Returns a list of UniverseEntry with `market_cap` already known —
    Phase B does NOT need to call /stable/quote again.
    """
    log = get_logger()
    log_stage_start(
        "STAGE 1 — Phase A (FMP company-screener: NASDAQ + NYSE >= $1B)"
    )

    if test_tickers:
        universe = _build_from_test_tickers(fmp_api_key, test_tickers)
        entered = len(test_tickers)
    else:
        universe = _build_from_screener(fmp_api_key)
        entered = len(universe)

    log.info(
        f"Phase A: final unique universe = {len(universe)} tickers "
        f"(NASDAQ + NYSE common stocks, marketCap >= "
        f"${MARKET_CAP_FLOOR_USD:,.0f})."
    )
    log_stage_summary("STAGE 1 — Phase A", entered=entered, passed=len(universe))
    return universe
