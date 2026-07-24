"""Worker execution context helpers used by benchmark session dispatch."""

from __future__ import annotations

from pathlib import Path

from netopsbench.models.runtime import RuntimeIdentity
from netopsbench.platform.session.types import WorkerExecutionContext


def build_worker_execution_context(worker: RuntimeIdentity, topology_dir: Path) -> WorkerExecutionContext:
    """Build an execution context from the worker's canonical runtime identity."""
    resolved_topology_dir = Path(topology_dir)
    if resolved_topology_dir.resolve() != worker.topology_dir.resolve():
        raise ValueError(
            f"Worker topology directory {resolved_topology_dir} does not match runtime identity "
            f"{worker.topology_dir}"
        )
    return WorkerExecutionContext(
        topology_dir=resolved_topology_dir,
        topology_id=worker.topology_id,
        influxdb_bucket=worker.bucket,
    )


__all__ = ["build_worker_execution_context"]
