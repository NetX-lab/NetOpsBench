"""Pure snapshot analysis helpers for the Pingmesh anomaly detector."""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import UTC, datetime
from math import ceil
from typing import Any

_MIN_LATENCY_DELTA_MS = 20.0
_MIN_ELEVATED_PORT_BATCHES = 2
_MIN_PATH_LOST_PROBES = 3
_MIN_ENDPOINT_LOSS_DELTA_PCT = 1.0
_MIN_ENDPOINT_LOST_PROBES = 2
_SUSTAINED_SAMPLE_FRACTION = 0.75
_BASELINE_UNREACHABLE_LOSS_PCT = 95.0


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


def _median_rtt_by_port_batch(rows: list[dict]) -> dict[int, tuple[float, int]]:
    batches: dict[int, list[float]] = {}
    for row in rows:
        value = _as_float(row.get("rtt_avg"))
        if value <= 0 or row.get("port_batch_index") is None:
            continue
        try:
            batch = int(float(row["port_batch_index"]))
        except (TypeError, ValueError):
            continue
        batches.setdefault(batch, []).append(value)
    return {batch: (statistics.median(values), len(values)) for batch, values in batches.items()}


def _loss_stats(rows: list[dict], *, prefix: str = "", drop_unreachable: bool = False) -> dict | None:
    sent_field = f"{prefix}packets_sent"
    lost_field = f"{prefix}packets_lost"
    pct_field = "packet_loss" if not prefix else f"{prefix}loss_pct"
    counted: list[tuple[float, float]] = []
    percentages: list[float] = []
    sample_count = 0
    mtu_drops = 0.0

    for row in rows:
        sent = _as_float(row.get(sent_field))
        lost = _as_float(row.get(lost_field))
        if sent > 0:
            loss_pct = (lost / sent) * 100.0
            if drop_unreachable and loss_pct >= _BASELINE_UNREACHABLE_LOSS_PCT:
                continue
            counted.append((sent, lost))
            sample_count += 1
        elif row.get(pct_field) is not None:
            loss_pct = _as_float(row.get(pct_field))
            if drop_unreachable and loss_pct >= _BASELINE_UNREACHABLE_LOSS_PCT:
                continue
            percentages.append(loss_pct)
            sample_count += 1
        if prefix:
            mtu_drops += _as_float(row.get("df_mtu_drops"))

    if not counted and not percentages:
        return None
    total_sent = sum(sent for sent, _lost in counted)
    total_lost = sum(lost for _sent, lost in counted)
    loss_pct = (total_lost / total_sent) * 100.0 if total_sent > 0 else statistics.mean(percentages)
    return {
        "loss_pct": loss_pct,
        "sent": total_sent,
        "lost": total_lost,
        "samples": sample_count,
        "has_counts": bool(counted),
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
        if anomaly_type == "jitter_spike":
            if increase >= 20.0:
                return "high"
            if increase >= 5.0:
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
            timestamp=_utcnow_iso(),
            samples_sent=samples_sent,
            samples_lost=samples_lost,
            sample_count=sample_count,
        )

    def _elevated_loss_endpoints(
        self,
        baseline_rows: list[dict],
        current_rows: list[dict],
        *,
        leaf_field: str,
        client_field: str,
    ) -> set[str]:
        def aggregate(rows: list[dict]) -> dict[str, tuple[float, float]]:
            totals: dict[str, list[float]] = {}
            for row in rows:
                sent = _as_float(row.get("packets_sent"))
                if sent <= 0:
                    continue
                leaf = self._resolve_leaf(str(row.get(leaf_field, "")), str(row.get(client_field, "")))
                if leaf == "unknown":
                    continue
                current = totals.setdefault(leaf, [0.0, 0.0])
                current[0] += sent
                current[1] += _as_float(row.get("packets_lost"))
            return {leaf: (values[0], values[1]) for leaf, values in totals.items()}

        baseline = aggregate(baseline_rows)
        current = aggregate(current_rows)
        elevated = set()
        for leaf, (sent, lost) in current.items():
            baseline_sent, baseline_lost = baseline.get(leaf, (0.0, 0.0))
            if sent <= 0 or baseline_sent <= 0 or lost < _MIN_ENDPOINT_LOST_PROBES:
                continue
            loss_pct = lost / sent * 100.0
            baseline_pct = baseline_lost / baseline_sent * 100.0
            if loss_pct - baseline_pct >= _MIN_ENDPOINT_LOSS_DELTA_PCT:
                elevated.add(leaf)
        return elevated

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
                baseline_batches = _median_rtt_by_port_batch(baseline_points)
                current_batches = _median_rtt_by_port_batch(current_points)
                elevated = sorted(
                    (
                        current_batch[0] - baseline_batches[batch][0],
                        baseline_batches[batch][0],
                        current_batch[0],
                        current_batch[1],
                    )
                    for batch, current_batch in current_batches.items()
                    if batch in baseline_batches
                    and current_batch[0] - baseline_batches[batch][0] >= _MIN_LATENCY_DELTA_MS
                )
                if len(elevated) < _MIN_ELEVATED_PORT_BATCHES:
                    continue
                _delta, baseline, current, _count = elevated[-_MIN_ELEVATED_PORT_BATCHES]
                threshold = baseline + _MIN_LATENCY_DELTA_MS
                sample_count = sum(item[3] for item in elevated[-_MIN_ELEVATED_PORT_BATCHES:])
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

    def _detect_jitter_from_rows(self, baseline_rows: list[dict], current_rows: list[dict]) -> list:
        baseline_paths = _group_by_path(baseline_rows)
        current_paths = _group_by_path(current_rows)
        anomalies = []
        for path_key, current_points in current_paths.items():
            baseline_points = baseline_paths.get(path_key, [])
            baseline_values = [
                _as_float(point.get("rtt_max")) - _as_float(point.get("rtt_min"))
                for point in baseline_points
                if _as_float(point.get("rtt_max")) > 0 and _as_float(point.get("rtt_min")) > 0
            ]
            current_values = [
                _as_float(point.get("rtt_max")) - _as_float(point.get("rtt_min"))
                for point in current_points
                if _as_float(point.get("rtt_max")) > 0 and _as_float(point.get("rtt_min")) > 0
            ]
            if not baseline_values or not current_values:
                continue
            baseline = statistics.mean(baseline_values)
            current = statistics.mean(current_values)
            baseline_std = statistics.stdev(baseline_values) if len(baseline_values) > 1 else 0.0
            threshold = max(baseline + 3 * baseline_std, baseline + 2.0)
            elevated = [value for value in current_values if value > threshold and value > baseline * 2.0]
            required = ceil(len(current_values) * _SUSTAINED_SAMPLE_FRACTION)
            if current > threshold and current > baseline * 2.0 and len(elevated) >= required:
                anomalies.append(
                    self._new_anomaly(
                        "jitter_spike",
                        current_points[0],
                        value=current,
                        baseline=baseline,
                        threshold=threshold,
                        severity=(
                            self._signal_severity("jitter_spike", current, baseline, threshold)
                            if len(current_values) >= 4 and len(elevated) == len(current_values)
                            else "low"
                        ),
                        sample_count=len(current_values),
                    )
                )
        return anomalies

    def _detect_loss_from_rows(self, baseline_rows: list[dict], current_rows: list[dict]) -> tuple[list, int]:
        baseline_paths = _group_by_path(baseline_rows)
        current_paths = _group_by_path(current_rows)
        elevated_sources = self._elevated_loss_endpoints(
            baseline_rows,
            current_rows,
            leaf_field="src_leaf",
            client_field="src_name",
        )
        elevated_destinations = self._elevated_loss_endpoints(
            baseline_rows,
            current_rows,
            leaf_field="dst_leaf",
            client_field="dst_name",
        )
        anomalies = []
        insufficient_baseline = 0
        for path_key, current_points in current_paths.items():
            current = _loss_stats(current_points)
            if current is None or (current["has_counts"] and current["sent"] <= 0):
                continue
            baseline = _loss_stats(baseline_paths.get(path_key, []), drop_unreachable=True)
            if baseline is None:
                insufficient_baseline += 1
                continue
            threshold = max(self.loss_pct_threshold, baseline["loss_pct"] + self.loss_pct_delta)
            if current["has_counts"] and current["sent"] > 0 and current["lost"] >= current["sent"]:
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
            elif current["loss_pct"] >= threshold and (
                not current["has_counts"]
                or current["lost"] >= _MIN_PATH_LOST_PROBES
                or (
                    current["lost"] > 0
                    and (
                        self._resolve_leaf(
                            str(current_points[0].get("src_leaf", "")),
                            str(current_points[0].get("src_name", "")),
                        )
                        in elevated_sources
                        or self._resolve_leaf(
                            str(current_points[0].get("dst_leaf", "")),
                            str(current_points[0].get("dst_name", "")),
                        )
                        in elevated_destinations
                    )
                )
            ):
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
            baseline_df = _loss_stats(baseline_points, prefix="df_", drop_unreachable=True)
            current_df = _loss_stats(current_points, prefix="df_")
            baseline_rtt = _loss_stats(baseline_points, drop_unreachable=True)
            current_rtt = _loss_stats(current_points)
            if baseline_df is None or current_df is None or baseline_rtt is None or current_rtt is None:
                continue
            rtt_threshold = max(self.loss_pct_threshold, baseline_rtt["loss_pct"] + self.loss_pct_delta)
            rtt_healthy = current_rtt["sent"] > 0 and current_rtt["loss_pct"] < rtt_threshold
            df_loss_signal = (
                current_df["loss_pct"] >= 20.0
                and (current_df["loss_pct"] - baseline_df["loss_pct"]) >= 15.0
                and (not current_df["has_counts"] or current_df["lost"] >= 2)
            )
            if not rtt_healthy or not (df_loss_signal or current_df["mtu_drops"] > 0):
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
            *self._detect_jitter_from_rows(baseline_rows, current_rows),
        ]
        baseline_paths = _group_by_path(baseline_rows)
        current_paths = _group_by_path(current_rows)
        anomalies.sort(key=lambda item: (item.type, item.src_ip, item.dst_ip))
        return SnapshotAnalysis(
            anomalies=anomalies,
            quality={
                "baseline_paths_observed": len(baseline_paths),
                "current_paths_observed": len(current_paths),
                "not_observed_paths": len(set(baseline_paths) - set(current_paths)),
                "insufficient_baseline_paths": insufficient_baseline,
            },
        )
