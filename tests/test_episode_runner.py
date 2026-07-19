"""Tests for the shared benchmark and simulator episode kernel."""

from __future__ import annotations

from typing import Any

import pytest

from netopsbench.models.scenario import EpisodeSpec
from netopsbench.platform.scenario.episode_runner import abort_episode, finish_episode, observe_episode, run_episode


class StubExecutor:
    def __init__(
        self,
        *,
        post_recovery_wait_seconds: float = 0,
        injection_success: bool = True,
        observe_payload: dict[str, Any] | None = None,
    ):
        self.post_recovery_wait_seconds = post_recovery_wait_seconds
        self._injection_success = injection_success
        self._observe_payload = observe_payload or {"observed": True}
        self.calls: list[str] = []
        self.baseline_windows: list[dict[str, Any] | None] = []

    def _inject_fault(self, episode):
        self.calls.append("inject")
        return {"success": self._injection_success, "fault_type": episode.fault_type}

    def _wait_and_observe(self, seconds, baseline_end_time=None, baseline_window=None):
        self.calls.append(f"observe:{seconds}")
        self.baseline_windows.append(baseline_window)
        return {"duration": seconds, **self._observe_payload}

    def _capture_observation_window(self, seconds, name):
        self.calls.append(f"capture:{name}:{seconds}")
        return {"name": name, "duration": seconds, **self._observe_payload}

    def _merge_observation_windows(
        self,
        windows,
        *,
        total_duration_seconds,
        baseline_end_time=None,
        baseline_window=None,
    ):
        self.calls.append("merge")
        self.baseline_windows.append(baseline_window)
        return {"windows": list(windows), "total": total_duration_seconds}

    def _recover_fault(self):
        self.calls.append("recover")
        return {"success": True}


def _episode(**overrides) -> EpisodeSpec:
    values = {
        "episode_id": "diagnosis",
        "fault_type": "link_down",
        "target_device": "leaf1",
        "target_interface": "Ethernet1",
        "duration_seconds": 12,
        "stabilization_time": 0,
        "metadata": {"early_observation_seconds": 1},
    }
    values.update(overrides)
    return EpisodeSpec(**values)


def test_healthy_episode_is_observed_and_diagnosed_without_fault_actions():
    executor = StubExecutor()
    result = run_episode(
        executor,
        _episode(fault_type="none", duration_seconds=2, metadata={}),
        diagnosis_callback=lambda _payload: {"verdict": "network_healthy"},
    )

    assert result["success"] is True
    assert result["diagnosis"]["verdict"] == "network_healthy"
    assert executor.calls == ["observe:2"]


def test_injection_failure_is_an_infrastructure_error_and_cleanup_is_attempted():
    executor = StubExecutor(injection_success=False)

    with pytest.raises(RuntimeError, match="Fault injection failed"):
        run_episode(executor, _episode(duration_seconds=2, metadata={"early_observation_seconds": 0}))

    assert executor.calls == ["inject", "recover"]


def test_interactive_kernel_keeps_fault_active_until_finish():
    executor = StubExecutor()
    episode = _episode(duration_seconds=2, metadata={"early_observation_seconds": 0})

    active = observe_episode(executor, episode)

    assert active["state"] == "active"
    assert executor.calls == ["inject", "capture:steady:2", "merge"]

    terminal = finish_episode(
        executor,
        episode,
        active,
        diagnosis_callback=lambda _payload: {"verdict": "fault_detected"},
    )
    assert terminal["state"] == "terminal"
    assert terminal["diagnosis"]["verdict"] == "fault_detected"
    assert executor.calls[-1] == "recover"


def test_interactive_kernel_uses_explicit_cached_baseline_window():
    executor = StubExecutor()
    baseline = {"start_time": "2026-07-15T00:00:00Z", "end_time": "2026-07-15T00:01:00Z"}

    observe_episode(
        executor,
        _episode(duration_seconds=2, metadata={"early_observation_seconds": 0}),
        baseline_window=baseline,
    )

    assert executor.baseline_windows == [baseline]


def test_runner_defers_detector_analysis_until_all_windows_are_captured():
    executor = StubExecutor()
    executor.sleep = lambda _seconds: None

    result = run_episode(
        executor,
        _episode(duration_seconds=12, stabilization_time=2, metadata={"early_observation_seconds": 4}),
    )

    assert result["success"] is True
    assert executor.calls == ["inject", "capture:early:4", "capture:steady:8", "merge", "recover"]


def test_runner_uses_executor_sleep_hook_for_stabilization_and_recovery():
    executor = StubExecutor(post_recovery_wait_seconds=2)
    slept: list[float] = []
    executor.sleep = slept.append

    run_episode(executor, _episode(duration_seconds=3, stabilization_time=1, metadata={"early_observation_seconds": 0}))

    assert slept == [1, 2]


def test_abort_episode_only_recovers_fault_cases():
    executor = StubExecutor()

    abort_episode(executor, _episode(fault_type="none"))
    abort_episode(executor, _episode())

    assert executor.calls == ["recover"]
