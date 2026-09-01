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
    # Phase B (FMP Starter tier) concurrency profile. Starter is 300
    # calls/minute with no daily cap. Phase A makes 2 screener calls;
    # Phase B makes 1 historical call per ticker × ~1,900 NASDAQ+NYSE
    # tickers = ~1,900 calls. The defaults yield ~4 calls/sec
    # (~240/min), safely under the 300/min ceiling.
    history_workers: int
    history_sleep_ms: int          # ignored when history_rate_per_min > 0
    history_rate_per_min: float    # 0 disables the limiter (sleep mode)
    history_retries: int          # attempts AFTER the first try
    history_retry_delay_s: float  # initial delay for non-429 errors;
                                  # 429 errors back off harder.
    # Universe / validation. A ticker must clear BOTH thresholds in
    # Phase B: marketCap >= min_market_cap_usd AND the 14-session mean
    # volume strictly greater than min_volume_ma.
    min_market_cap_usd: float
    min_volume_ma: float
    history_trading_days: int
    # Trading days pulled per ticker. Same single FMP call, wider date
    # range — no extra calls. Used ONLY to locate the 52-week low; every
    # indicator and category still runs on `history_trading_days`.
    history_long_trading_days: int
    # Category 7 (distance from the 52-week low). The lowest low of the
    # last `low52w_lookback_sessions` sessions must sit at least
    # `low52w_min_pct_above` percent above the 52-week low.
    low52w_lookback_sessions: int
    low52w_min_pct_above: float
    test_tickers: list[str]
    # FMP API key (Starter tier or higher — Basic free tier is too
    # constrained at this universe size). Validated by `load_config`.
    fmp_api_key: str


@dataclass(frozen=True)
class AppConfig:
    telegram: TelegramConfig
    paths: PathsConfig
    runtime: RuntimeConfig


_FMP_API_KEY_PLACEHOLDER = "PUT-YOUR-FMP-API-KEY-HERE"


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

    fmp_api_key = str(_require(rt_raw, "fmp_api_key", "runtime")).strip()
    if not fmp_api_key or fmp_api_key == _FMP_API_KEY_PLACEHOLDER:
        raise ValueError(
            "runtime.fmp_api_key is still the placeholder. Register "
            "for free at https://site.financialmodelingprep.com/register, "
            "copy the API key from the dashboard, and paste it into "
            "config.yaml."
        )
    if len(fmp_api_key) < 8:
        raise ValueError(
            "runtime.fmp_api_key looks too short to be a real FMP token. "
            "Check the value in config.yaml."
        )

    runtime = RuntimeConfig(
        history_workers=int(rt_raw.get("history_workers", 1)),
        history_sleep_ms=int(rt_raw.get("history_sleep_ms", 250)),
        history_rate_per_min=float(rt_raw.get("history_rate_per_min", 295)),
        history_retries=int(rt_raw.get("history_retries", 3)),
        history_retry_delay_s=float(rt_raw.get("history_retry_delay_s", 5.0)),
        min_market_cap_usd=float(_require(rt_raw, "min_market_cap_usd", "runtime")),
        # Defaults to 1,000,000 shares so configs written before this
        # threshold existed keep the intended behaviour.
        min_volume_ma=float(rt_raw.get("min_volume_ma", 1_000_000)),
        history_trading_days=int(_require(rt_raw, "history_trading_days", "runtime")),
        # Defaults match the shipped config.yaml so a config written
        # before these keys existed keeps working.
        history_long_trading_days=int(rt_raw.get("history_long_trading_days", 252)),
        low52w_lookback_sessions=int(rt_raw.get("low52w_lookback_sessions", 10)),
        low52w_min_pct_above=float(rt_raw.get("low52w_min_pct_above", 10.0)),
        test_tickers=test_tickers,
        fmp_api_key=fmp_api_key,
    )

    return AppConfig(telegram=telegram, paths=paths, runtime=runtime)
