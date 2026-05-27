"""Stooq daily OHLCV fetcher.

Stooq (https://stooq.com) publishes per-ticker daily CSVs at:

    https://stooq.com/q/d/l/?s={ticker}.us&i=d

For U.S. tickers the `.us` suffix is required. The ticker portion uses
the same dash-for-class-share convention as our internal representation
(e.g. BRK-B -> "brk-b"). The CSV is in chronological order, oldest
first, with the header row:

    Date,Open,High,Low,Close,Volume

When Stooq does not recognise the ticker the response body is short
HTML or a plain "No data" line rather than a CSV with headers. We
detect the absence of a "Date," header and raise `StooqNotFound`.

No official rate limit is published. Empirically Stooq tolerates
modest rates (~2 req/sec from a single IP); the caller throttles via
the standard `parallel_map` helper.
"""
from __future__ import annotations

import io

import pandas as pd
import requests

STOOQ_DAILY_URL_FMT = "https://stooq.com/q/d/l/?s={ticker}.us&i=d"

OHLCV_COLS = ["Open", "High", "Low", "Close", "Volume"]


class StooqNotFound(Exception):
    """Stooq returned a body that is not a CSV with the expected header.
    Treat as a permanent answer (ticker unknown), do NOT retry."""


def fetch_ohlcv(ticker: str, user_agent: str, timeout: int = 30) -> pd.DataFrame:
    """Fetch the full daily OHLCV history for `ticker` from Stooq.

    Returns a DataFrame indexed by date (DatetimeIndex, normalised to
    midnight, no tz) with columns Open / High / Low / Close / Volume.
    The DataFrame may have years of history; the caller slices to the
    expected 60-trading-day window.

    Raises:
      * StooqNotFound — Stooq did not return a CSV (unknown ticker).
      * requests.HTTPError — Stooq returned a non-200 status.
      * Other exceptions — network errors propagate verbatim.
    """
    if not ticker:
        raise ValueError("ticker must be non-empty")
    url = STOOQ_DAILY_URL_FMT.format(ticker=ticker.lower())
    headers = {"User-Agent": user_agent or "MA20-Screener/1.0"}
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    body = resp.text or ""
    # Stooq returns a short HTML/plain-text fragment when the ticker is
    # unknown ("No data", "B�ad sk�adni", or a redirect page). A valid
    # CSV always starts with the header row "Date,Open,High,...".
    head = body[:200].lstrip().lower()
    if not head.startswith("date,"):
        raise StooqNotFound(
            f"Stooq did not return a CSV body for {ticker!r} "
            f"(first bytes: {body[:80]!r})"
        )
    df = pd.read_csv(io.StringIO(body))
    missing = [c for c in ["Date"] + OHLCV_COLS if c not in df.columns]
    if missing:
        raise StooqNotFound(
            f"Stooq CSV for {ticker!r} missing columns: {missing}"
        )
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df = df.dropna(subset=["Date"])
    df = df.set_index("Date")
    df.index = df.index.normalize()
    df.index.name = None
    # Keep only the OHLCV columns in the canonical order. Adjusted-close
    # is not present in the daily endpoint; we use raw close.
    df = df[OHLCV_COLS]
    # Cast to float to mirror the previous yfinance-derived dtype.
    return df.astype(float)
