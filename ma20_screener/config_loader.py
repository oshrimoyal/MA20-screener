"""Load and validate the YAML configuration file."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class TelegramConfig:
    token: str
    chat_id: str


@dataclass(frozen=True)
class PathsConfig:
    csv_dir: Path
    log_dir: Path


@dataclass(frozen=True)
class RuntimeConfig:
    # Phase A (marketCap via yfinance.fast_info — quote endpoint).
    workers: int
    fetch_sleep_ms: int
    # Phase A retry profile (recovers transient YFRateLimitError /
    # JSONDecodeError on legitimate stocks that briefly hit Yahoo's
    # rate limit).
    marketcap_retries: int           # attempts AFTER the first try
    marketcap_retry_delay_s: float   # initial delay, doubles each retry
    # Phase B (OHLCV via yfinance.Ticker.history — chart endpoint).
    # Yahoo's chart endpoint rate-limits aggressively, so Phase B uses
    # lower concurrency and longer sleeps than Phase A by default.
    history_workers: int
    history_sleep_ms: int
    history_retries: int          # attempts AFTER the first try
    history_retry_delay_s: float  # initial delay, doubles each retry
    # Universe / validation.
    min_market_cap_usd: float
    history_trading_days: int
    test_tickers: list[str]


@dataclass(frozen=True)
class AppConfig:
    telegram: TelegramConfig
    paths: PathsConfig
    runtime: RuntimeConfig


def _require(d: dict, key: str, parent: str) -> object:
    if key not in d:
        raise ValueError(f"Missing required config key: {parent}.{key}")
    return d[key]


def load_config(path: str | os.PathLike = "config.yaml") -> AppConfig:
    """Read the YAML config file, validate required keys, and ensure output
    directories exist."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Config file not found: {p.resolve()}")
    with p.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    tg_raw = _require(raw, "telegram", "root")
    paths_raw = _require(raw, "paths", "root")
    rt_raw = _require(raw, "runtime", "root")

    telegram = TelegramConfig(
        token=str(_require(tg_raw, "token", "telegram")),
        chat_id=str(_require(tg_raw, "chat_id", "telegram")),
    )

    csv_dir = Path(str(_require(paths_raw, "csv_dir", "paths"))).expanduser()
    log_dir = Path(str(_require(paths_raw, "log_dir", "paths"))).expanduser()
    csv_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    paths = PathsConfig(csv_dir=csv_dir, log_dir=log_dir)

    test_tickers_raw = rt_raw.get("test_tickers", "") or ""
    test_tickers = [t.strip().upper() for t in str(test_tickers_raw).split(",") if t.strip()]

    runtime = RuntimeConfig(
        workers=int(_require(rt_raw, "workers", "runtime")),
        fetch_sleep_ms=int(_require(rt_raw, "fetch_sleep_ms", "runtime")),
        # Phase A retry profile: 2 retries with 2 s initial delay (so
        # 2 s, then 4 s) recovers most legitimate stocks that briefly
        # hit a Yahoo rate-limit on the quote endpoint. fast_info is
        # less throttled than .history(), hence the lighter profile.
        marketcap_retries=int(rt_raw.get("marketcap_retries", 2)),
        marketcap_retry_delay_s=float(rt_raw.get("marketcap_retry_delay_s", 2.0)),
        # Phase B settings default to an even more conservative profile.
        # Yahoo's chart/history endpoint is rate-limited harder, so we
        # use 3 retries with 5 s initial delay (5 s, 10 s, 20 s = up to
        # 35 s of waiting per ticker) to push the success rate above 95 %.
        history_workers=int(rt_raw.get("history_workers", 3)),
        history_sleep_ms=int(rt_raw.get("history_sleep_ms", 500)),
        history_retries=int(rt_raw.get("history_retries", 3)),
        history_retry_delay_s=float(rt_raw.get("history_retry_delay_s", 5.0)),
        min_market_cap_usd=float(_require(rt_raw, "min_market_cap_usd", "runtime")),
        history_trading_days=int(_require(rt_raw, "history_trading_days", "runtime")),
        test_tickers=test_tickers,
    )

    return AppConfig(telegram=telegram, paths=paths, runtime=runtime)
