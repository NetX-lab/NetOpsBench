"""Explicit worker execution context helpers for session execution."""

from __future__ import annotations

from pathlib import Path

from netopsbench.platform.session.types import WorkerExecutionContext
from netopsbench.platform.worker.pool import WorkerSpec


def build_worker_execution_context(worker: WorkerSpec, topology_dir: Path) -> WorkerExecutionContext:
    resolved_topology_dir = Path(topology_dir)
    return WorkerExecutionContext(
        topology_dir=resolved_topology_dir,
        topology_id=resolved_topology_dir.name,
        influxdb_bucket=str(worker.bucket or ""),
    )
