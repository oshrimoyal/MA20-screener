"""Financial Modeling Prep (FMP) free-tier wrapper.

FMP's free Basic plan offers two endpoints relevant to the screener:

  * /api/v3/historical-price-full/{symbols}?from=&to=
        Daily OHLCV. Accepts 1-3 comma-separated symbols per call
        (FMP's documented batch ceiling), all from the same exchange.

  * /api/v3/quote/{symbols}
        Real-time quote with `marketCap`. Accepts a comma-separated
        list; the wrapper defaults to batches of 50 symbols.

Free-tier limits: 250 calls/day and 5 calls/minute. Phase B paces the
batch loop at ~12.5 s/call to stay safely under the per-minute cap.

Auth via the `apikey` query parameter. Ticker normalisation: internal
form uses '-' for class shares (BRK-B); FMP uses '.' (BRK.B). The
wrapper converts at the URL boundary and converts back when keying
the returned dicts so callers always see the internal form.
"""
from __future__ import annotations

import pandas as pd
import requests

HISTORICAL_URL_FMT = (
    "https://financialmodelingprep.com/api/v3/historical-price-full/{symbols}"
)
QUOTE_URL_FMT = "https://financialmodelingprep.com/api/v3/quote/{symbols}"

OHLCV_COLS = ["Open", "High", "Low", "Close", "Volume"]

# FMP-documented hard ceiling for /historical-price-full batching.
HISTORICAL_BATCH_MAX = 3
# Conservative ceiling for /quote — FMP does not document an upper
# bound, so we cap to keep URLs short and predictable.
QUOTE_BATCH_MAX = 50


class FMPNotFound(Exception):
    """FMP returned a clean response that does not contain the
    requested symbol (or contains no historical rows). Treat as a
    permanent answer — do NOT retry."""


class FMPRateLimited(Exception):
    """FMP returned HTTP 429. Retryable, but back off harder than for
    generic transient errors since the free-tier window is 60s."""


def _to_fmp_symbol(ticker: str) -> str:
    """Internal 'BRK-B' -> FMP 'BRK.B'."""
    return ticker.upper().strip().replace("-", ".")


def _from_fmp_symbol(symbol: str) -> str:
    """FMP 'BRK.B' -> internal 'BRK-B'."""
    return symbol.upper().strip().replace(".", "-")


def _normalise_historical_rows(rows: list[dict]) -> pd.DataFrame:
    """Convert FMP's `historical` array (list of {date, open, high,
    low, close, volume, ...} dicts) into a DataFrame indexed by date
    (normalised, no tz) with float OHLCV columns. Returns an empty
    DataFrame if `rows` is empty."""
    if not rows:
        return pd.DataFrame(columns=OHLCV_COLS)
    df = pd.DataFrame(rows)
    if "date" not in df.columns:
        raise FMPNotFound("FMP historical row is missing the 'date' field")
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).set_index("date")
    df.index = df.index.normalize()
    df.index.name = None
    # FMP uses lowercase column names; rename to canonical OHLCV.
    rename_map = {
        "open": "Open", "high": "High", "low": "Low",
        "close": "Close", "volume": "Volume",
    }
    df = df.rename(columns=rename_map)
    missing = [c for c in OHLCV_COLS if c not in df.columns]
    if missing:
        raise FMPNotFound(f"FMP historical batch missing columns: {missing}")
    return df[OHLCV_COLS].astype(float).sort_index()


def fetch_historical_prices_batch(
    symbols: list[str],
    api_key: str,
    from_date: str,
    to_date: str,
    timeout: int = 30,
) -> dict[str, pd.DataFrame]:
    """Fetch daily OHLCV for 1-3 symbols in one call.

    Returns a dict keyed by INTERNAL ticker form (with '-'). Symbols
    absent from the response are absent from the dict — the caller
    treats them as "FMP did not return data" without raising.

    Raises:
      * FMPRateLimited — HTTP 429.
      * FMPNotFound    — payload structure is unrecognised.
      * requests.HTTPError — any other non-2xx response.
    """
    if not symbols:
        return {}
    if len(symbols) > HISTORICAL_BATCH_MAX:
        raise ValueError(
            f"fetch_historical_prices_batch accepts at most "
            f"{HISTORICAL_BATCH_MAX} symbols per call; got {len(symbols)}."
        )
    if not api_key:
        raise ValueError("api_key must be non-empty")

    fmp_symbols = [_to_fmp_symbol(s) for s in symbols]
    url = HISTORICAL_URL_FMT.format(symbols=",".join(fmp_symbols))
    params = {"from": from_date, "to": to_date, "apikey": api_key}
    resp = requests.get(url, params=params, timeout=timeout)
    if resp.status_code == 429:
        raise FMPRateLimited(
            f"FMP rate-limited /historical-price-full for {symbols!r}"
        )
    resp.raise_for_status()
    payload = resp.json()

    # Single-symbol response: {"symbol": "...", "historical": [...]}
    # Multi-symbol response:  {"historicalStockList": [{"symbol": "...", "historical": [...]}, ...]}
    # Empty response: [] (FMP returns an empty array if no data at all)
    out: dict[str, pd.DataFrame] = {}
    if isinstance(payload, list):
        # No data returned for any symbol. Treat each requested symbol
        # as absent (caller will log accordingly).
        return out
    if not isinstance(payload, dict):
        raise FMPNotFound(
            f"FMP /historical-price-full returned unexpected type "
            f"{type(payload).__name__} for {symbols!r}"
        )

    if "historicalStockList" in payload:
        entries = payload.get("historicalStockList") or []
    else:
        entries = [payload]

    for entry in entries:
        sym_raw = entry.get("symbol")
        hist = entry.get("historical")
        if not sym_raw or not hist:
            continue
        try:
            df = _normalise_historical_rows(hist)
        except FMPNotFound:
            continue
        if df.empty:
            continue
        out[_from_fmp_symbol(str(sym_raw))] = df

    return out


def fetch_quotes_batch(
    symbols: list[str],
    api_key: str,
    timeout: int = 30,
) -> dict[str, dict]:
    """Fetch current quotes (including `marketCap`) for up to
    QUOTE_BATCH_MAX symbols in one call.

    Returns a dict keyed by INTERNAL ticker form (with '-'). Symbols
    absent from the response are absent from the dict.

    Raises:
      * FMPRateLimited — HTTP 429.
      * FMPNotFound    — payload structure is unrecognised.
      * requests.HTTPError — any other non-2xx response.
    """
    if not symbols:
        return {}
    if len(symbols) > QUOTE_BATCH_MAX:
        raise ValueError(
            f"fetch_quotes_batch accepts at most {QUOTE_BATCH_MAX} "
            f"symbols per call; got {len(symbols)}."
        )
    if not api_key:
        raise ValueError("api_key must be non-empty")

    fmp_symbols = [_to_fmp_symbol(s) for s in symbols]
    url = QUOTE_URL_FMT.format(symbols=",".join(fmp_symbols))
    params = {"apikey": api_key}
    resp = requests.get(url, params=params, timeout=timeout)
    if resp.status_code == 429:
        raise FMPRateLimited(f"FMP rate-limited /quote for {symbols!r}")
    resp.raise_for_status()
    payload = resp.json()

    if not isinstance(payload, list):
        raise FMPNotFound(
            f"FMP /quote returned unexpected type "
            f"{type(payload).__name__} for {symbols!r}"
        )

    out: dict[str, dict] = {}
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        sym_raw = entry.get("symbol")
        if not sym_raw:
            continue
        out[_from_fmp_symbol(str(sym_raw))] = entry
    return out
