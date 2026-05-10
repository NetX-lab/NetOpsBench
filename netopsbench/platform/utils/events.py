"""Unified structured-logging helper for platform modules."""

from __future__ import annotations

import logging


def emit(*args, level: str = "info", logger: logging.Logger | None = None, **kwargs) -> None:
    """Log a message at the given level, joining all positional args with spaces."""
    target = logger or logging.getLogger(__name__)
    message = " ".join(str(arg) for arg in args)
    getattr(target, level, target.info)(message)
