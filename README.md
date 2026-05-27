# MA20 Screener

A daily long-side stock screener for the S&P 500. The system applies a
6-category filter to every closed-candle session and emits both a
per-run CSV (all stocks, passed + rejected) and a Telegram broadcast of
the day's candidates.

For installation, configuration and operation, see **[`GUIDE.md`](GUIDE.md)**
(written in Hebrew for the operator).

## Quickstart for developers

```bash
pip install -r requirements.txt
# Edit config.yaml (telegram token/chat_id; optional test_tickers).
python main.py
```

Pipeline stages live in `ma20_screener/`:

| Stage | Module |
|---|---|
| 1. Data infrastructure | `stage1_data/` (Phase A S&P 500 universe + SEC EDGAR exchange labels, Phase B FMP OHLCV + marketCap with strict validation, Phase C SMA20 / Wilder ATR% / Lambert CCI14 / open gaps) |
| 2. Six raw checks | `stage2_checks/` (trend, candle + 11 formations, 7-day volume, SMA20 position, gaps vs price, CCI status) |
| 3. Six-category AND filter | `stage3_filter/filter.py` |
| 4. CSV + Telegram | `stage4_output/csv_writer.py`, `stage4_output/telegram_sender.py` |
