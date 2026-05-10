"""Observation runtime helpers for scenario execution."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

from netopsbench.config import config
from netopsbench.logging_utils import get_logger
from netopsbench.platform.utils.events import emit as _emit

logger = get_logger(__name__)

# Minimum baseline window (seconds) to ensure enough samples for statistical detection.
# With a 5s ping cycle, 60s guarantees ~12 data points per path.
_MIN_BASELINE_WINDOW_SECONDS = 60


def _utc_iso(dt: datetime) -> str:
    """Format *dt* as an InfluxDB-compatible UTC ISO-8601 string ending in 'Z'.

    ``datetime.isoformat()`` on an aware UTC datetime returns ``+00:00``;
    appending ``Z`` on top of that produces the invalid ``+00:00Z`` which
    InfluxDB's Flux ``time()`` rejects with HTTP 400.
    """
    s = dt.isoformat()
    if s.endswith("+00:00"):
        return s[:-6] + "Z"
    return s if s.endswith("Z") else s + "Z"


def wait_and_observe(runner, duration: int, baseline_end_time: datetime | None = None) -> dict:
    """Collect a single observation window and run Pingmesh anomaly detection."""

    _emit(f"\n[Observation] Monitoring for {duration} seconds...")

    start_time = datetime.now(UTC).replace(microsecond=0)
    observations = {
        "start_time": _utc_iso(start_time),
        "duration_seconds": duration,
        "pingmesh_metrics": [],
        "anomalies_detected": False,
        "data_source_status": "unavailable",
    }

    for i in range(duration):
        time.sleep(1)
        if (i + 1) % 10 == 0:
            _emit(f"  Observed {i + 1}/{duration}s...")

    end_time = datetime.now(UTC).replace(microsecond=0)
    observations["end_time"] = _utc_iso(end_time)

    baseline_end_dt = baseline_end_time or start_time
    # Ensure minimum baseline window for reliable statistical detection
    baseline_window = max(duration, _MIN_BASELINE_WINDOW_SECONDS)
    baseline_start = _utc_iso(baseline_end_dt - timedelta(seconds=baseline_window))
    baseline_end = _utc_iso(baseline_end_dt)
    current_start = _utc_iso(start_time)
    current_end = _utc_iso(end_time)

    try:
        from netopsbench.platform.pingmesh.detector import AnomalyDetector

        detector = AnomalyDetector(
            influxdb_url=runner.influxdb_url or config.influxdb_url,
            token=runner.influxdb_token or config.influxdb_token,
            org=runner.influxdb_org or config.influxdb_org,
            bucket=runner.influxdb_bucket or config.influxdb_bucket,
            topology_id=runner.topology_id,
        )
        report = detector.generate_anomaly_report(
            baseline_start=baseline_start,
            baseline_end=baseline_end,
            current_start=current_start,
            current_end=current_end,
        )
        observations["pingmesh_metrics"] = report
        observations["anomalies_detected"] = report.get("summary", {}).get("total_anomalies", 0) > 0
        query_status = report.get("query_status", {}) if isinstance(report, dict) else {}
        if query_status.get("ok", False):
            observations["data_source_status"] = "ok"
        else:
            error_msg = query_status.get("error") or "query_failed"
            observations["data_source_status"] = f"error: {error_msg}"
    except Exception as e:
        observations["data_source_status"] = f"error: {e}"

    return observations


def merge_observation_windows(windows: list[dict], total_duration_seconds: int) -> dict:
    """Merge early/steady observation windows into one agent-facing payload."""
    if not windows:
        now = _utc_iso(datetime.now(UTC).replace(microsecond=0))
        return {
            "start_time": now,
            "duration_seconds": total_duration_seconds,
            "pingmesh_metrics": {
                "summary": {
                    "total_anomalies": 0,
                    "latency_spikes": 0,
                    "packet_loss_events": 0,
                    "path_unreachable_events": 0,
                    "mtu_or_fragmentation_events": 0,
                    "jitter_spikes": 0,
                },
                "anomalies": [],
            },
            "anomalies_detected": False,
            "data_source_status": "unavailable",
            "end_time": now,
            "observation_windows": [],
        }

    valid_windows = [w for w in windows if isinstance(w, dict) and w]
    if not valid_windows:
        return merge_observation_windows([], total_duration_seconds)

    combined_anomalies: list[dict] = []
    combined_src_leaf: dict[str, dict[str, int]] = {}
    combined_dst_leaf: dict[str, dict[str, int]] = {}
    combined_spine: dict[str, dict[str, int]] = {}
    latency_spikes = 0
    packet_loss_events = 0
    path_unreachable_events = 0
    mtu_or_fragmentation_events = 0
    jitter_spikes = 0
    query_ok = True
    query_errors = []
    observation_windows = []

    for idx, observation in enumerate(valid_windows, start=1):
        pm = observation.get("pingmesh_metrics", {})
        if not isinstance(pm, dict):
            continue

        summary = pm.get("summary", {}) or {}
        latency_spikes += int(summary.get("latency_spikes", 0) or 0)
        packet_loss_events += int(summary.get("packet_loss_events", 0) or 0)
        path_unreachable_events += int(summary.get("path_unreachable_events", 0) or 0)
        mtu_or_fragmentation_events += int(summary.get("mtu_or_fragmentation_events", 0) or 0)
        jitter_spikes += int(summary.get("jitter_spikes", 0) or 0)

        for anomaly in pm.get("anomalies", []) or []:
            anomaly_with_window = dict(anomaly)
            anomaly_with_window["observation_window"] = f"window_{idx}"
            combined_anomalies.append(anomaly_with_window)

        agg = pm.get("aggregated_anomalies", {}) or {}
        _agg_keys = ("drop_count", "latency_spikes", "jitter_spikes", "path_unreachable")
        for leaf, values in (agg.get("by_src_leaf", {}) or {}).items():
            bucket = combined_src_leaf.setdefault(leaf, {k: 0 for k in _agg_keys})
            for k in _agg_keys:
                bucket[k] += int(values.get(k, 0) or 0)
        for leaf, values in (agg.get("by_dst_leaf", {}) or {}).items():
            bucket = combined_dst_leaf.setdefault(leaf, {k: 0 for k in _agg_keys})
            for k in _agg_keys:
                bucket[k] += int(values.get(k, 0) or 0)
        for spine, values in (agg.get("by_spine", {}) or {}).items():
            bucket = combined_spine.setdefault(spine, {k: 0 for k in _agg_keys})
            for k in _agg_keys:
                bucket[k] += int(values.get(k, 0) or 0)

        query_status = pm.get("query_status", {}) or {}
        if not query_status.get("ok", False):
            query_ok = False
            if query_status.get("error"):
                query_errors.append(str(query_status["error"]))

        observation_windows.append(
            {
                "name": f"window_{idx}",
                "start_time": observation.get("start_time"),
                "end_time": observation.get("end_time"),
                "duration_seconds": observation.get("duration_seconds"),
                "summary": summary,
            }
        )

    merged_query_error = "; ".join(dict.fromkeys(query_errors)) if query_errors else None

    return {
        "start_time": valid_windows[0].get("start_time"),
        "duration_seconds": total_duration_seconds,
        "pingmesh_metrics": {
            "timestamp": datetime.now(UTC).isoformat(),
            "windows": {
                "window_1": valid_windows[0].get("pingmesh_metrics", {}).get("windows", {}),
                "window_2": valid_windows[-1].get("pingmesh_metrics", {}).get("windows", {}),
            },
            "query_status": {
                "ok": query_ok,
                "error": merged_query_error,
            },
            "summary": {
                "total_anomalies": len(combined_anomalies),
                "latency_spikes": latency_spikes,
                "packet_loss_events": packet_loss_events,
                "path_unreachable_events": path_unreachable_events,
                "mtu_or_fragmentation_events": mtu_or_fragmentation_events,
                "jitter_spikes": jitter_spikes,
            },
            "anomalies": combined_anomalies,
            "aggregated_anomalies": {
                "by_src_leaf": combined_src_leaf,
                "by_dst_leaf": combined_dst_leaf,
                "by_spine": combined_spine,
            },
        },
        "anomalies_detected": len(combined_anomalies) > 0,
        "data_source_status": "ok" if query_ok else f"error: {merged_query_error or 'query_failed'}",
        "end_time": valid_windows[-1].get("end_time"),
        "observation_windows": observation_windows,
    }
