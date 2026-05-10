"""Shared helpers for worker runtime orchestration."""

from __future__ import annotations

from netopsbench.logging_utils import get_logger

logger = get_logger(__name__)


class WorkerRuntimeError(RuntimeError):
    """Raised for user-facing worker runtime setup errors."""


# Alias retained for existing imports.
ParallelBenchmarkError = WorkerRuntimeError


def _safe_label(value: str) -> str:
    text = (value or "unknown").strip().lower()
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in text) or "unknown"


# Third-octet base for worker management subnets: 172.31.{base+worker_idx}.0/24
# Each scale tier gets a 20-address range so workers from different scales
# never overlap even if multiple tiers are deployed simultaneously.
#   xs:     172.31.100-119.0/24
#   small:  172.31.120-139.0/24
#   medium: 172.31.140-159.0/24
#   large:  172.31.160-179.0/24
_SCALE_SUBNET_BASE = {
    "xs": 100,
    "small": 120,
    "medium": 140,
    "large": 160,
}
