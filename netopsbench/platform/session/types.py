"""Internal session-layer execution contracts and payload references."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WorkerExecutionContext:
    """Explicit per-worker runtime context for session execution without mutating process env."""

    topology_dir: Path
    topology_id: str
    influxdb_bucket: str


__all__ = ["WorkerExecutionContext"]
