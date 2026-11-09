"""Tests for the shared benchmark and simulator episode kernel."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from netopsbench.models.scenario import EpisodeSpec
from netopsbench.platform.scenario.episode_runner import observe_episode
from netopsbench.platform.scenario.timing import PhaseTimings

_BASELINE = {"start_time": "baseline-start", "end_time": "baseline-end"}


class StubExecutor:
    def __init__(
        self,
        *,
        injection_success: bool = True,
        observe_payload: dict[str, Any] | None = None,
    ):
        self.timings = PhaseTimings()
        self.observation_timeout_seconds = 60
        self._injection_success = injection_success
        self._observe_payload = observe_payload or {"observed": True}
        self.calls: list[str] = []
        self.baseline_windows: list[dict[str, Any] | None] = []

    def _inject_fault(self, episode):
        self.calls.append("inject")
        return {"success": self._injection_success, "fault_type": episode.fault_type}

    def _capture_observation_window(self, seconds, name):
        self.calls.append(f"capture:{name}:{seconds}")
        return {
            "name": name,
            "duration_seconds": seconds,
            "start_time": "2026-10-24T00:00:00+00:00",
            "end_time": (datetime(2026, 10, 24, tzinfo=UTC) + timedelta(seconds=seconds)).isoformat(),
            **self._observe_payload,
        }

    def _merge_observation_windows(
        self,
        windows,
        total_duration_seconds,
        *,
        baseline_window,
    ):
        self.calls.append("merge")
        self.baseline_windows.append(baseline_window)
        return {"windows": list(windows), "total": total_duration_seconds, **self._observe_payload}


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


def test_healthy_episode_is_observed_without_fault_actions():
    executor = StubExecutor()
    result = observe_episode(
        executor,
        _episode(fault_type="none", duration_seconds=2, metadata={}),
        baseline_window=_BASELINE,
    )

    assert result["state"] == "active"
    assert result["observations"]["observed"] is True
    assert executor.calls == ["capture:full:2", "merge"]


def test_healthy_episode_promotes_coverage_audit_without_private_observation_key():
    coverage = {"coverage_status": "complete", "destination_pairs_observed": 2}
    executor = StubExecutor(observe_payload={"_coverage_audit": coverage})

    result = observe_episode(
        executor,
        _episode(fault_type="none", duration_seconds=2, metadata={}),
        baseline_window={"start_time": "start", "end_time": "end"},
    )

    assert result["coverage_audit"] == coverage
    assert "_coverage_audit" not in result["observations"]


def test_observation_keeps_fault_active_for_incident_engine():
    executor = StubExecutor()
    episode = _episode(duration_seconds=2, metadata={"early_observation_seconds": 0})

    active = observe_episode(executor, episode, baseline_window=_BASELINE)

    assert active["state"] == "active"
    assert executor.calls == ["inject", "capture:full:2", "merge"]


def test_interactive_kernel_uses_explicit_cached_baseline_window():
    executor = StubExecutor()
    baseline = {"start_time": "2026-07-15T00:00:00Z", "end_time": "2026-07-15T00:01:00Z"}

    observe_episode(
        executor,
        _episode(duration_seconds=2, metadata={"early_observation_seconds": 0}),
        baseline_window=baseline,
    )

    assert executor.baseline_windows == [baseline]


def test_observation_defers_detector_analysis_until_all_windows_are_captured():
    executor = StubExecutor()
    executor.sleep = lambda _seconds: None

    result = observe_episode(
        executor,
        _episode(duration_seconds=12, stabilization_time=2, metadata={"early_observation_seconds": 4}),
        baseline_window=_BASELINE,
    )

    assert result["state"] == "active"
    assert executor.calls == ["inject", "capture:full:12", "merge"]


def test_stabilization_is_a_slice_gap_not_an_additional_wait():
    executor = StubExecutor()
    slept: list[float] = []
    executor.sleep = slept.append

    observe_episode(
        executor,
        _episode(duration_seconds=3, stabilization_time=1, metadata={"early_observation_seconds": 0}),
        baseline_window=_BASELINE,
    )

    assert slept == []
    assert executor.calls == ["inject", "capture:full:3", "merge"]
