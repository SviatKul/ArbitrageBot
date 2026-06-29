"""Loguru configuration."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from loguru import logger


def setup_logging(level: str = "INFO", *, serialize: bool = False) -> None:
    """
    Configure loguru for CLI / long-running process use.

    Removes default handler and adds stderr with level and optional JSON serialization.
    """
    logger.remove()
    logger.add(
        sys.stderr,
        level=level.upper(),
        serialize=serialize,
        backtrace=False,
        diagnose=False,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
            "<level>{message}</level>"
        ),
    )


def get_logger(name: Optional[str] = None):
    """Return a loguru logger bound with an optional context name."""
    if name:
        return logger.bind(component=name)
    return logger


def setup_logging_rotating(
    level: str = "INFO",
    *,
    log_directory: Path,
    retention_days: int = 14,
    serialize: bool = False,
) -> None:
    """
    Stderr sink plus daily-rotated file under ``log_directory`` (rotation at midnight UTC).

    Retention deletes files older than ``retention_days``.
    """
    log_directory.mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.add(
        sys.stderr,
        level=level.upper(),
        serialize=serialize,
        backtrace=False,
        diagnose=False,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
            "<level>{message}</level>"
        ),
    )
    logger.add(
        str(log_directory / "bot.log"),
        level=level.upper(),
        serialize=serialize,
        rotation="00:00",
        retention=f"{retention_days} days",
        encoding="utf-8",
        format=(
            "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} | {message}"
        ),
    )
