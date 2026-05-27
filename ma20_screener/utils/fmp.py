"""Financial Modeling Prep (FMP) /stable wrapper for the Starter plan.

FMP retired the legacy `/api/v3/...` endpoints for users who joined
after 31 Aug 2025; all new subscribers must use the `/stable/...`
namespace. Empirically (verified against the operator's live Starter
key), the `/stable` endpoints accept ONE symbol per call. Multi-symbol
comma-separated input on `/stable/historical-price-eod/full` is
silently dropped (returns `[]`); `/stable/batch-quote` is a Premium-tier
endpoint not included in Starter. So this wrapper exposes two
single-symbol functions and lets Phase B iterate at the Starter rate
limit (300 calls/min).

Endpoints:
  * /stable/historical-price-eod/full?symbol=AAPL&from=&to=
        Daily OHLCV. Returns a flat array of row dicts: one per
        trading day, each with {symbol, date, open, high, low, close,
        volume, ...}. Empty array if Starter does not include the
        symbol or the date range has no data.

  * /stable/quote?symbol=AAPL
        Real-time quote. Returns an array of length 1 (or 0 if the
        symbol is unknown), where the single element carries
        marketCap (USD, not millions), price, exchange, etc.

Auth via the `apikey` query parameter. Ticker normalisation: internal
form uses '-' for class shares (BRK-B); FMP uses '.' (BRK.B). The
wrapper converts at the URL boundary.
"""
from __future__ import annotations

import pandas as pd
import requests

HISTORICAL_URL = (
    "https://financialmodelingprep.com/stable/historical-price-eod/full"
)
QUOTE_URL = "https://financialmodelingprep.com/stable/quote"

OHLCV_COLS = ["Open", "High", "Low", "Close", "Volume"]


class FMPNotFound(Exception):
    """FMP returned an empty payload for this symbol. Permanent — do NOT retry."""


class FMPRateLimited(Exception):
    """FMP returned HTTP 429. Retryable; the rate-limit window is short."""


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
