"""Observation phase of the shared incident execution engine."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from netopsbench.logging_utils import get_logger
from netopsbench.models.scenario import EpisodeSpec

logger = get_logger(__name__)


def observe_episode(
    executor: Any,
    episode: EpisodeSpec,
    *,
    baseline_window: dict[str, Any],
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Activate one episode and collect its diagnostic observation window.

    Fault recovery intentionally does not happen here. ``IncidentEngine.close``
    owns recovery for both benchmark and simulator callers.
    """
    result = {} if result is None else result
    result.update(
        {
            "episode_id": episode.episode_id,
            "description": episode.description,
            "episode": episode.model_dump(mode="json", exclude_none=True),
            "start_time": datetime.now(UTC).isoformat(),
            "success": False,
            "state": "preparing",
        }
    )

    if episode.is_healthy:
        logger.info("[Healthy Observation] Monitoring without fault injection")
        executor.timings.skip("injection", "healthy scenario")
    else:
        with executor.timings.measure("injection", timeout_seconds=executor.observation_timeout_seconds):
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
    stabilization_seconds = 0 if episode.is_healthy else max(0, int(episode.stabilization_time))
    steady_seconds = episode.duration_seconds - early_seconds - stabilization_seconds
    if steady_seconds < 1:
        raise ValueError(
            "Fault observation budget must leave at least one steady-state second: "
            f"duration={episode.duration_seconds}, early={early_seconds}, "
            f"stabilization={stabilization_seconds}"
        )

    # A single capture; early/steady are slices of the same queried records.
    with executor.timings.measure("observation", planned_seconds=episode.duration_seconds):
        window = executor._capture_observation_window(episode.duration_seconds, "full")
    start = datetime.fromisoformat(window["start_time"].replace("Z", "+00:00"))
    windows = []
    if early_seconds:
        windows.append(
            {
                **window,
                "name": "early",
                "end_time": (start + timedelta(seconds=early_seconds)).isoformat(),
                "duration_seconds": early_seconds,
            }
        )
    windows.append(
        {
            **window,
            "name": "steady",
            "start_time": (start + timedelta(seconds=early_seconds + stabilization_seconds)).isoformat(),
            "duration_seconds": steady_seconds,
        }
    )
    with executor.timings.measure("observation_query", timeout_seconds=executor.observation_timeout_seconds):
        observations = executor._merge_observation_windows(
            windows, total_duration_seconds=episode.duration_seconds, baseline_window=baseline_window
        )
    if episode.is_healthy:
        executor._offer_healthy_window(observations)
    coverage = observations.pop("_coverage_audit", None)
    observations.pop("_baseline_health", None)
    result["observations"] = observations
    if coverage is not None:
        result["coverage_audit"] = coverage
    result["state"] = "active"
    return result


__all__ = ["observe_episode"]
