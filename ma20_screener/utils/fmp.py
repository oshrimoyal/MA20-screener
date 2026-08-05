"""Financial Modeling Prep (FMP) /stable wrapper for the Starter plan.

FMP retired the legacy `/api/v3/...` endpoints for users who joined
after 31 Aug 2025; all new subscribers must use the `/stable/...`
namespace. Empirically (verified against the operator's live Starter
key):

  * /stable/historical-price-eod/full accepts ONE symbol per call.
    Multi-symbol comma-separated input is silently dropped (returns
    `[]`).
  * /stable/batch-quote is Premium-tier (HTTP 402 on Starter).
  * /stable/quote works for a single symbol.
  * /stable/company-screener works on Starter and supports filtering
    by exchange, marketCap, isEtf, isFund, isActivelyTrading, country,
    etc. ONE call returns up to `limit` matching companies with
    `marketCap` and `exchangeShortName` already attached — so Phase A
    can build the universe and skip a separate quote pass.

Endpoints exposed:
  * /stable/company-screener?exchange=NASDAQ&marketCapMoreThan=&...
        Universe builder used by Phase A. Returns array of company
        dicts with at least {symbol, exchangeShortName, marketCap,
        isEtf, isFund, isActivelyTrading}.

  * /stable/historical-price-eod/full?symbol=AAPL&from=&to=
        Daily OHLCV. Returns a flat array of row dicts (one per
        trading day).

  * /stable/quote?symbol=AAPL
        Real-time quote with marketCap (in USD, not millions). Used
        only on the test-mode path (one call per test ticker).

Auth via the `apikey` query parameter. Ticker normalisation: internal
form uses '-' for class shares (BRK-B); FMP uses '.' (BRK.B). The
wrapper converts at the URL boundary.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd
import requests

SCREENER_URL = "https://financialmodelingprep.com/stable/company-screener"
HISTORICAL_URL = (
    "https://financialmodelingprep.com/stable/historical-price-eod/full"
)
QUOTE_URL = "https://financialmodelingprep.com/stable/quote"

OHLCV_COLS = ["Open", "High", "Low", "Close", "Volume"]


class FMPNotFound(Exception):
    """FMP returned an empty payload for this symbol OR a permanent
    4xx that will not change with retries on the current subscription
    tier (401 / 402 / 403 / 404). Do NOT retry."""


class FMPRateLimited(Exception):
    """FMP returned HTTP 429. Retryable; the rate-limit window is short."""


# 4xx codes that mean "permanent answer on this tier" — retrying with
# the same key will not change the result. 402 in particular shows up
# for individual tickers that are gated behind a higher FMP plan
# (e.g. BRK.B on Starter).
_PERMANENT_HTTP_STATUSES = (401, 402, 403, 404)


def _raise_for_permanent_status(resp, label: str) -> None:
    """If the response status is in `_PERMANENT_HTTP_STATUSES`, raise
    FMPNotFound with a clear reason so the caller's retry loop treats
    it as permanent (no backoff, no re-attempt)."""
    if resp.status_code in _PERMANENT_HTTP_STATUSES:
        raise FMPNotFound(
            f"FMP {label} returned permanent HTTP {resp.status_code} "
            f"(likely gated to a higher subscription tier)"
        )


def _to_fmp_symbol(ticker: str) -> str:
    """Internal 'BRK-B' -> FMP 'BRK.B'."""
    return ticker.upper().strip().replace("-", ".")


def _normalise_historical_rows(rows: list[dict], ticker: str) -> pd.DataFrame:
    """Convert a flat row list into a DataFrame indexed by date
    (normalised, no tz) with float OHLCV columns. Raises FMPNotFound
    if the rows are missing required fields."""
    if not rows:
        raise FMPNotFound(f"FMP returned no historical rows for {ticker!r}")
    df = pd.DataFrame(rows)
    if "date" not in df.columns:
        raise FMPNotFound(f"FMP historical row missing 'date' for {ticker!r}")
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).set_index("date")
    df.index = df.index.normalize()
    df.index.name = None
    rename_map = {
        "open": "Open", "high": "High", "low": "Low",
        "close": "Close", "volume": "Volume",
    }
    df = df.rename(columns=rename_map)
    missing = [c for c in OHLCV_COLS if c not in df.columns]
    if missing:
        raise FMPNotFound(
            f"FMP historical for {ticker!r} missing columns: {missing}"
        )
    return df[OHLCV_COLS].astype(float).sort_index()


def fetch_company_screener(
    api_key: str,
    exchange: Optional[str] = None,
    market_cap_more_than: Optional[float] = None,
    volume_more_than: Optional[float] = None,
    is_etf: Optional[bool] = None,
    is_fund: Optional[bool] = None,
    is_actively_trading: Optional[bool] = None,
    country: Optional[str] = None,
    limit: int = 10000,
    timeout: int = 30,
) -> list[dict]:
    """Call /stable/company-screener with the supplied filters and
    return the parsed JSON array.

    Each row contains at least: symbol, companyName, marketCap,
    exchange, exchangeShortName, isEtf, isFund, isActivelyTrading.
    Phase A uses `exchangeShortName` and `marketCap` directly.

    Raises:
      * FMPRateLimited — HTTP 429.
      * FMPNotFound    — payload is not a JSON array (unexpected shape).
      * requests.HTTPError — any other non-2xx response.
    """
    if not api_key:
        raise ValueError("api_key must be non-empty")

    params: dict[str, str] = {"apikey": api_key, "limit": str(int(limit))}
    if exchange is not None:
        params["exchange"] = str(exchange)
    if market_cap_more_than is not None:
        params["marketCapMoreThan"] = str(int(market_cap_more_than))
    if volume_more_than is not None:
        # NOTE: FMP ignores volumeMoreThan=0 (verified against the live
        # API: the row count is identical with and without it). Pass 1
        # to exclude non-trading instruments.
        params["volumeMoreThan"] = str(int(volume_more_than))
    if is_etf is not None:
        params["isEtf"] = "true" if is_etf else "false"
    if is_fund is not None:
        params["isFund"] = "true" if is_fund else "false"
    if is_actively_trading is not None:
        params["isActivelyTrading"] = "true" if is_actively_trading else "false"
    if country is not None:
        params["country"] = str(country)

    resp = requests.get(SCREENER_URL, params=params, timeout=timeout)
    if resp.status_code == 429:
        raise FMPRateLimited("FMP rate-limited /company-screener")
    _raise_for_permanent_status(resp, "/company-screener")
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, list):
        raise FMPNotFound(
            f"FMP /company-screener returned unexpected type "
            f"{type(payload).__name__}"
        )
    return payload


def fetch_historical_prices(
    ticker: str,
    api_key: str,
    from_date: str,
    to_date: str,
    timeout: int = 30,
) -> pd.DataFrame:
    """Fetch daily OHLCV between `from_date` and `to_date` (YYYY-MM-DD)
    for ONE symbol.

    Returns a DataFrame indexed by date (normalised, no tz) with float
    OHLCV columns.

    Raises:
      * FMPNotFound — empty payload (symbol unknown or no data in
        the window).
      * FMPRateLimited — HTTP 429.
      * requests.HTTPError — any other non-2xx response.
    """
    if not ticker:
        raise ValueError("ticker must be non-empty")
    if not api_key:
        raise ValueError("api_key must be non-empty")

    params = {
        "symbol": _to_fmp_symbol(ticker),
        "from": from_date,
        "to": to_date,
        "apikey": api_key,
    }
    resp = requests.get(HISTORICAL_URL, params=params, timeout=timeout)
    if resp.status_code == 429:
        raise FMPRateLimited(
            f"FMP rate-limited /historical-price-eod/full for {ticker!r}"
        )
    _raise_for_permanent_status(
        resp, f"/historical-price-eod/full for {ticker!r}"
    )
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, list):
        raise FMPNotFound(
            f"FMP /historical-price-eod/full returned unexpected type "
            f"{type(payload).__name__} for {ticker!r}"
        )
    return _normalise_historical_rows(payload, ticker)


def fetch_quote(
    ticker: str,
    api_key: str,
    timeout: int = 30,
) -> dict:
    """Fetch the current quote (including `marketCap` in USD) for ONE
    symbol.

    Returns the raw quote payload (a dict).

    Raises:
      * FMPNotFound — payload is empty, symbol absent, or marketCap
        is missing / non-positive.
      * FMPRateLimited — HTTP 429.
      * requests.HTTPError — any other non-2xx response.
    """
    if not ticker:
        raise ValueError("ticker must be non-empty")
    if not api_key:
        raise ValueError("api_key must be non-empty")

    params = {"symbol": _to_fmp_symbol(ticker), "apikey": api_key}
    resp = requests.get(QUOTE_URL, params=params, timeout=timeout)
    if resp.status_code == 429:
        raise FMPRateLimited(f"FMP rate-limited /quote for {ticker!r}")
    _raise_for_permanent_status(resp, f"/quote for {ticker!r}")
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, list) or not payload:
        raise FMPNotFound(f"FMP /quote returned empty payload for {ticker!r}")
    entry = payload[0]
    if not isinstance(entry, dict):
        raise FMPNotFound(
            f"FMP /quote returned non-dict entry for {ticker!r}: "
            f"{type(entry).__name__}"
        )
    mc_raw = entry.get("marketCap")
    try:
        mc = float(mc_raw) if mc_raw is not None else 0.0
    except (TypeError, ValueError):
        mc = 0.0
    if mc <= 0:
        raise FMPNotFound(
            f"FMP /quote has no positive marketCap for {ticker!r}"
        )
    return entry
