"""Observation timing and Pingmesh analysis for scenario execution."""

from __future__ import annotations

import time
from datetime import UTC, datetime

from netopsbench.config import config
from netopsbench.logging_utils import get_logger
from netopsbench.platform.topology.topology_utils import coerce_topology_manifest

logger = get_logger(__name__)


def _utc_iso(dt: datetime) -> str:
    value = dt.isoformat()
    if value.endswith("+00:00"):
        return value[:-6] + "Z"
    return value if value.endswith("Z") else value + "Z"


def complete_window_seconds(runner) -> int:
    """Return the topology-derived complete Pingmesh observation window."""
    manifest = coerce_topology_manifest(runner.topology_metadata)
    return manifest.pingmesh.complete_window_seconds(manifest.facts.total_clients)


def baseline_window_seconds(runner, minimum_seconds: int) -> int:
    """Return the explicit baseline duration for one runner."""
    return max(max(0, int(minimum_seconds)), complete_window_seconds(runner))


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


def capture_baseline_window(runner, minimum_seconds: int) -> dict:
    """Capture a baseline only after traffic setup has completed."""
    return capture_observation_window(
        runner,
        baseline_window_seconds(runner, minimum_seconds),
        name="baseline",
    )


def baseline_gate_errors(observation: dict) -> list[str]:
    """Return absolute health failures for a candidate healthy Pingmesh window."""
    errors: list[str] = []
    if observation.get("data_source_status") != "ok":
        errors.append(f"data={observation.get('data_source_status')}")
    if observation.get("coverage_status") != "complete":
        errors.append(f"coverage={observation.get('coverage_status')}")
    baseline_coverage = observation.get("_baseline_coverage") or {}
    if baseline_coverage.get("coverage_status") != "complete":
        errors.append(f"baseline_coverage={baseline_coverage.get('coverage_status') or 'missing'}")

    report = observation.get("pingmesh_metrics") or {}
    summary = report.get("summary") or {}
    quality = report.get("quality") or {}
    absolute_health = observation.get("_baseline_health") or quality
    current_paths = int(quality.get("current_paths_observed", 0) or 0)
    if current_paths <= 0:
        errors.append("current_paths_observed=0")
        return errors

    exact_zero = {
        "absolute_unreachable_paths": int(absolute_health.get("absolute_unreachable_paths", 0) or 0),
        "latency_spikes": int(summary.get("latency_spikes", 0) or 0),
        "absolute_network_mtu_paths": int(absolute_health.get("absolute_network_mtu_paths", 0) or 0),
        "local_df_mtu_drops": int(quality.get("local_df_mtu_drops", 0) or 0),
        "local_probe_errors": int(quality.get("local_probe_errors", 0) or 0),
    }
    errors.extend(f"{name}={value}" for name, value in exact_zero.items() if value)

    packet_loss = int(absolute_health.get("absolute_packet_loss_paths", 0) or 0)
    loss_rate = packet_loss / current_paths
    if loss_rate > 0.001:
        errors.append(f"packet_loss_path_rate={loss_rate:.6f}")
    return errors


def observation_integrity_errors(
    observation: dict,
    *,
    allow_fault_local_errors: bool = False,
) -> list[str]:
    """Return data-integrity failures without judging fault anomalies.

    Local DF drops and probe errors are valid observations during a positive
    impairment/link episode.  They remain integrity failures by default (and
    for healthy baselines), but the incident backend may preserve them as
    evidence once source coverage and data-source status are otherwise valid.
    """
    errors: list[str] = []
    if observation.get("data_source_status") != "ok":
        errors.append(f"data={observation.get('data_source_status')}")
    coverage_status = observation.get("coverage_status")
    if coverage_status != "complete":
        coverage = (observation.get("pingmesh_metrics") or {}).get("coverage") or {}
        coverage_complete_enough = (
            allow_fault_local_errors
            and coverage.get("status") == "ok"
            and int(coverage.get("missing_pair_combinations", 0) or 0) == 0
            and int(coverage.get("missing_source_clients", 0) or 0) == 0
            and not (coverage.get("missing_port_batches") or [])
            and not (coverage.get("missing_destination_batches") or [])
            and int(coverage.get("source_clients_observed", 0) or 0)
            == int(coverage.get("expected_source_clients", 0) or 0)
            and int(coverage.get("destination_pairs_observed", 0) or 0)
            == int(coverage.get("expected_destination_pairs", 0) or 0)
            and int(coverage.get("pair_port_combinations_observed", 0) or 0)
            == int(coverage.get("expected_pair_port_combinations", 0) or 0)
        )
        if not coverage_complete_enough:
            errors.append(f"coverage={coverage_status}")
    baseline_coverage = observation.get("_baseline_coverage") or {}
    if baseline_coverage.get("coverage_status") != "complete":
        errors.append(f"baseline_coverage={baseline_coverage.get('coverage_status') or 'missing'}")

    report = observation.get("pingmesh_metrics") or {}
    quality = report.get("quality") or {}
    if int(quality.get("current_paths_observed", 0) or 0) <= 0:
        errors.append("current_paths_observed=0")
    if not allow_fault_local_errors:
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


def analyze_observation_windows(
    runner,
    windows: list[dict],
    total_duration_seconds: int,
    baseline_window: dict,
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

    baseline_start = str(baseline_window.get("start_time") or "")
    baseline_end = str(baseline_window.get("end_time") or "")
    if not baseline_start or not baseline_end:
        raise ValueError("baseline_window must contain start_time and end_time")
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
        include_internal_health=True,
    )
    baseline_health = report.pop("_baseline_health", {})
    baseline_coverage = report.pop("_baseline_coverage", {})
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
        "_baseline_health": baseline_health,
        "_baseline_coverage": baseline_coverage,
    }


def wait_and_observe(
    runner,
    duration: int,
    *,
    baseline_window: dict,
) -> dict:
    """Capture and analyze one observation window."""
    window = capture_observation_window(runner, duration, name="steady")
    return analyze_observation_windows(
        runner,
        [window],
        total_duration_seconds=duration,
        baseline_window=baseline_window,
    )


__all__ = [
    "analyze_observation_windows",
    "baseline_gate_errors",
    "baseline_window_seconds",
    "capture_baseline_window",
    "capture_observation_window",
    "complete_window_seconds",
    "observation_integrity_errors",
    "wait_and_observe",
]
