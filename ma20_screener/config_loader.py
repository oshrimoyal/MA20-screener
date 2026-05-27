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
    # Phase B (Stooq OHLCV) concurrency profile. Stooq's rate limit is
    # undocumented but generous at ~2 req/sec; we throttle in case of
    # transient errors and rely on retry-with-exponential-backoff.
    history_workers: int
    history_sleep_ms: int
    history_retries: int          # attempts AFTER the first try
    history_retry_delay_s: float  # initial delay, doubles each retry
    # Universe / validation.
    min_market_cap_usd: float
    history_trading_days: int
    test_tickers: list[str]
    # SEC EDGAR fair-access policy: every request MUST include a real
    # contact email in the User-Agent header. The operator must set
    # this in config.yaml. Stooq does not require contact info but a
    # polite UA is good practice.
    sec_user_agent: str
    stooq_user_agent: str


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

    sec_user_agent = str(_require(rt_raw, "sec_user_agent", "runtime")).strip()
    if "@" not in sec_user_agent:
        raise ValueError(
            "runtime.sec_user_agent must include a real contact email "
            "per SEC's fair-access policy. Example: "
            "'MA20-Screener you@example.com'."
        )
    stooq_user_agent = str(rt_raw.get("stooq_user_agent", sec_user_agent)).strip()

    runtime = RuntimeConfig(
        history_workers=int(rt_raw.get("history_workers", 3)),
        history_sleep_ms=int(rt_raw.get("history_sleep_ms", 500)),
        history_retries=int(rt_raw.get("history_retries", 3)),
        history_retry_delay_s=float(rt_raw.get("history_retry_delay_s", 5.0)),
        min_market_cap_usd=float(_require(rt_raw, "min_market_cap_usd", "runtime")),
        history_trading_days=int(_require(rt_raw, "history_trading_days", "runtime")),
        test_tickers=test_tickers,
        sec_user_agent=sec_user_agent,
        stooq_user_agent=stooq_user_agent,
    )

    return AppConfig(telegram=telegram, paths=paths, runtime=runtime)
