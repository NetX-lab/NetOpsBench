"""Per-episode execution helper extracted from :class:`ScenarioExecutor`.

Keeping the episode loop in its own class makes it easier to unit-test the
inject → observe → diagnose → recover state machine in isolation, and shrinks
``executor.py`` so it can focus on scenario-level orchestration.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from netopsbench.platform.utils.events import emit as _emit

from .models import Episode


@runtime_checkable
class EpisodeExecutionPort(Protocol):
    """Contract that :class:`EpisodeRunner` requires from its host executor.

    This Protocol exists to make the (historically implicit) coupling
    between :class:`EpisodeRunner` and :class:`ScenarioExecutor` explicit,
    so test stubs and future executors can satisfy a typed surface instead
    of duck-typing five underscore methods. The underscore names are
    preserved to keep the existing executor implementation source-compatible.
    """

    skip_none_episodes: bool
    post_recovery_wait_seconds: float

    def _build_skipped_episode_result(
        self,
        episode: Episode,
        *,
        start_time: str,
        end_time: str,
        waited_seconds: int,
    ) -> dict[str, Any]: ...

    def _inject_fault(self, episode: Episode) -> dict[str, Any]: ...

    def _wait_and_observe(
        self,
        seconds: int,
        *,
        baseline_end_time: datetime | None = None,
    ) -> dict[str, Any]: ...

    def _merge_observation_windows(
        self,
        windows: list[dict[str, Any]],
        *,
        total_duration_seconds: int,
    ) -> dict[str, Any]: ...

    def _recover_fault(self) -> dict[str, Any]: ...


class EpisodeRunner:
    """Run a single :class:`Episode` against an :class:`EpisodeExecutionPort`.

    The runner does not own state of its own — it delegates fault injection,
    observation, and recovery to the executor it was constructed with. This
    keeps backward-compatible behaviour while letting tests substitute a
    minimal stub executor that satisfies :class:`EpisodeExecutionPort`.
    """

    def __init__(self, executor: EpisodeExecutionPort):
        self.executor = executor

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(
        self,
        episode: Episode,
        diagnosis_callback: Callable[[dict], dict] | None = None,
        diagnose_if_skipped: bool = False,
    ) -> dict:
        """Execute ``episode`` and return its result dict.

        Args:
            diagnose_if_skipped: When True and the episode is skipped (fault_type=none),
                still invoke ``diagnosis_callback`` after the observation wait.  Used for
                negative-sample (healthy-network) scenarios so the agent can produce a
                verdict that is checked for false-positives.
        """
        executor = self.executor
        _emit(f"\n{'='*70}")
        _emit(f"Episode: {episode.episode_id}")
        _emit(f"Description: {episode.description}")
        _emit(f"{'='*70}")

        episode_result: dict[str, Any] = {
            "episode_id": episode.episode_id,
            "description": episode.description,
            "episode": _episode_payload(episode),
            "start_time": datetime.now().isoformat(),
            "success": False,
            "error": None,
        }

        try:
            if executor.skip_none_episodes and episode.fault_type == "none":
                _emit("\n[Episode] Skipping baseline fault actions (fault_type=none)")
                skipped_start = datetime.now().isoformat()
                waited_seconds = max(0, int(episode.duration_seconds or 0))
                if waited_seconds > 0:
                    _emit(f"[Episode] Waiting {waited_seconds}s to reserve a clean pre-fault baseline window...")
                    _sleep(executor, waited_seconds)
                skipped_end = datetime.now().isoformat()
                skipped_result = executor._build_skipped_episode_result(
                    episode,
                    start_time=skipped_start,
                    end_time=skipped_end,
                    waited_seconds=waited_seconds,
                )
                # For negative-sample (healthy-network) episodes, call the agent so
                # we can measure false-positive rate.  The skipped_result already
                # contains the episode payload needed by the callback.
                if diagnosis_callback and diagnose_if_skipped:
                    _emit("\n[Diagnosis] Calling agent on healthy-network episode (negative sample)...")
                    try:
                        skipped_result["diagnosis"] = diagnosis_callback(skipped_result)
                    except Exception as diagnosis_error:  # noqa: BLE001
                        skipped_result["diagnosis"] = {
                            "error": str(diagnosis_error),
                            "success": False,
                        }
                return skipped_result

            pre_fault_reference = datetime.utcnow().replace(microsecond=0)

            # Step 1: Inject fault
            injection_result = executor._inject_fault(episode)
            episode_result["injection"] = injection_result

            if not injection_result.get("success"):
                episode_result["error"] = "Fault injection failed"
                return episode_result

            # Step 2: Dual-window observation (early + steady) for transient capture.
            early_observation_seconds = int(
                episode.metadata.get(
                    "early_observation_seconds",
                    min(20, max(10, episode.duration_seconds // 3)),
                )
            )
            if early_observation_seconds >= episode.duration_seconds:
                early_observation_seconds = max(0, episode.duration_seconds - 10)
            steady_observation_seconds = max(1, episode.duration_seconds - early_observation_seconds)

            observation_windows: list[dict] = []
            if early_observation_seconds > 0:
                _emit("\n[Early Observation] " f"Monitoring immediately for {early_observation_seconds} seconds...")
                early_observation = executor._wait_and_observe(
                    early_observation_seconds,
                    baseline_end_time=pre_fault_reference,
                )
                observation_windows.append(early_observation)

            # Stabilization wait before the steady window.
            _emit(f"\n[Stabilization] Waiting {episode.stabilization_time}s...")
            _sleep(executor, episode.stabilization_time)

            _emit("\n[Steady Observation] " f"Monitoring stabilized fault for {steady_observation_seconds} seconds...")
            steady_observation = executor._wait_and_observe(
                steady_observation_seconds,
                baseline_end_time=pre_fault_reference,
            )
            observation_windows.append(steady_observation)

            episode_result["observations"] = executor._merge_observation_windows(
                observation_windows,
                total_duration_seconds=episode.duration_seconds,
            )

            # Step 3.5: Optional in-fault diagnosis (skip baseline episodes).
            if diagnosis_callback and episode.fault_type != "none":
                try:
                    episode_result["diagnosis"] = diagnosis_callback(episode_result)
                except Exception as diagnosis_error:  # noqa: BLE001 — surface via result
                    episode_result["diagnosis"] = {
                        "error": str(diagnosis_error),
                        "success": False,
                    }

            # Step 4: Recovery + post-recovery settle.
            recovery_results = executor._recover_fault()
            episode_result["recovery"] = recovery_results

            _emit(f"\n[Post-Recovery] Waiting {executor.post_recovery_wait_seconds}s for recovery...")
            _sleep(executor, executor.post_recovery_wait_seconds)

            episode_result["success"] = True
            episode_result["end_time"] = datetime.now().isoformat()
            return episode_result

        except Exception as exc:  # noqa: BLE001 — preserve historical behaviour
            _emit(f"\n✗ Episode failed: {exc}")
            episode_result["error"] = str(exc)
            episode_result["end_time"] = datetime.now().isoformat()

            # Best-effort recovery even on failure.
            try:
                executor._recover_fault()
            except Exception as recovery_error:  # noqa: BLE001
                _emit(f"  Warning: Recovery failed: {recovery_error}")

            return episode_result


def _episode_payload(episode: Episode) -> dict[str, Any]:
    return {
        "episode_id": episode.episode_id,
        "fault_type": episode.fault_type,
        "target_device": episode.target_device,
        "target_interface": episode.target_interface,
        "target_prefix": episode.target_prefix,
        "mtu": episode.mtu,
        "duration_seconds": episode.duration_seconds,
        "stabilization_time": episode.stabilization_time,
        "metadata": episode.metadata,
        "parameters": episode.parameters,
    }


def _sleep(executor: EpisodeExecutionPort, seconds: float) -> None:
    sleep_fn = getattr(executor, "sleep", time.sleep)
    sleep_fn(seconds)


__all__ = ["EpisodeExecutionPort", "EpisodeRunner"]
