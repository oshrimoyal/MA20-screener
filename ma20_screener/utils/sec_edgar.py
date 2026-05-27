"""SEC EDGAR helpers — ticker -> CIK + exchange mapping.

One free, anonymous HTTP endpoint from the U.S. Securities and Exchange
Commission:

    https://www.sec.gov/files/company_tickers_exchange.json

A single static file mapping every U.S.-listed common stock to its CIK
number and listing exchange. ~5,000-10,000 entries. Used by Phase A to
enrich each universe ticker with the authoritative SEC exchange label
(NYSE / NASDAQ).

SEC fair-access policy: every request MUST include a User-Agent header
with real contact information (a company name and an email). The
helper refuses to send a request without an "@" in the User-Agent.

(Previous revisions also queried the SEC XBRL Frames API for
`CommonStockSharesOutstanding`. That second endpoint was retired when
Phase B moved to a dedicated market-data provider for both OHLCV and
market cap.)
"""
from __future__ import annotations

from typing import Optional

import requests

SEC_COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"


def _http_get_json(url: str, user_agent: str, timeout: int = 30) -> dict:
    """One HTTP GET to an SEC endpoint, returning parsed JSON.

    Raises a clear RuntimeError if the operator forgot to set a real
    contact email in the User-Agent (SEC's fair-access policy will
    return 403 otherwise).
    """
    if not user_agent or "@" not in user_agent:
        raise RuntimeError(
            "SEC EDGAR requires a User-Agent header with a real contact "
            "email per their fair-access policy. Set "
            "runtime.sec_user_agent in config.yaml to something like "
            "'MA20-Screener you@example.com'."
        )
    headers = {"User-Agent": user_agent, "Accept": "application/json"}
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def fetch_company_tickers(user_agent: str) -> dict[str, tuple[int, str]]:
    """Download SEC's company_tickers_exchange.json and return a dict
    mapping uppercase ticker -> (CIK_int, SEC_exchange_string).

    The exchange string is what SEC reports verbatim, typically one of
    "Nasdaq", "NYSE", "NYSE American", "NYSE Arca", "CBOE", "OTC", or
    "" (blank). The caller normalises this to the screener's
    "NASDAQ" / "NYSE" labels.

    Tickers are normalised to internal form: uppercase, "." -> "-"
    (e.g. "BRK.B" -> "BRK-B") so they line up directly with the
    tickers used elsewhere in the pipeline.

    The CIK component of the returned tuple is currently unused by the
    pipeline (Phase B no longer queries SEC XBRL Frames), but is kept
    in the return shape so callers can still cross-reference SEC
    filings without an extra round-trip.
    """
    data = _http_get_json(SEC_COMPANY_TICKERS_URL, user_agent)
    fields = data.get("fields") or []
    rows = data.get("data") or []
    try:
        i_cik = fields.index("cik")
        i_tk = fields.index("ticker")
        i_ex = fields.index("exchange")
    except ValueError as e:
        raise RuntimeError(
            f"Unexpected SEC company_tickers_exchange.json schema: {fields}"
        ) from e

    out: dict[str, tuple[int, str]] = {}
    max_i = max(i_cik, i_tk, i_ex)
    for row in rows:
        if len(row) <= max_i:
            continue
        cik = row[i_cik]
        tk = row[i_tk]
        ex = row[i_ex] if row[i_ex] is not None else ""
        if cik is None or tk is None:
            continue
        norm = str(tk).strip().upper().replace(".", "-")
        if not norm:
            continue
        try:
            cik_int = int(cik)
        except (TypeError, ValueError):
            continue
        # First occurrence wins (the file rarely has duplicates).
        out.setdefault(norm, (cik_int, str(ex).strip()))
    return out


_SEC_EXCHANGE_NORMALISATION = {
    "nasdaq": "NASDAQ",
    "nasdaqgs": "NASDAQ",
    "nasdaqcm": "NASDAQ",
    "nasdaqgm": "NASDAQ",
    "nyse": "NYSE",
    "nyse american": "NYSE",
    "nyse arca": "NYSE",
    "amex": "NYSE",
    "arca": "NYSE",
}


def normalise_sec_exchange(sec_exchange: str) -> Optional[str]:
    """Map SEC's reported exchange string to the screener's label
    ("NASDAQ" or "NYSE"). Returns None for unrecognised values (e.g.
    "OTC", "CBOE", ""), so the caller can fall back to a source-known
    exchange or the default."""
    if not sec_exchange:
        return None
    low = sec_exchange.strip().lower()
    if low in _SEC_EXCHANGE_NORMALISATION:
        return _SEC_EXCHANGE_NORMALISATION[low]
    if "nasdaq" in low:
        return "NASDAQ"
    if "nyse" in low or low == "arca" or low == "amex":
        return "NYSE"
    return None
