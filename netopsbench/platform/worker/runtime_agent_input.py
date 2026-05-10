"""Runtime agent input contract helpers.

This module builds the public diagnostic payload passed to agents during
runtime execution. It intentionally strips any fault-injection labels/targets
so agents only see observable symptoms.
"""

from __future__ import annotations

import hashlib
from typing import Any

_EPISODE_ALLOWED_KEYS = {
    "episode_id",
    "duration_seconds",
    "stabilization_time",
}


def build_public_case_id(*, scenario_id: str, episode_result: dict[str, Any]) -> str:
    """Return a stable, non-semantic case id for agent context."""
    episode = episode_result.get("episode", {}) if isinstance(episode_result, dict) else {}
    episode_id = episode.get("episode_id") if isinstance(episode, dict) else None
    source = f"{scenario_id}:{episode_id or 'unknown'}"
    digest = hashlib.sha1(source.encode("utf-8")).hexdigest()[:12]
    return f"case-{digest}"


def build_public_symptoms(*, episode_result: dict[str, Any], pingmesh_query_window: dict[str, Any]) -> dict[str, Any]:
    """Build sanitized symptoms payload for agents."""
    episode = episode_result.get("episode", {}) if isinstance(episode_result, dict) else {}
    observations = episode_result.get("observations", {}) if isinstance(episode_result, dict) else {}

    safe_episode = {}
    if isinstance(episode, dict):
        safe_episode = {key: episode.get(key) for key in _EPISODE_ALLOWED_KEYS if key in episode}

    return {
        "episode": safe_episode,
        "observations": observations if isinstance(observations, dict) else {},
        "pingmesh_query_window": pingmesh_query_window if isinstance(pingmesh_query_window, dict) else {},
        "observation_type": "scenario_episode",
    }
