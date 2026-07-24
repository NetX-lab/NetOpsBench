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

    def as_env(self) -> dict[str, str]:
        return {
            "NETOPSBENCH_TOPOLOGY_DIR": str(self.topology_dir),
            "NETOPSBENCH_INFLUXDB_BUCKET": self.influxdb_bucket,
        }


__all__ = ["WorkerExecutionContext"]
