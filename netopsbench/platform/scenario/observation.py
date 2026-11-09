"""Observation timing and Pingmesh analysis for scenario execution."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

from netopsbench.config import config
from netopsbench.logging_utils import get_logger
from netopsbench.platform.utils.control import OperationControl, current_operation_control

logger = get_logger(__name__)


def _utc_iso(dt: datetime) -> str:
    value = dt.isoformat()
    if value.endswith("+00:00"):
        return value[:-6] + "Z"
    return value if value.endswith("Z") else value + "Z"


def complete_window_seconds(runner) -> int:
    """Return the topology-derived complete Pingmesh observation window."""
    manifest = runner.current_manifest()
    return manifest.require_pingmesh_plan().settings.complete_window_seconds


def baseline_window_seconds(runner, minimum_seconds: int) -> int:
    """Return the explicit baseline duration for one runner."""
    return max(max(0, int(minimum_seconds)), complete_window_seconds(runner))


def capture_observation_window(runner, duration: int, *, name: str = "window", deadline: float | None = None) -> dict:
    """Record one observation interval without querying observability backends."""
    safe_duration = max(0, int(duration))
    inherited = current_operation_control()
    limits = [value for value in (deadline, inherited.deadline if inherited else None) if value is not None]
    control = OperationControl(inherited.cancel_event if inherited else None, min(limits) if limits else None)
    if control.deadline is not None and control.remaining() < safe_duration:
        raise TimeoutError("Insufficient recovery budget for a complete observation window")
    logger.info(f"\n[Observation] Monitoring for {safe_duration} seconds...")
    started = time.monotonic()
    stop = started + safe_duration
    start_time = datetime.now(UTC)
    sleep_fn = getattr(runner, "sleep", time.sleep)
    next_progress = 10
    while (remaining := stop - time.monotonic()) > 0:
        sleep_fn(control.remaining(min(1.0, remaining)))
        control.remaining()
        elapsed = time.monotonic() - started
        if elapsed >= next_progress:
            logger.info(f"  Observed {min(int(elapsed), safe_duration)}/{safe_duration}s...")
            next_progress = (int(elapsed) // 10 + 1) * 10
    actual = max(0.0, time.monotonic() - started)
    return {
        "name": name,
        "plan_id": runner.topology_metadata["pingmesh_plan"]["settings"]["plan_id"],
        "start_time": _utc_iso(start_time),
        "end_time": _utc_iso(start_time + timedelta(seconds=safe_duration)),
        "duration_seconds": safe_duration,
        "actual_wait_seconds": actual,
        "wake_overrun_seconds": max(0.0, actual - safe_duration),
    }


def capture_baseline_window(runner, minimum_seconds: int) -> dict:
    """Capture a baseline only after traffic setup has completed."""
    return capture_observation_window(
        runner,
        baseline_window_seconds(runner, minimum_seconds),
        name="baseline",
    )


def baseline_gate_errors(observation: dict) -> list[str]:
    """Return absolute health failures for a candidate healthy Pingmesh window."""
    errors = observation_integrity_errors(observation)
    report = observation.get("pingmesh_metrics") or {}
    summary = report.get("summary") or {}
    quality = report.get("quality") or {}
    absolute_health = observation.get("_baseline_health") or quality
    current_paths = int(quality.get("current_paths_observed", 0) or 0)
    if current_paths <= 0:
        return errors

    exact_zero = {
        "absolute_unreachable_paths": int(absolute_health.get("absolute_unreachable_paths", 0) or 0),
        "latency_spikes": int(summary.get("latency_spikes", 0) or 0),
        "absolute_network_mtu_paths": int(absolute_health.get("absolute_network_mtu_paths", 0) or 0),
    }
    errors.extend(f"{name}={value}" for name, value in exact_zero.items() if value)

    packet_loss = int(absolute_health.get("absolute_packet_loss_paths", 0) or 0)
    loss_rate = packet_loss / current_paths
    if loss_rate > 0.001:
        errors.append(f"packet_loss_path_rate={loss_rate:.6f}")
    return errors


def observation_integrity_errors(observation: dict) -> list[str]:
    """Return data-integrity failures without judging fault anomalies."""
    errors: list[str] = []
    if observation.get("data_source_status") != "ok":
        errors.append(f"data={observation.get('data_source_status')}")
    if observation.get("coverage_status") != "complete":
        errors.append(f"coverage={observation.get('coverage_status')}")
    baseline_coverage = observation.get("_baseline_coverage") or {}
    if baseline_coverage.get("coverage_status") != "complete":
        errors.append(f"baseline_coverage={baseline_coverage.get('coverage_status') or 'missing'}")

    report = observation.get("pingmesh_metrics") or {}
    quality = report.get("quality") or {}
    if int(quality.get("current_paths_observed", 0) or 0) <= 0:
        errors.append("current_paths_observed=0")
    local_df_drops = int(quality.get("local_df_mtu_drops", 0) or 0)
    if local_df_drops:
        errors.append(f"local_df_mtu_drops={local_df_drops}")
    local_probe_errors = int(quality.get("local_probe_errors", 0) or 0)
    if local_probe_errors:
        errors.append(f"local_probe_errors={local_probe_errors}")
    return errors


def _summary_for_window(anomalies: list[dict], window_name: str) -> dict:
    selected = [item for item in anomalies if window_name in (item.get("windows_observed") or [])]
    return {
        "total_anomalies": len(selected),
        "latency_spikes": sum(item.get("type") == "latency_spike" for item in selected),
        "packet_loss_events": sum(item.get("type") == "packet_loss" for item in selected),
        "path_unreachable_events": sum(item.get("type") == "path_unreachable" for item in selected),
        "mtu_or_fragmentation_events": sum(item.get("type") == "mtu_or_fragmentation_suspect" for item in selected),
    }


def observation_detector(runner):
    from netopsbench.platform.pingmesh.detector import AnomalyDetector

    manifest = runner.current_manifest()
    settings = {
        "influxdb_url": runner.influxdb_url or config.influxdb_url,
        "token": runner.influxdb_token or config.influxdb_token,
        "org": runner.influxdb_org or config.influxdb_org,
        "bucket": runner.influxdb_bucket or config.influxdb_bucket,
        "topology_id": runner.topology_id,
        "runtime_start_time": getattr(runner, "_pingmesh_runtime_start", None),
    }
    scope = (manifest.topology_id, manifest.require_pingmesh_plan().settings.plan_id, *settings.values())
    if runner._detector_scope != scope:
        runner._detector = AnomalyDetector(topology_metadata=manifest, **settings)
        runner._detector_scope = scope
    return runner._detector


def analyze_observation_windows(
    runner,
    windows: list[dict],
    total_duration_seconds: int,
    baseline_window: dict,
    deadline: float | None = None,
) -> dict:
    """Analyze captured intervals using one baseline and one current snapshot."""
    control = current_operation_control()
    if control is not None and control.deadline is not None:
        deadline = min(deadline, control.deadline) if deadline is not None else control.deadline
    valid_windows = [window for window in windows if isinstance(window, dict) and window.get("start_time")]
    if not valid_windows:
        now = _utc_iso(datetime.now(UTC).replace(microsecond=0))
        return {
            "start_time": now,
            "end_time": now,
            "duration_seconds": total_duration_seconds,
            "pingmesh_metrics": {"summary": {"total_anomalies": 0}, "anomalies": []},
            "anomalies_detected": False,
            "coverage_status": "incomplete",
            "data_source_status": "unavailable",
            "observation_windows": [],
        }

    baseline_start = str(baseline_window.get("start_time") or "")
    baseline_end = str(baseline_window.get("end_time") or "")
    if not baseline_start or not baseline_end:
        raise ValueError("baseline_window must contain start_time and end_time")
    current_start = str(valid_windows[0]["start_time"])
    current_end = str(valid_windows[-1]["end_time"])

    detector = observation_detector(runner)
    report = detector.generate_windowed_anomaly_report(
        baseline_start=baseline_start,
        baseline_end=baseline_end,
        current_start=current_start,
        current_end=current_end,
        windows=valid_windows,
        include_internal_health=True,
        **({"deadline": deadline} if deadline is not None else {}),
        **({"evidence_pairs": runner._case_probe_pairs} if getattr(runner, "_case_probe_pairs", None) else {}),
    )
    baseline_health = report.pop("_baseline_health", {})
    baseline_coverage = report.pop("_baseline_coverage", {})
    probe_evidence = report.pop("_probe_evidence", [])
    query_status = report.get("query_status", {})
    query_ok = bool(query_status.get("ok"))
    anomalies = report.get("anomalies", []) or []
    coverage = dict(report.get("coverage", {}) or {})
    for key in ("missing_tasks", "invalid_tasks", "invalid_rows", "observable_pairs", "unexpected_records"):
        report.get("coverage", {}).pop(key, None)
    observation_windows = [
        {
            **window,
            "summary": _summary_for_window(anomalies, str(window.get("name") or "window")),
        }
        for window in valid_windows
    ]
    return {
        "start_time": current_start,
        "end_time": current_end,
        "duration_seconds": total_duration_seconds,
        "pingmesh_metrics": report,
        "anomalies_detected": bool(report.get("summary", {}).get("total_anomalies", 0)),
        "coverage_status": coverage.get("coverage_status", "error"),
        "data_source_status": "ok" if query_ok else f"error: {query_status.get('error') or 'query_failed'}",
        "observation_windows": observation_windows,
        "_coverage_audit": coverage,
        "_probe_evidence": probe_evidence,
        "_baseline_health": baseline_health,
        "_baseline_coverage": baseline_coverage,
    }


__all__ = [
    "analyze_observation_windows",
    "baseline_gate_errors",
    "baseline_window_seconds",
    "capture_baseline_window",
    "capture_observation_window",
    "complete_window_seconds",
    "observation_integrity_errors",
]
