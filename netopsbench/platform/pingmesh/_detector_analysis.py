"""Pure snapshot analysis helpers for the Pingmesh anomaly detector."""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

_MIN_LATENCY_DELTA_MS = 20.0
_MIN_PATH_LOST_PROBES = 3


def _utcnow_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S") + "Z"


def _as_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _group_by_path(rows: list[dict]) -> dict[tuple[str, str], list[dict]]:
    paths: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        key = (str(row.get("src_ip", "")), str(row.get("dst_ip", "")))
        paths.setdefault(key, []).append(row)
    for points in paths.values():
        points.sort(key=lambda point: str(point.get("_time") or point.get("time") or ""))
    return paths


def _loss_stats(rows: list[dict], *, prefix: str = "") -> dict | None:
    sent_field = f"{prefix}packets_sent"
    lost_field = f"{prefix}packets_lost"
    counted: list[tuple[float, float]] = []
    sample_count = 0
    mtu_drops = 0.0

    for row in rows:
        sent = _as_float(row.get(sent_field))
        lost = _as_float(row.get(lost_field))
        if sent > 0:
            counted.append((sent, lost))
            sample_count += 1
        if prefix:
            mtu_drops += _as_float(row.get("df_mtu_drops"))

    if not counted:
        return None
    total_sent = sum(sent for sent, _lost in counted)
    total_lost = sum(lost for _sent, lost in counted)
    loss_pct = (total_lost / total_sent) * 100.0
    return {
        "loss_pct": loss_pct,
        "sent": total_sent,
        "lost": total_lost,
        "samples": sample_count,
        "mtu_drops": mtu_drops,
    }


@dataclass
class SnapshotAnalysis:
    anomalies: list
    quality: dict[str, int]


