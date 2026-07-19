"""Shared interactive episode kernel used by sessions and RL environments."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from netopsbench.logging_utils import get_logger
from netopsbench.models.scenario import EpisodeSpec

logger = get_logger(__name__)


def observe_episode(
    executor: Any,
    episode: EpisodeSpec,
    *,
    baseline_window: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Activate one episode and collect its diagnostic observation window.

    Fault recovery intentionally does not happen here. Interactive simulators
    keep the episode active while tools are called; benchmark sessions call
    :func:`finish_episode` immediately after agent diagnosis.
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
        result["observations"] = executor._wait_and_observe(
            episode.duration_seconds,
            baseline_window=baseline_window,
        )
        result["state"] = "active"
        return result

    pre_fault_reference = datetime.now(UTC).replace(microsecond=0)
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
        baseline_end_time=pre_fault_reference,
        baseline_window=baseline_window,
    )
    coverage = observations.pop("_coverage_audit", None)
    result["observations"] = observations
    if coverage is not None:
        result["coverage_audit"] = coverage
    result["state"] = "active"
    return result


def finish_episode(
    executor: Any,
    episode: EpisodeSpec,
    episode_result: dict[str, Any],
    diagnosis_callback: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Record diagnosis, recover the active fault, and close the episode."""
    if diagnosis_callback is not None:
        try:
            episode_result["diagnosis"] = diagnosis_callback(episode_result)
        except Exception as exc:  # noqa: BLE001 - an agent failure is a scored outcome
            episode_result["diagnosis"] = {
                "error": str(exc),
                "success": False,
                "metadata": {
                    "agent_failure_stage": "diagnose",
                    "error_type": type(exc).__name__,
                },
            }
    if not episode.is_healthy:
        episode_result["recovery"] = executor._recover_fault()
        _sleep(executor, executor.post_recovery_wait_seconds)
    episode_result["success"] = True
    episode_result["state"] = "terminal"
    episode_result["end_time"] = datetime.now(UTC).isoformat()
    return episode_result


def abort_episode(executor: Any, episode: EpisodeSpec) -> None:
    """Best-effort cleanup for a partially prepared episode."""
    if not episode.is_healthy:
        executor._recover_fault()


def run_episode(
    executor: Any,
    episode: EpisodeSpec,
    diagnosis_callback: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run the shared kernel to completion for a benchmark session."""
    logger.info("Episode: %s", episode.episode_id)
    try:
        result = observe_episode(executor, episode)
        return finish_episode(executor, episode, result, diagnosis_callback)
    except Exception:
        try:
            abort_episode(executor, episode)
        except Exception:
            logger.warning("Episode recovery failed", exc_info=True)
        raise


def _sleep(executor: Any, seconds: float) -> None:
    getattr(executor, "sleep", time.sleep)(seconds)


__all__ = ["abort_episode", "finish_episode", "observe_episode", "run_episode"]
