# MA20 Screener

A daily long-side stock screener over the NYSE + NASDAQ universe —
every listed symbol above $1B market cap, ETFs, closed-end funds and
ADRs included. The system applies a
7-category filter to every closed-candle session and emits both a
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

# 3. Put your secrets in config.local.yaml, NOT config.yaml.
#    config.yaml is tracked by git, so local edits to it collide on
#    every pull and branch switch. config.local.yaml is gitignored and
#    never will.
cp config.yaml config.local.yaml     # Windows: copy config.yaml config.local.yaml
nano config.local.yaml               # or any editor

#    The three fields you MUST set:
#       telegram.token
#       telegram.chat_id
#       runtime.fmp_api_key
#    Leave runtime.test_tickers empty for a full universe scan, or set
#    it to a comma-separated list ("AAPL,MSFT,NVDA") for a smoke test.

# 4. Run
python main.py --config config.local.yaml
```

Expected wall-clock for a full NASDAQ+NYSE >= $1B run (~1,900 tickers):
**~8-9 minutes.**

The floor is set by your FMP quota, not by the machine. Phase B makes
one call per ticker and a shared rate limiter hands out evenly spaced
slots at `runtime.history_rate_per_min` (295, just under Starter's 300),
so the run takes `tickers ÷ 295` minutes no matter how fast FMP answers
on the day. `history_workers` is **not** the throttle — it only caps how
many calls may be in flight, and 8 is plenty.

The end of Phase B logs the rate actually achieved. If it lands well
under the ceiling, raise `history_workers`. If you see
`fmp historical rate-limited` in the log, lower `history_rate_per_min`.

Widening the history window to 252 sessions for the 52-week low costs
**nothing here** — it is the same one call per ticker with a wider date
range, and the limiter, not the payload, sets the pace.

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
| 1. Data infrastructure | `stage1_data/` (Phase A FMP `company-screener` for the NASDAQ+NYSE >= $1B universe, Phase B FMP OHLCV with strict validation + liquidity gate — market cap >= $1B **and** 14-session average volume > 1M shares, Phase C SMA20 / Wilder ATR% / Lambert CCI14 / open gaps) |
| 2. Seven raw checks | `stage2_checks/` (trend, candle + 11 formations, 7-day volume, SMA20 position, gaps vs price, CCI status, 52-week-low proximity) |
| 3. Category AND filter | `stage3_filter/filter.py` — Categories 2-7 decide; Category 1 (trend) is computed but disabled |

## What actually decides

A ticker reaches Telegram only if it clears **every** active category.
It is a plain AND — one failure and it is out, with no rescue path and
no scoring. Every passing ticker is sent, unranked and uncapped.

| # | Category | Passes when |
|---|---|---|
| 1 | Trend | **disabled** — computed and reported, never rejects |
| 2 | Candle | last candle is green **and** matches one of 11 bullish formations |
| 3 | Volume | one of three accepted 5-day volume stories holds, and it is not the fifth green day running |
| 4 | SMA 20 | less than 2.0 ATR% above the average, **or** more than 1.5 ATR% below it |
| 5 | Gaps | the **nearest** open gap is above the price (or the price is inside one, or there are none) |
| 6 | CCI 14 | rising, and between −120 and +130 |
| 7 | 52-week low | the lowest low of the last 10 sessions is at least 20% above the 52-week low |

Ahead of all of it, Stage 1 admits only NASDAQ/NYSE symbols above $1B
market cap with a 14-session average volume over 1M shares, 60 complete
sessions of data, and finite indicators.

### Diagnostics

`explain.py` prints the numbers behind Categories 4 and 7 per ticker,
with the verdict and the reason. It reads only — no CSV, no Telegram:

```bash
python explain.py --config config.local.yaml
```

Point `runtime.test_tickers` at a handful of symbols first; leave it
empty to explain the whole universe.

### Category 4 — distance from the SMA 20

The distance is read the way it looks on a chart: the gap between the
close and the SMA 20 as a **percent of price**, divided by **ATR(14) as a
percent of price**. One threshold then means the same thing on a quiet
stock and on a volatile one.

| Close vs SMA 20 | Result | Why |
|---|---|---|
| **2.0 ATR% or more above** | rejected | the move already happened — we do not chase |
| **less than 2.0 ATR% above** | **passes** | healthy zone, the average is holding it up |
| **on the average, or up to 1.5 ATR% below** | rejected | stuck — the average is a ceiling it has not broken |
| **more than 1.5 ATR% below** | **passes** | far enough that the average acts as a magnet |

Both boundaries are exclusive on the passing side, and a close sitting
exactly on the average is rejected. Tune them via `runtime.sma20_max_atr_above`
and `runtime.sma20_min_atr_below` in `config.yaml` — no code change needed.

`SMA20_Role` and `SMA20_Breakout` are still computed and still written to
the CSV and the Telegram message, but they no longer affect the decision.

### Category 5 — the nearest gap decides

Category 5 used to pass the moment any open gap sat above the price, no
matter how far. A gap 15 ATR overhead could excuse one half an ATR
underfoot — and it is the near one that actually pulls the price.

Every open gap is now measured to its **near edge** — the first level the
price would touch — and the closest one carries the verdict:

| Situation | Result |
|---|---|
| price is inside a gap | passes (it is filling it now; no magnet left) |
| no open gaps at all | passes |
| nearest gap is **above** | passes — room overhead |
| nearest gap is **below** | **rejected**, whatever sits above it |

There is no distance threshold: a gap below rejects at any range, which
is how this category already treated a lone gap below. An exact tie
resolves to below.

Distance is reported in ATR units (`Gap_Nearest_ATR`), but the ranking
uses the raw price distance — every gap on a ticker divides by the same
ATR, so the order is identical either way, and ranking on the raw figure
keeps the check well-defined if ATR ever collapses to zero.

Note that gaps are only found inside the 60-session analysis window; one
that formed before it does not exist as far as this category is
concerned.

### Category 7 — distance from the 52-week low

Rejects stocks camped on their lows. A stock in a downtrend prints the
same green candles and volume patterns as one in a pullback; nothing in
Categories 2-6 can tell them apart.

The check takes the **lowest low of the last 10 sessions** — not the last
close — so a stock that tagged its low last week and has since bounced is
still caught. That low must sit at least **20% above the 52-week low**:

```
percent above = (recent low - 52-week low) / 52-week low * 100
```

Below the threshold, the ticker is rejected outright. Like every other
category this is a hard AND — there is no rescue path.

The 52-week low costs **no extra API call**. Phase B already makes one
`historical-price-eod/full` request per ticker; it now asks for 252
sessions instead of 60 in that same request (FMP Starter allows 5 years
per call). The extra sessions are used **only** to locate the low —
every indicator and every other category still runs on the last 60
sessions, because widening the analysis window would make Category 5
count year-old gaps and flip verdicts.

A ticker listed less than 52 weeks ago is **not** dropped: its low comes
from whatever history exists, and `Low_52W_Sessions` in the CSV reports
how many sessions that was.

Tune via `runtime.low52w_min_pct_above` and
`runtime.low52w_lookback_sessions` in `config.yaml`.
| 4. CSV + Telegram | `stage4_output/csv_writer.py`, `stage4_output/telegram_sender.py` |