class DetectorAnalysisMixin:
    client_to_leaf: dict[str, str]
    loss_pct_threshold: float
    loss_pct_delta: float
    _anomaly_type: Any

    def _resolve_leaf(self, leaf_tag: str, client_name: str) -> str:
        if isinstance(leaf_tag, str) and leaf_tag:
            return leaf_tag
        if isinstance(client_name, str) and client_name in self.client_to_leaf:
            return self.client_to_leaf[client_name]
        return "unknown"

    @staticmethod
    def _signal_severity(anomaly_type: str, value: float, baseline: float, threshold: float) -> str:
        increase = max(0.0, value - baseline)
        if anomaly_type == "latency_spike":
            if increase >= 50.0:
                return "high"
            if increase >= 10.0:
                return "medium"
            return "low"
        if threshold == 0:
            return "high"
        ratio = value / threshold
        if ratio > 2:
            return "high"
        if ratio > 1.5:
            return "medium"
        return "low"

    def _new_anomaly(
        self,
        anomaly_type: str,
        point: dict,
        *,
        value: float,
        baseline: float,
        threshold: float,
        severity: str | None = None,
        samples_sent: int = 0,
        samples_lost: int = 0,
        sample_count: int = 0,
    ):
        return self._anomaly_type(
            type=anomaly_type,
            src_ip=str(point.get("src_ip", "")),
            src_name=str(point.get("src_name", "")),
            dst_ip=str(point.get("dst_ip", "")),
            dst_name=str(point.get("dst_name", "")),
            src_leaf=str(point.get("src_leaf", "")),
            dst_leaf=str(point.get("dst_leaf", "")),
            value=value,
            baseline=baseline,
            threshold=threshold,
            severity=severity or self._signal_severity(anomaly_type, value, baseline, threshold),
            timestamp=str(point.get("_time") or point.get("time") or _utcnow_iso()),
            samples_sent=samples_sent,
            samples_lost=samples_lost,
            sample_count=sample_count,
        )

    def _detect_latency_from_rows(self, baseline_rows: list[dict], current_rows: list[dict]) -> list:
        baseline_paths = _group_by_path(baseline_rows)
        current_paths = _group_by_path(current_rows)
        anomalies = []
        for path_key, current_points in current_paths.items():
            baseline_points = baseline_paths.get(path_key, [])
            baseline_values = [_as_float(point.get("rtt_avg")) for point in baseline_points]
            current_values = [_as_float(point.get("rtt_avg")) for point in current_points]
            baseline_values = [value for value in baseline_values if value > 0]
            current_values = [value for value in current_values if value > 0]
            if not current_values or not baseline_values:
                continue
            baseline = statistics.median(baseline_values)
            current = statistics.median(current_values)
            threshold = baseline + _MIN_LATENCY_DELTA_MS
            sample_count = len(current_values)
            if current < threshold:
                continue
            anomalies.append(
                self._new_anomaly(
                    "latency_spike",
                    current_points[0],
                    value=current,
                    baseline=baseline,
                    threshold=threshold,
                    sample_count=sample_count,
                )
            )
        return anomalies

    def _detect_loss_from_rows(self, baseline_rows: list[dict], current_rows: list[dict]) -> tuple[list, int]:
        baseline_paths = _group_by_path(baseline_rows)
        current_paths = _group_by_path(current_rows)
        anomalies = []
        insufficient_baseline = 0
        for path_key, current_points in current_paths.items():
            current = _loss_stats(current_points)
            if current is None or current["sent"] <= 0:
                continue
            baseline = _loss_stats(baseline_paths.get(path_key, []))
            if baseline is None:
                insufficient_baseline += 1
                continue
            threshold = max(self.loss_pct_threshold, baseline["loss_pct"] + self.loss_pct_delta)
            if current["lost"] >= current["sent"]:
                anomalies.append(
                    self._new_anomaly(
                        "path_unreachable",
                        current_points[0],
                        value=100.0,
                        baseline=baseline["loss_pct"],
                        threshold=100.0,
                        severity="high",
                        samples_sent=int(current["sent"]),
                        samples_lost=int(current["lost"]),
                        sample_count=int(current["samples"]),
                    )
                )
            elif current["loss_pct"] >= threshold and current["lost"] >= _MIN_PATH_LOST_PROBES:
                anomalies.append(
                    self._new_anomaly(
                        "packet_loss",
                        current_points[0],
                        value=current["loss_pct"],
                        baseline=baseline["loss_pct"],
                        threshold=threshold,
                        samples_sent=int(current["sent"]),
                        samples_lost=int(current["lost"]),
                        sample_count=int(current["samples"]),
                    )
                )
        return anomalies, insufficient_baseline

    def _detect_df_from_rows(self, baseline_rows: list[dict], current_rows: list[dict]) -> list:
        baseline_paths = _group_by_path(baseline_rows)
        current_paths = _group_by_path(current_rows)
        anomalies = []
        for path_key, current_points in current_paths.items():
            baseline_points = baseline_paths.get(path_key, [])
            baseline_df = _loss_stats(baseline_points, prefix="df_")
            current_df = _loss_stats(current_points, prefix="df_")
            baseline_rtt = _loss_stats(baseline_points)
            current_rtt = _loss_stats(current_points)
            if baseline_df is None or current_df is None or baseline_rtt is None or current_rtt is None:
                continue
            rtt_threshold = max(self.loss_pct_threshold, baseline_rtt["loss_pct"] + self.loss_pct_delta)
            rtt_healthy = (
                current_rtt["sent"] > 0 and current_rtt["loss_pct"] < rtt_threshold and current_rtt["lost"] == 0
            )
            df_loss_signal = (
                current_df["loss_pct"] >= 20.0
                and (current_df["loss_pct"] - baseline_df["loss_pct"]) >= 15.0
                and current_df["lost"] >= _MIN_PATH_LOST_PROBES
            )
            if not rtt_healthy or not df_loss_signal or current_df["mtu_drops"] > 0:
                continue
            anomalies.append(
                self._new_anomaly(
                    "mtu_or_fragmentation_suspect",
                    current_points[0],
                    value=current_df["loss_pct"],
                    baseline=baseline_df["loss_pct"],
                    threshold=max(20.0, baseline_df["loss_pct"] + 15.0),
                    severity="high" if current_df["loss_pct"] >= 50.0 else "medium",
                    samples_sent=int(current_df["sent"]),
                    samples_lost=int(current_df["lost"]),
                    sample_count=int(current_df["samples"]),
                )
            )
        return anomalies

    def analyze_snapshot_rows(self, baseline_rows: list[dict], current_rows: list[dict]) -> SnapshotAnalysis:
        loss_anomalies, insufficient_baseline = self._detect_loss_from_rows(baseline_rows, current_rows)
        anomalies = [
            *self._detect_latency_from_rows(baseline_rows, current_rows),
            *loss_anomalies,
            *self._detect_df_from_rows(baseline_rows, current_rows),
        ]
        baseline_paths = _group_by_path(baseline_rows)
        current_paths = _group_by_path(current_rows)
        absolute_unreachable_paths = 0
        absolute_loss_paths = 0
        absolute_mtu_paths = 0
        for current_points in current_paths.values():
            regular = _loss_stats(current_points)
            df = _loss_stats(current_points, prefix="df_")
            if regular is None:
                continue
            if regular["sent"] > 0 and regular["lost"] >= regular["sent"]:
                absolute_unreachable_paths += 1
            elif regular["loss_pct"] >= self.loss_pct_threshold and regular["lost"] >= _MIN_PATH_LOST_PROBES:
                absolute_loss_paths += 1
            if (
                df is not None
                and regular["loss_pct"] < self.loss_pct_threshold
                and df["loss_pct"] >= 20.0
                and df["lost"] >= _MIN_PATH_LOST_PROBES
                and df["mtu_drops"] == 0
            ):
                absolute_mtu_paths += 1
        anomalies.sort(key=lambda item: (item.type, item.src_ip, item.dst_ip))
        return SnapshotAnalysis(
            anomalies=anomalies,
            quality={
                "baseline_paths_observed": len(baseline_paths),
                "current_paths_observed": len(current_paths),
                "not_observed_paths": len(set(baseline_paths) - set(current_paths)),
                "insufficient_baseline_paths": insufficient_baseline,
                "absolute_unreachable_paths": absolute_unreachable_paths,
                "absolute_packet_loss_paths": absolute_loss_paths,
                "absolute_network_mtu_paths": absolute_mtu_paths,
                "local_df_mtu_drops": int(sum(_as_float(row.get("df_mtu_drops")) for row in current_rows)),
                "local_probe_errors": int(sum(_as_float(row.get("local_probe_errors")) for row in current_rows)),
            },
        )
