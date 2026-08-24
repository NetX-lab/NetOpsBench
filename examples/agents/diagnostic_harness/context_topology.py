"""Shared loading of operator-visible topology inventory from a diagnosis context."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from netopsbench.models.topology import TopologyManifest
from netopsbench.platform.topology.topology_utils import load_topology_manifest


def load_context_manifest(
    context: Any,
    *,
    include_context_payload: bool = True,
) -> tuple[TopologyManifest | None, str | None]:
    """Load the worker inventory first, then the public context projection."""
    metadata = getattr(context, "metadata", {}) or {}
    worker_env = metadata.get("worker_env") if isinstance(metadata, Mapping) else None
    topology_dir = worker_env.get("NETOPSBENCH_TOPOLOGY_DIR") if isinstance(worker_env, Mapping) else None
    if topology_dir:
        source = Path(str(topology_dir)) / "topology.json"
        try:
            return load_topology_manifest(source), str(source)
        except (OSError, TypeError, ValueError):
            pass
    if not include_context_payload:
        return None, None
    try:
        return TopologyManifest.model_validate(getattr(context, "topology", {}) or {}), "context_manifest"
    except (TypeError, ValueError):
        return None, None


__all__ = ["load_context_manifest"]
