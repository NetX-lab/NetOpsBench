"""CLI package for NetOpsBench.

Keep exports lazy to avoid importing ``netopsbench.cli.main`` during package
initialization. This prevents ``python -m netopsbench.cli.main`` from emitting
runpy duplicate-module RuntimeWarning.
"""

from __future__ import annotations

from typing import Any


def build_parser(*args: Any, **kwargs: Any):
    from .main import build_parser as _build_parser

    return _build_parser(*args, **kwargs)


def main(*args: Any, **kwargs: Any):
    from .main import main as _main

    return _main(*args, **kwargs)


__all__ = ["build_parser", "main"]
