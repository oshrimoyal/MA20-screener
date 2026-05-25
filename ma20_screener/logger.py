"""Centralized logger. Writes the same records to stdout AND a per-run log file.

Per the document: every stage start, ticker counts entering/passing,
per-ticker failure reason, and end-of-run summary must be emitted to both
sinks.
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

_LOGGER_NAME = "ma20_screener"
_initialized = False
_log_file_path: Path | None = None


def setup_logger(log_dir: Path) -> Path:
    """Create the run-scoped log file and attach handlers. Returns the log
    file path. Safe to call once at the start of each run."""
    global _initialized, _log_file_path
    if _initialized:
        assert _log_file_path is not None
        return _log_file_path

    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = log_dir / f"run_{stamp}.log"

    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(fmt)
    logger.addHandler(stream_handler)

    _initialized = True
    _log_file_path = log_file
    return log_file


def get_logger() -> logging.Logger:
    return logging.getLogger(_LOGGER_NAME)


def log_stage_start(name: str) -> None:
    get_logger().info("=" * 8 + f" {name} START " + "=" * 8)


def log_stage_summary(name: str, entered: int, passed: int) -> None:
    get_logger().info(
        f"{name} SUMMARY: entered={entered} passed={passed} rejected={entered - passed}"
    )


def log_ticker_fail(ticker: str, reason: str) -> None:
    get_logger().info(f"{ticker} — FAIL: {reason}")
