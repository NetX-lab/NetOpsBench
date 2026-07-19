"""Observation timing and Pingmesh analysis for scenario execution."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

from netopsbench.config import config
from netopsbench.logging_utils import get_logger
from netopsbench.platform.topology.topology_utils import coerce_topology_manifest

logger = get_logger(__name__)

_MIN_BASELINE_WINDOW_SECONDS = 60


def _utc_iso(dt: datetime) -> str:
    value = dt.isoformat()
    if value.endswith("+00:00"):
        return value[:-6] + "Z"
    return value if value.endswith("Z") else value + "Z"


def _coverage_epoch_seconds(runner) -> int:
    manifest = coerce_topology_manifest(runner.topology_metadata)
    return manifest.pingmesh.coverage_epoch_seconds(manifest.facts.total_clients)


def capture_observation_window(runner, duration: int, *, name: str = "window") -> dict:
    """Record one observation interval without querying observability backends."""
    safe_duration = max(0, int(duration))
    logger.info(f"\n[Observation] Monitoring for {safe_duration} seconds...")
    start_time = datetime.now(UTC).replace(microsecond=0)
    sleep_fn = getattr(runner, "sleep", time.sleep)
    for index in range(safe_duration):
        sleep_fn(1)
        if (index + 1) % 10 == 0:
            logger.info(f"  Observed {index + 1}/{safe_duration}s...")
    end_time = datetime.now(UTC).replace(microsecond=0)
    return {
        "name": name,
        "start_time": _utc_iso(start_time),
        "end_time": _utc_iso(end_time),
        "duration_seconds": safe_duration,
    }


def _summary_for_window(anomalies: list[dict], window_name: str) -> dict:
    selected = [item for item in anomalies if window_name in (item.get("windows_observed") or [])]
    return {
        "total_anomalies": len(selected),
        "latency_spikes": sum(item.get("type") == "latency_spike" for item in selected),
        "packet_loss_events": sum(item.get("type") == "packet_loss" for item in selected),
        "path_unreachable_events": sum(item.get("type") == "path_unreachable" for item in selected),
        "mtu_or_fragmentation_events": sum(item.get("type") == "mtu_or_fragmentation_suspect" for item in selected),
        "jitter_spikes": sum(item.get("type") == "jitter_spike" for item in selected),
    }


def analyze_observation_windows(
    runner,
    windows: list[dict],
    total_duration_seconds: int,
    baseline_end_time: datetime | None = None,
    baseline_window: dict | None = None,
) -> dict:
    """Analyze captured intervals using one baseline and one current snapshot."""
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

    from netopsbench.platform.pingmesh.detector import AnomalyDetector

    if baseline_window is not None:
        baseline_start = str(baseline_window.get("start_time") or "")
        baseline_end = str(baseline_window.get("end_time") or "")
        if not baseline_start or not baseline_end:
            raise ValueError("baseline_window must contain start_time and end_time")
    else:
        baseline_end_dt = baseline_end_time or datetime.fromisoformat(
            str(valid_windows[0]["start_time"]).replace("Z", "+00:00")
        )
        baseline_seconds = max(_MIN_BASELINE_WINDOW_SECONDS, _coverage_epoch_seconds(runner))
        baseline_start = _utc_iso(baseline_end_dt - timedelta(seconds=baseline_seconds))
        baseline_end = _utc_iso(baseline_end_dt)
    current_start = str(valid_windows[0]["start_time"])
    current_end = str(valid_windows[-1]["end_time"])

    detector = AnomalyDetector(
        influxdb_url=runner.influxdb_url or config.influxdb_url,
        token=runner.influxdb_token or config.influxdb_token,
        org=runner.influxdb_org or config.influxdb_org,
        bucket=runner.influxdb_bucket or config.influxdb_bucket,
        topology_metadata=runner.topology_metadata,
        topology_id=runner.topology_id,
    )
    report = detector.generate_windowed_anomaly_report(
        baseline_start=baseline_start,
        baseline_end=baseline_end,
        current_start=current_start,
        current_end=current_end,
        windows=valid_windows,
    )
    query_status = report.get("query_status", {})
    query_ok = bool(query_status.get("ok"))
    anomalies = report.get("anomalies", []) or []
    coverage = report.get("coverage", {}) or {}
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
    }


def wait_and_observe(
    runner,
    duration: int,
    baseline_end_time: datetime | None = None,
    baseline_window: dict | None = None,
) -> dict:
    """Capture and analyze one observation window."""
    window = capture_observation_window(runner, duration, name="steady")
    return analyze_observation_windows(
        runner,
        [window],
        total_duration_seconds=duration,
        baseline_end_time=baseline_end_time,
        baseline_window=baseline_window,
    )
