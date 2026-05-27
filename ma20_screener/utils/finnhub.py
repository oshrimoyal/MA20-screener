"""Finnhub daily OHLCV and company-profile wrapper.

Finnhub (https://finnhub.io) offers a free tier (60 calls/minute,
one-time email signup) that covers both the per-ticker daily candle
endpoint and a profile endpoint that includes `marketCapitalization`.
This replaces the previous Stooq + SEC-XBRL-Frames combination that
became unusable from the operator's IP range.

Two endpoints, both anonymous after the token is set:

  * /stock/candle  — daily OHLCV bars between two Unix timestamps.
    https://finnhub.io/api/v1/stock/candle?symbol=AAPL&resolution=D
        &from={unix_from}&to={unix_to}&token={api_key}
    Response shape:
        {"o": [...], "h": [...], "l": [...], "c": [...],
         "v": [...], "t": [unix_ts...], "s": "ok"|"no_data"}

  * /stock/profile2 — company profile including marketCapitalization
    in MILLIONS of USD.
    https://finnhub.io/api/v1/stock/profile2?symbol=AAPL&token={api_key}

Rate-limit policy: 60 calls/min (1/sec) on the free tier with a 30/sec
hard cap across all plans. Phase B throttles via the standard
parallel_map helper plus an explicit 429-aware retry loop in
phase_b_history.py.

Ticker normalisation: internal tickers use '-' for class shares (e.g.
BRK-B); Finnhub expects '.' (BRK.B). The helpers convert at the URL
boundary only.
"""
from __future__ import annotations

import pandas as pd
import requests

CANDLE_URL = "https://finnhub.io/api/v1/stock/candle"
PROFILE_URL = "https://finnhub.io/api/v1/stock/profile2"

OHLCV_COLS = ["Open", "High", "Low", "Close", "Volume"]


class FinnhubNotFound(Exception):
    """Finnhub answered cleanly that the ticker is not in its database
    (or that the requested window contains no candles). Permanent —
    do NOT retry."""


class FinnhubRateLimited(Exception):
    """Finnhub returned HTTP 429. Retryable, but back off harder than
    for generic transient errors since the rate-limit window is 60s."""


def _to_finnhub_symbol(ticker: str) -> str:
    """Map internal ticker form to Finnhub's: '-' -> '.' (BRK-B -> BRK.B)."""
    return ticker.upper().strip().replace("-", ".")


def fetch_candles(
    ticker: str,
    api_key: str,
    unix_from: int,
    unix_to: int,
    timeout: int = 30,
) -> pd.DataFrame:
    """Fetch daily OHLCV bars between two Unix timestamps.

    Returns a DataFrame indexed by date (DatetimeIndex, normalised to
    midnight, no tz) with columns Open / High / Low / Close / Volume
    (float). The number of rows depends on how many trading days fall
    inside the requested window; the caller is responsible for slicing
    to the expected 60-trading-day list.

    Raises:
      * FinnhubNotFound  — Finnhub returned s="no_data" or an empty
        result, or the response was structurally malformed.
      * FinnhubRateLimited — HTTP 429.
      * requests.HTTPError — any other non-2xx response.
    """
    if not ticker:
        raise ValueError("ticker must be non-empty")
    if not api_key:
        raise ValueError("api_key must be non-empty")
    params = {
        "symbol": _to_finnhub_symbol(ticker),
        "resolution": "D",
        "from": str(int(unix_from)),
        "to": str(int(unix_to)),
        "token": api_key,
    }
    resp = requests.get(CANDLE_URL, params=params, timeout=timeout)
    if resp.status_code == 429:
        raise FinnhubRateLimited(f"Finnhub rate-limited /stock/candle for {ticker!r}")
    resp.raise_for_status()
    payload = resp.json() or {}
    status = payload.get("s")
    if status != "ok":
        raise FinnhubNotFound(
            f"Finnhub /stock/candle returned s={status!r} for {ticker!r}"
        )
    timestamps = payload.get("t") or []
    closes = payload.get("c") or []
    if not timestamps or not closes or len(timestamps) != len(closes):
        raise FinnhubNotFound(
            f"Finnhub /stock/candle empty or length-mismatched for {ticker!r}"
        )
    opens = payload.get("o") or []
    highs = payload.get("h") or []
    lows = payload.get("l") or []
    volumes = payload.get("v") or []
    n = len(timestamps)
    if not (len(opens) == len(highs) == len(lows) == len(volumes) == n):
        raise FinnhubNotFound(
            f"Finnhub /stock/candle column length mismatch for {ticker!r}"
        )
    df = pd.DataFrame({
        "Open":   opens,
        "High":   highs,
        "Low":    lows,
        "Close":  closes,
        "Volume": volumes,
    })
    idx = pd.to_datetime(timestamps, unit="s", utc=True).tz_convert(None).normalize()
    df.index = idx
    df.index.name = None
    return df.astype(float)


def fetch_profile(ticker: str, api_key: str, timeout: int = 30) -> dict:
    """Fetch /stock/profile2 and return the parsed payload.

    Raises:
      * FinnhubNotFound — payload is empty or `marketCapitalization` is
        missing / non-positive (Finnhub does not know the ticker, or
        does not publish a market cap for it).
      * FinnhubRateLimited — HTTP 429.
      * requests.HTTPError — any other non-2xx response.

    `marketCapitalization` from Finnhub is in MILLIONS of USD; the
    caller is responsible for multiplying by 1_000_000.
    """
    if not ticker:
        raise ValueError("ticker must be non-empty")
    if not api_key:
        raise ValueError("api_key must be non-empty")
    params = {"symbol": _to_finnhub_symbol(ticker), "token": api_key}
    resp = requests.get(PROFILE_URL, params=params, timeout=timeout)
    if resp.status_code == 429:
        raise FinnhubRateLimited(f"Finnhub rate-limited /stock/profile2 for {ticker!r}")
    resp.raise_for_status()
    payload = resp.json() or {}
    if not payload:
        raise FinnhubNotFound(f"Finnhub /stock/profile2 empty payload for {ticker!r}")
    market_cap = payload.get("marketCapitalization")
    try:
        mc_float = float(market_cap)
    except (TypeError, ValueError):
        mc_float = 0.0
    if mc_float <= 0:
        raise FinnhubNotFound(
            f"Finnhub /stock/profile2 has no positive marketCapitalization "
            f"for {ticker!r}"
        )
    return payload
