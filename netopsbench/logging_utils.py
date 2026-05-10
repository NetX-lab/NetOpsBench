"""Centralized NetOpsBench logging configuration helpers."""

from __future__ import annotations

import logging
import os

_DEFAULT_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
_DEFAULT_DATEFMT = "%Y-%m-%d %H:%M:%S"
_CONFIGURED = False


def _resolve_level(level: str | None = None) -> int:
    raw = str(level or os.environ.get("NETOPSBENCH_LOG_LEVEL", "INFO")).strip().upper()
    return getattr(logging, raw, logging.INFO)


def configure_logging(*, level: str | None = None, force: bool = False) -> None:
    """Configure the shared ``netopsbench`` logger tree."""
    global _CONFIGURED
    if _CONFIGURED and not force:
        return

    logger = logging.getLogger("netopsbench")
    logger.setLevel(_resolve_level(level))
    logger.propagate = False

    if force:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)

    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setLevel(_resolve_level(level))
        handler.setFormatter(logging.Formatter(_DEFAULT_FORMAT, _DEFAULT_DATEFMT))
        logger.addHandler(handler)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a logger under the shared NetOpsBench logger tree."""
    configure_logging()
    logger_name = name if name.startswith("netopsbench") else f"netopsbench.{name}"
    return logging.getLogger(logger_name)
