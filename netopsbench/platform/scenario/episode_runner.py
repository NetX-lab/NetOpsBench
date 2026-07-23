"""Observation phase of the shared incident execution engine."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from netopsbench.logging_utils import get_logger
from netopsbench.models.scenario import EpisodeSpec

logger = get_logger(__name__)


def observe_episode(
    executor: Any,
    episode: EpisodeSpec,
    *,
    baseline_window: dict[str, Any],
) -> dict[str, Any]:
    """Activate one episode and collect its diagnostic observation window.

    Fault recovery intentionally does not happen here. ``IncidentEngine.close``
    owns recovery for both benchmark and simulator callers.
    """
    result: dict[str, Any] = {
        "episode_id": episode.episode_id,
        "description": episode.description,
        "episode": episode.model_dump(mode="json", exclude_none=True),
        "start_time": datetime.now(UTC).isoformat(),
        "success": False,
        "state": "preparing",
    }

    if episode.is_healthy:
        logger.info("[Healthy Observation] Monitoring without fault injection")
        observations = executor._wait_and_observe(
            episode.duration_seconds,
            baseline_window=baseline_window,
        )
        coverage = observations.pop("_coverage_audit", None)
        result["observations"] = observations
        if coverage is not None:
            result["coverage_audit"] = coverage
        result["state"] = "active"
        return result

    injection = executor._inject_fault(episode)
    result["injection"] = injection
    if not injection.get("success"):
        raise RuntimeError(f"Fault injection failed: {injection}")

    early_seconds = int(
        episode.metadata.get(
            "early_observation_seconds",
            min(20, max(10, episode.duration_seconds // 3)),
        )
    )
    if early_seconds >= episode.duration_seconds:
        early_seconds = max(0, episode.duration_seconds - 10)
    steady_seconds = max(1, episode.duration_seconds - early_seconds)

    windows: list[dict[str, Any]] = []
    if early_seconds:
        windows.append(executor._capture_observation_window(early_seconds, "early"))
    _sleep(executor, episode.stabilization_time)
    windows.append(executor._capture_observation_window(steady_seconds, "steady"))

    observations = executor._merge_observation_windows(
        windows,
        total_duration_seconds=episode.duration_seconds,
        baseline_window=baseline_window,
    )
    coverage = observations.pop("_coverage_audit", None)
    result["observations"] = observations
    if coverage is not None:
        result["coverage_audit"] = coverage
    result["state"] = "active"
    return result


def _sleep(executor: Any, seconds: float) -> None:
    getattr(executor, "sleep", time.sleep)(seconds)


__all__ = ["observe_episode"]
