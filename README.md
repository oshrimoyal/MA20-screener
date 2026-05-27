# MA20 Screener

A daily long-side stock screener over the NYSE + NASDAQ universe
(market cap >= $1B, common stocks only). The system applies a
6-category filter to every closed-candle session and emits both a
per-run CSV (all stocks, passed + rejected) and a Telegram broadcast
of the day's candidates.

For the full operator manual (Hebrew), see **[`GUIDE.md`](GUIDE.md)**.

## Prerequisites

You need three things set up before the first run. Without all three,
`python main.py` will exit immediately with a validation error.

| What | How | Cost |
|---|---|---|
| **FMP Starter subscription** | Subscribe at [financialmodelingprep.com/pricing-plans](https://site.financialmodelingprep.com/pricing-plans), copy the API key from your dashboard. | $19/mo (billed annually = $228/yr) |
| **Telegram bot** | Create a bot via [@BotFather](https://t.me/BotFather) on Telegram → save the token. Send `/start` to your bot, then visit `https://api.telegram.org/bot<TOKEN>/getUpdates` to find your numeric `chat_id`. | Free |
| **Python 3.11+** | `python --version` to verify. | Free |

Notes about FMP:

* **FMP Basic (free) is NOT enough.** Multi-symbol historical batching
  and `/stable/batch-quote` are Premium-tier; `/stable/company-screener`
  needs Starter. The screener-based universe build assumes Starter.
* The same FMP key powers both Phase A (`company-screener`) and
  Phase B (`historical-price-eod/full`). Keep it in
  `runtime.fmp_api_key` only — never commit it.
* A few high-profile tickers (e.g. `BRK.B`) are gated to Premium on
  `historical-price-eod/full` and will appear in the CSV as `Rejected`
  with reason `fmp historical: ... permanent HTTP 402`. This is
  expected on Starter.

## Quick start

```bash
# 1. Pull the latest code
git pull origin main

# 2. (First time on a machine) install Python deps
pip install -r requirements.txt

# 3. Edit config.yaml with your real values. The fields you MUST set:
#       telegram.token
#       telegram.chat_id
#       runtime.fmp_api_key
#    Leave runtime.test_tickers empty for a full universe scan, or
#    set it to a comma-separated list (e.g. "AAPL,MSFT,NVDA") for a
#    fast smoke test.
nano config.yaml   # or any editor

# 4. Run
python main.py
```

Expected runtime for a full NASDAQ+NYSE >= $1B run (~1,900 tickers):

| `history_workers` × `history_sleep_ms` | Wall-clock |
|---|---|
| 1 × 250 (defaults shipped in `config.yaml`) | ~27 minutes |
| **4 × 400 (recommended)** | **~8 minutes** |

Bumping `history_workers` to 4 keeps us at ~4 calls/sec, safely under
FMP Starter's 300 calls/min ceiling. If you ever see
`fmp historical rate-limited after N attempts` in the log, lower
`history_workers` back to 1 or raise `history_sleep_ms`.

Outputs after each run:

* `./output/MA20_Stocks_DD-MM-YYYY.csv` — every analysed ticker
  (Passed + Rejected with reason).
* `./logs/run_YYYYMMDD_HHMMSS.log` — per-stage log, including the
  exact failure reason for every dropped ticker.
* Telegram — passed candidates only, header + groups of 5 per message.

## Architecture

Pipeline stages live in `ma20_screener/`:

| Stage | Module |
|---|---|
| 1. Data infrastructure | `stage1_data/` (Phase A FMP `company-screener` for the NASDAQ+NYSE >= $1B universe, Phase B FMP OHLCV with strict validation, Phase C SMA20 / Wilder ATR% / Lambert CCI14 / open gaps) |
| 2. Six raw checks | `stage2_checks/` (trend, candle + 11 formations, 7-day volume, SMA20 position, gaps vs price, CCI status) |
| 3. Six-category AND filter | `stage3_filter/filter.py` |
| 4. CSV + Telegram | `stage4_output/csv_writer.py`, `stage4_output/telegram_sender.py` |
