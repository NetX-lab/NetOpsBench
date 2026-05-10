"""Backward-compatible re-export shim.

The builtin spec aggregator now lives in :mod:`netopsbench.platform.faults.specs`.
This module is kept solely so that any external code importing
``register_builtin_fault_specs`` from the historic location continues to work.
"""

from __future__ import annotations

from .specs import register_builtin_fault_specs

__all__ = ["register_builtin_fault_specs"]
