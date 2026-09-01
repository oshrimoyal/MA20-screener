"""Throwaway probe: what does FMP actually return? Delete when done.

    python fmp_fields.py --config config.local.yaml
"""
import argparse, json
from ma20_screener.config_loader import load_config
from ma20_screener.utils import fmp

ap = argparse.ArgumentParser()
ap.add_argument("--config", default="config.yaml")
key = load_config(ap.parse_args().config).runtime.fmp_api_key

print("=" * 70)
print("1) /stable/company-screener  — the call Phase A already makes")
print("=" * 70)
rows = fmp.fetch_company_screener(
    api_key=key, exchange="NASDAQ", market_cap_more_than=1e9,
    volume_more_than=1, is_actively_trading=True, limit=1,
)
if rows:
    print(json.dumps(rows[0], indent=2))
    keys = set(rows[0])
    print("\n>>> yearLow present?  ", "YES" if "yearLow" in keys else "NO")
    print(">>> yearHigh present? ", "YES" if "yearHigh" in keys else "NO")
else:
    print("no rows returned")

print()
print("=" * 70)
print("2) /stable/quote  — one extra call per ticker if we used it")
print("=" * 70)
q = fmp.fetch_quote("AAPL", key)
print(json.dumps(q, indent=2))
keys = set(q)
print("\n>>> yearLow present?  ", "YES" if "yearLow" in keys else "NO")
print(">>> yearHigh present? ", "YES" if "yearHigh" in keys else "NO")
