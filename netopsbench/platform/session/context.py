"""Runtime diagnosis context helpers used by session execution."""

from __future__ import annotations

import glob
import json
import os
from typing import Any

from netopsbench.logging_utils import get_logger

# Internal platform code can call AgentToolkit directly; external integrations should use SDK MCP helpers.
from netopsbench.platform.toolkit.toolkit import AgentToolkit

logger = get_logger(__name__)


def build_topology_snapshot(toolkit: AgentToolkit) -> dict:
    topology_result = toolkit.get_topology()
    if hasattr(topology_result, "success") and topology_result.success:
        return topology_result.data
    return {"devices": {}}


def _build_toolkit_for_topology(topology_dir: str) -> AgentToolkit:
    topology_file = os.path.join(topology_dir, "dcn.clab.yaml")
    if not os.path.exists(topology_file):
        candidates = sorted(glob.glob(os.path.join(topology_dir, "*.clab.y*ml")))
        if candidates:
            topology_file = candidates[0]
    topology_metadata = None
    topology_json = os.path.join(topology_dir, "topology.json")
    if os.path.exists(topology_json):
        try:
            with open(topology_json, encoding="utf-8") as f:
                topology_metadata = json.load(f)
        except Exception:
            logger.debug("failed to parse topology.json at %s", topology_json, exc_info=True)
            topology_metadata = None
    toolkit_kwargs: dict[str, Any] = {}
    if os.path.exists(topology_file):
        toolkit_kwargs["topology_file"] = topology_file
    if isinstance(topology_metadata, dict):
        toolkit_kwargs["topology_metadata"] = topology_metadata
    return AgentToolkit(**toolkit_kwargs)


def _extract_episode_pingmesh_query_window(episode_result: dict[str, Any]) -> dict[str, str | None]:
    observations = episode_result.get("observations", {}) if isinstance(episode_result, dict) else {}
    if not isinstance(observations, dict):
        return {"start_time": None, "end_time": None}
    start_time = observations.get("start_time")
    end_time = observations.get("end_time")
    if isinstance(start_time, str) and isinstance(end_time, str) and start_time and end_time:
        return {"start_time": start_time, "end_time": end_time}
    pingmesh_metrics = observations.get("pingmesh_metrics", {}) if isinstance(observations, dict) else {}
    windows = pingmesh_metrics.get("windows", {}) if isinstance(pingmesh_metrics, dict) else {}
    if isinstance(windows, dict) and windows:
        current_starts = []
        current_ends = []
        for window in windows.values():
            if not isinstance(window, dict):
                continue
            current = window.get("current", {})
            if not isinstance(current, dict):
                continue
            start_candidate = current.get("start")
            end_candidate = current.get("end")
            if isinstance(start_candidate, str) and start_candidate:
                current_starts.append(start_candidate)
            if isinstance(end_candidate, str) and end_candidate:
                current_ends.append(end_candidate)
        if current_starts and current_ends:
            return {"start_time": min(current_starts), "end_time": max(current_ends)}
    return {"start_time": None, "end_time": None}
