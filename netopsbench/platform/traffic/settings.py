"""Traffic-planning limits."""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_SWITCH_PPS_LIMIT = 5000


@dataclass(frozen=True)
class TrafficSettings:
    switch_pps_limit: int | None = DEFAULT_SWITCH_PPS_LIMIT


__all__ = [
    "DEFAULT_SWITCH_PPS_LIMIT",
    "TrafficSettings",
]
