"""Canonical topology loading and Containerlab naming helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from netopsbench.models.topology import SCHEMA_VERSION, TopologyManifest

NETWORK_DEVICE_PREFIXES: tuple[str, ...] = ("spine", "leaf", "core", "agg", "edge")
TopologyInput = TopologyManifest | dict[str, Any]


def clab_container_name(lab_name: str, device_name: str) -> str:
    return f"clab-{lab_name}-{device_name}"


def coerce_topology_manifest(topology: TopologyInput) -> TopologyManifest:
    """Validate a canonical topology value without legacy fallback."""
    if isinstance(topology, TopologyManifest):
        return topology
    if not isinstance(topology, dict):
        raise TypeError("Topology must be a TopologyManifest or schema-v4 dictionary")
    schema_version = topology.get("schema_version")
    if schema_version != SCHEMA_VERSION:
        rendered = "missing" if schema_version is None else repr(schema_version)
        raise ValueError(
            f"Unsupported topology schema_version {rendered}; expected {SCHEMA_VERSION!r}. "
            "Regenerate topology.json with the current topology generator."
        )
    try:
        manifest = TopologyManifest.model_validate(topology)
        plan = manifest.require_pingmesh_plan()
        from netopsbench.platform.pingmesh.planner import plan_fingerprint

        if plan.settings.plan_id != plan_fingerprint(manifest, plan):
            raise ValueError("Stale Pingmesh plan fingerprint; regenerate topology.json")
        return manifest
    except ValidationError as exc:
        raise ValueError(f"Invalid topology schema_version {SCHEMA_VERSION!r} manifest: {exc}") from exc


def load_topology_manifest(path: str | Path) -> TopologyManifest:
    """Load ``topology.json`` from a file or generated topology directory."""
    source = Path(path)
    if source.is_dir():
        source = source / "topology.json"
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Failed to read canonical topology manifest {source}: {exc}") from exc
    try:
        return coerce_topology_manifest(payload)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Failed to load canonical topology manifest {source}: {exc}") from exc


def is_network_device_name(device: str) -> bool:
    return str(device or "").startswith(NETWORK_DEVICE_PREFIXES)


__all__ = [
    "NETWORK_DEVICE_PREFIXES",
    "TopologyInput",
    "clab_container_name",
    "coerce_topology_manifest",
    "is_network_device_name",
    "load_topology_manifest",
]
