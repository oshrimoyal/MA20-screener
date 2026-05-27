"""SEC EDGAR helpers — ticker -> CIK + exchange mapping, and bulk
`CommonStockSharesOutstanding` lookups via the XBRL Frames API.

Two free, anonymous HTTP endpoints from the U.S. Securities and
Exchange Commission:

1. Company tickers JSON
   https://www.sec.gov/files/company_tickers_exchange.json
   A single static file mapping every U.S.-listed common stock to its
   CIK number and listing exchange. ~5,000-10,000 entries.

2. XBRL Frames API
   https://data.sec.gov/api/xbrl/frames/us-gaap/CommonStockSharesOutstanding/shares/CY{Y}Q{Q}I.json
   A bulk endpoint that returns one tagged numeric fact for every
   reporting filer at a given calendar period. We use the
   `CommonStockSharesOutstanding` tag at instant period boundaries
   (CY{Y}Q{Q}I = end of Q{Q} of year {Y}). Iterating the most recent
   2-4 calendar-quarter endpoints and keeping the most-recent reported
   value per CIK gives us shares outstanding for almost every U.S.
   filer in 1-4 HTTP calls (no per-ticker round-trip).

SEC fair-access policy: every request MUST include a User-Agent
header with real contact information (a company name and an email).
The helpers refuse to send a request without such a User-Agent.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

import requests

SEC_COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SEC_FRAMES_URL_FMT = (
    "https://data.sec.gov/api/xbrl/frames/us-gaap/"
    "CommonStockSharesOutstanding/shares/{period}.json"
)


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
    yfinance-style tickers used elsewhere.
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


def _candidate_quarters(today: Optional[date] = None, count: int = 4) -> list[str]:
    """Return the most recent `count` calendar-quarter codes
    ("CY{Y}Q{Q}I"), starting from the most recently CLOSED quarter
    and walking backwards. We never include the in-progress quarter
    because the frames endpoint only publishes data for completed
    periods."""
    if today is None:
        today = date.today()
    y, m = today.year, today.month
    # The most recently CLOSED calendar quarter.
    if m <= 3:
        cy, cq = y - 1, 4
    elif m <= 6:
        cy, cq = y, 1
    elif m <= 9:
        cy, cq = y, 2
    else:
        cy, cq = y, 3
    out: list[str] = []
    for _ in range(max(1, count)):
        out.append(f"CY{cy}Q{cq}I")
        cq -= 1
        if cq == 0:
            cq = 4
            cy -= 1
    return out


def fetch_shares_outstanding(
    user_agent: str,
    today: Optional[date] = None,
    max_quarters: int = 4,
) -> tuple[dict[int, int], list[str]]:
    """Fetch the most-recent reported `CommonStockSharesOutstanding`
    value for every U.S. filer.

    Returns a `(shares_by_cik, quarters_used)` tuple where:
      * shares_by_cik : dict CIK_int -> shares_outstanding_int.
      * quarters_used : list of CY{Y}Q{Q}I codes that contributed at
                        least one new CIK (for logging).

    Iterates the last `max_quarters` calendar-quarter endpoints (most
    recent first). A CIK is filled from the first frame in which it
    appears. ADRs filing 20-F often do NOT appear in any frame; they
    are simply absent from the returned dict (the caller logs them as
    "shares outstanding unavailable").
    """
    by_cik: dict[int, int] = {}
    quarters_used: list[str] = []
    for quarter in _candidate_quarters(today=today, count=max_quarters):
        url = SEC_FRAMES_URL_FMT.format(period=quarter)
        try:
            data = _http_get_json(url, user_agent)
        except Exception:
            # Quarter not available yet (typical 404 for the very latest
            # quarter while filings are still rolling in). Try the next.
            continue
        added_here = 0
        for entry in data.get("data") or []:
            cik = entry.get("cik")
            val = entry.get("val")
            if cik is None or val is None:
                continue
            try:
                cik_int = int(cik)
                shares = int(val)
            except (TypeError, ValueError):
                continue
            if shares <= 0:
                continue
            if cik_int not in by_cik:
                by_cik[cik_int] = shares
                added_here += 1
        if added_here > 0:
            quarters_used.append(quarter)
    return by_cik, quarters_used


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
