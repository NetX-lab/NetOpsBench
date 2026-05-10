"""Analysis helpers for Pingmesh anomaly detector."""

from __future__ import annotations

import statistics
from datetime import UTC, datetime


def _utcnow_iso() -> str:
    """Return current UTC time in ISO-8601 format with Z suffix."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S") + "Z"


# Absolute minimum latency increase (ms) to avoid false positives on low-baseline paths.
_MIN_LATENCY_ABS_THRESHOLD_MS = 2.0

# Samples at/above this loss percentage are treated as "unconverged / unreachable"
# and are excluded from the baseline mean. Without this filter, BGP startup samples
# (routing not yet established → 100% loss) pollute the baseline and push the
# anomaly threshold so high that later real faults (e.g. packet_corruption at
# 10-25% loss) become undetectable. 95 is a conservative cutoff: normal noise
# rarely exceeds ~70%, while un-routed paths are always 100%.
_BASELINE_UNREACHABLE_LOSS_PCT = 95.0


def _filter_converged_loss(points: list[dict]) -> list[dict]:
    """Drop baseline samples that reflect pre-BGP-convergence state.

    A sample with ``loss_pct >= 95`` almost certainly means the path was
    unreachable (routing not yet installed), not that the link is bad —
    so it should not influence the baseline mean used to compute the
    packet-loss detection threshold.
    """
    return [p for p in points if p.get("value", 0.0) < _BASELINE_UNREACHABLE_LOSS_PCT]


def _filter_converged_rtt(points: list[dict]) -> list[dict]:
    """Drop baseline samples where ``rtt_avg == 0`` (UDP burst returned no
    successful probes — a pre-convergence sentinel, not a legitimate RTT)."""
    return [p for p in points if p.get("value", 0.0) > 0.0]


class DetectorAnalysisMixin:
    @staticmethod
    def _safe_flux_string(value: str) -> str:
        """Escape a string for safe interpolation into a Flux query literal."""
        return str(value).replace("\\", "\\\\").replace('"', '\\"')

    def _group_by_path(self, data: list[dict]) -> dict[tuple, list[dict]]:
        paths = {}
        for row in data:
            key = (row.get("src_ip", ""), row.get("dst_ip", ""))
            paths.setdefault(key, []).append(row)
        return paths

    def _calculate_severity(self, value: float, threshold: float) -> str:
        if threshold == 0:
            return "high"
        ratio = value / threshold
        if ratio > 2:
            return "high"
        if ratio > 1.5:
            return "medium"
        return "low"

    def _resolve_leaf(self, leaf_tag: str, client_name: str) -> str:
        if isinstance(leaf_tag, str) and leaf_tag:
            return leaf_tag
        if isinstance(client_name, str) and client_name in self.client_to_leaf:
            return self.client_to_leaf[client_name]
        return "unknown"

    def detect_latency_anomalies(self, baseline_start: str, baseline_end: str, current_start: str, current_end: str):
        topology_filter = self._topology_filter()
        _b = self._safe_flux_string(self.bucket)
        _bs = self._safe_flux_string(baseline_start)
        _be = self._safe_flux_string(baseline_end)
        _cs = self._safe_flux_string(current_start)
        _ce = self._safe_flux_string(current_end)
        baseline_query = f'\nfrom(bucket: "{_b}")\n  |> range(start: time(v: "{_bs}"), stop: time(v: "{_be}"))\n  |> filter(fn: (r) => r._measurement == "pingmesh")\n{topology_filter}  |> filter(fn: (r) => r._field == "rtt_avg")\n  |> group(columns: ["src_ip", "dst_ip", "src_name", "dst_name", "src_leaf", "dst_leaf"])\n'
        current_query = f'\nfrom(bucket: "{_b}")\n  |> range(start: time(v: "{_cs}"), stop: time(v: "{_ce}"))\n  |> filter(fn: (r) => r._measurement == "pingmesh")\n{topology_filter}  |> filter(fn: (r) => r._field == "rtt_avg")\n  |> group(columns: ["src_ip", "dst_ip", "src_name", "dst_name", "src_leaf", "dst_leaf"])\n'
        baseline_data = self._query_influxdb(baseline_query)
        current_data = self._query_influxdb(current_query)
        if not baseline_data or not current_data:
            return []
        baseline_paths = self._group_by_path(baseline_data)
        current_paths = self._group_by_path(current_data)
        anomalies = []
        for path_key, current_points in current_paths.items():
            baseline_points = baseline_paths.get(path_key, [])
            if len(current_points) < 1 or len(baseline_points) < 1:
                continue
            # Drop rtt_avg == 0 baseline samples: these are the sentinel written
            # when a UDP burst had zero successful probes (pre-BGP-convergence
            # or path briefly unreachable), not a real 0 ms RTT. Mixing them
            # with healthy RTTs understates the baseline mean and inflates
            # the stddev → threshold, masking real latency spikes.
            clean_baseline = _filter_converged_rtt(baseline_points)
            if not clean_baseline:
                continue
            baseline_values = [point["value"] for point in clean_baseline]
            current_values = [point["value"] for point in current_points]
            baseline = statistics.mean(baseline_values)
            stddev = statistics.stdev(baseline_values) if len(baseline_values) > 1 else 0
            current = statistics.mean(current_values)
            # P2-fix: ensure minimum absolute threshold to avoid false positives on low-baseline paths
            threshold = max(baseline + 3 * stddev, baseline + _MIN_LATENCY_ABS_THRESHOLD_MS)
            min_multiplier = 1.3
            min_abs_increase = 0.0
            if len(baseline_values) < 3 or len(current_values) < 2:
                # With 1 s probe cycles short-window cases are rare; keep a
                # modest guard so single noisy samples cannot trigger.
                min_multiplier = 1.5
                min_abs_increase = 2.0
            if current > threshold and current > baseline * min_multiplier and (current - baseline) >= min_abs_increase:
                first_point = current_points[0]
                anomalies.append(
                    self._anomaly_type(
                        type="latency_spike",
                        src_ip=first_point.get("src_ip", ""),
                        src_name=first_point.get("src_name", ""),
                        dst_ip=first_point.get("dst_ip", ""),
                        dst_name=first_point.get("dst_name", ""),
                        src_leaf=first_point.get("src_leaf", ""),
                        dst_leaf=first_point.get("dst_leaf", ""),
                        value=current,
                        baseline=baseline,
                        threshold=threshold,
                        severity=self._calculate_severity(current, threshold),
                        timestamp=_utcnow_iso(),
                    )
                )
        return anomalies

    def detect_jitter_anomalies(self, baseline_start: str, baseline_end: str, current_start: str, current_end: str):
        """Detect paths where RTT variance increased significantly (e.g. packet corruption)."""
        topology_filter = self._topology_filter()
        # Query both rtt_min and rtt_max to compute per-probe jitter
        fields_filter = '|> filter(fn: (r) => r._field == "rtt_avg" or r._field == "rtt_max" or r._field == "rtt_min")'
        _b = self._safe_flux_string(self.bucket)
        _bs = self._safe_flux_string(baseline_start)
        _be = self._safe_flux_string(baseline_end)
        _cs = self._safe_flux_string(current_start)
        _ce = self._safe_flux_string(current_end)
        baseline_query = f'\nfrom(bucket: "{_b}")\n  |> range(start: time(v: "{_bs}"), stop: time(v: "{_be}"))\n  |> filter(fn: (r) => r._measurement == "pingmesh")\n{topology_filter}  {fields_filter}\n  |> group(columns: ["src_ip", "dst_ip", "src_name", "dst_name", "src_leaf", "dst_leaf", "_field"])\n'
        current_query = f'\nfrom(bucket: "{_b}")\n  |> range(start: time(v: "{_cs}"), stop: time(v: "{_ce}"))\n  |> filter(fn: (r) => r._measurement == "pingmesh")\n{topology_filter}  {fields_filter}\n  |> group(columns: ["src_ip", "dst_ip", "src_name", "dst_name", "src_leaf", "dst_leaf", "_field"])\n'
        baseline_data = self._query_influxdb(baseline_query)
        current_data = self._query_influxdb(current_query)
        if not baseline_data or not current_data:
            return []

        def _build_jitter_map(data: list[dict]) -> dict[tuple, list[float]]:
            """Group by path and compute per-sample jitter (rtt_max - rtt_min)."""
            fields_by_path_time: dict[tuple, dict[str, dict[str, float]]] = {}
            for row in data:
                path_key = (row.get("src_ip", ""), row.get("dst_ip", ""))
                ts = row.get("_time", row.get("time", ""))
                field = row.get("_field", "")
                fields_by_path_time.setdefault(path_key, {}).setdefault(ts, {})[field] = row.get("value", 0.0)
            jitter_map: dict[tuple, list[float]] = {}
            for path_key, timestamps in fields_by_path_time.items():
                jitters = []
                for _ts, fields in timestamps.items():
                    rtt_max = fields.get("rtt_max", 0.0)
                    rtt_min = fields.get("rtt_min", 0.0)
                    if rtt_max > 0 and rtt_min > 0:
                        jitters.append(rtt_max - rtt_min)
                if jitters:
                    jitter_map[path_key] = jitters
            return jitter_map

        # Also collect metadata (src_name, dst_name, etc.) from the first point of each path
        path_metadata: dict[tuple, dict] = {}
        for row in current_data:
            key = (row.get("src_ip", ""), row.get("dst_ip", ""))
            if key not in path_metadata:
                path_metadata[key] = row

        baseline_jitter = _build_jitter_map(baseline_data)
        current_jitter = _build_jitter_map(current_data)

        anomalies = []
        for path_key, current_jitters in current_jitter.items():
            baseline_jitters = baseline_jitter.get(path_key, [])
            if not baseline_jitters or not current_jitters:
                continue
            baseline_mean = statistics.mean(baseline_jitters)
            current_mean = statistics.mean(current_jitters)
            baseline_std = statistics.stdev(baseline_jitters) if len(baseline_jitters) > 1 else 0
            # Require: current jitter > baseline + 3*stddev, at least 2ms absolute increase, and 2x multiplier
            threshold = max(baseline_mean + 3 * baseline_std, baseline_mean + 2.0)
            if current_mean > threshold and current_mean > baseline_mean * 2.0:
                meta = path_metadata.get(path_key, {})
                anomalies.append(
                    self._anomaly_type(
                        type="jitter_spike",
                        src_ip=meta.get("src_ip", ""),
                        src_name=meta.get("src_name", ""),
                        dst_ip=meta.get("dst_ip", ""),
                        dst_name=meta.get("dst_name", ""),
                        src_leaf=meta.get("src_leaf", ""),
                        dst_leaf=meta.get("dst_leaf", ""),
                        value=current_mean,
                        baseline=baseline_mean,
                        threshold=threshold,
                        severity=self._calculate_severity(current_mean, threshold),
                        timestamp=_utcnow_iso(),
                    )
                )
        return anomalies

    def detect_packet_loss(self, baseline_start: str, baseline_end: str, current_start: str, current_end: str):
        topology_filter = self._topology_filter()
        _b = self._safe_flux_string(self.bucket)
        _bs = self._safe_flux_string(baseline_start)
        _be = self._safe_flux_string(baseline_end)
        _cs = self._safe_flux_string(current_start)
        _ce = self._safe_flux_string(current_end)
        baseline_query = f'\nfrom(bucket: "{_b}")\n  |> range(start: time(v: "{_bs}"), stop: time(v: "{_be}"))\n  |> filter(fn: (r) => r._measurement == "pingmesh")\n{topology_filter}  |> filter(fn: (r) => r._field == "packet_loss")\n  |> group(columns: ["src_ip", "dst_ip", "src_name", "dst_name", "src_leaf", "dst_leaf"])\n'
        current_query = f'\nfrom(bucket: "{_b}")\n  |> range(start: time(v: "{_cs}"), stop: time(v: "{_ce}"))\n  |> filter(fn: (r) => r._measurement == "pingmesh")\n{topology_filter}  |> filter(fn: (r) => r._field == "packet_loss")\n  |> group(columns: ["src_ip", "dst_ip", "src_name", "dst_name", "src_leaf", "dst_leaf"])\n'
        baseline_data = self._query_influxdb(baseline_query)
        current_data = self._query_influxdb(current_query)

        baseline_paths = self._group_by_path(baseline_data) if baseline_data else {}
        current_paths = self._group_by_path(current_data) if current_data else {}

        anomalies = []

        # P0-fix: detect paths that had baseline data but disappeared in current window
        # (complete connectivity loss — link_down, blackhole, etc.)
        if baseline_paths:
            for path_key, baseline_points in baseline_paths.items():
                if path_key in current_paths:
                    continue  # path still exists, will be checked below
                if len(baseline_points) < 1:
                    continue
                first_point = baseline_points[0]
                anomalies.append(
                    self._anomaly_type(
                        type="path_unreachable",
                        src_ip=first_point.get("src_ip", ""),
                        src_name=first_point.get("src_name", ""),
                        dst_ip=first_point.get("dst_ip", ""),
                        dst_name=first_point.get("dst_name", ""),
                        src_leaf=first_point.get("src_leaf", ""),
                        dst_leaf=first_point.get("dst_leaf", ""),
                        value=100.0,
                        baseline=statistics.mean([p["value"] for p in baseline_points]),
                        threshold=100.0,
                        severity="high",
                        timestamp=_utcnow_iso(),
                    )
                )

        for path_key, current_points in current_paths.items():
            baseline_points = baseline_paths.get(path_key, [])
            if len(current_points) < 1:
                continue
            # Drop unreachable (>=95%) samples from baseline: they represent
            # pre-BGP-convergence state, not real path quality. Without this
            # filter, 100% loss samples from startup inflate baseline_loss
            # and raise `threshold` above the fault's real loss signal.
            clean_baseline = _filter_converged_loss(baseline_points)
            baseline_loss = statistics.mean([point["value"] for point in clean_baseline]) if clean_baseline else 0.0
            current_loss = statistics.mean([point["value"] for point in current_points])
            threshold = max(self.loss_pct_threshold, baseline_loss + self.loss_pct_delta)
            if current_loss >= threshold:
                first_point = current_points[0]
                anomalies.append(
                    self._anomaly_type(
                        type="packet_loss",
                        src_ip=first_point.get("src_ip", ""),
                        src_name=first_point.get("src_name", ""),
                        dst_ip=first_point.get("dst_ip", ""),
                        dst_name=first_point.get("dst_name", ""),
                        src_leaf=first_point.get("src_leaf", ""),
                        dst_leaf=first_point.get("dst_leaf", ""),
                        value=current_loss,
                        baseline=baseline_loss,
                        threshold=threshold,
                        severity=self._calculate_severity(current_loss, threshold),
                        timestamp=_utcnow_iso(),
                    )
                )
        return anomalies

    def detect_df_fragmentation_anomalies(
        self, baseline_start: str, baseline_end: str, current_start: str, current_end: str
    ):
        topology_filter = self._topology_filter()
        _b = self._safe_flux_string(self.bucket)
        _bs = self._safe_flux_string(baseline_start)
        _be = self._safe_flux_string(baseline_end)
        _cs = self._safe_flux_string(current_start)
        _ce = self._safe_flux_string(current_end)
        baseline_query = f'\nfrom(bucket: "{_b}")\n  |> range(start: time(v: "{_bs}"), stop: time(v: "{_be}"))\n  |> filter(fn: (r) => r._measurement == "pingmesh")\n{topology_filter}  |> filter(fn: (r) => r._field == "df_loss_pct")\n  |> group(columns: ["src_ip", "dst_ip", "src_name", "dst_name", "src_leaf", "dst_leaf"])\n'
        current_query = f'\nfrom(bucket: "{_b}")\n  |> range(start: time(v: "{_cs}"), stop: time(v: "{_ce}"))\n  |> filter(fn: (r) => r._measurement == "pingmesh")\n{topology_filter}  |> filter(fn: (r) => r._field == "df_loss_pct")\n  |> group(columns: ["src_ip", "dst_ip", "src_name", "dst_name", "src_leaf", "dst_leaf"])\n'
        baseline_data = self._query_influxdb(baseline_query)
        current_data = self._query_influxdb(current_query)
        if not baseline_data or not current_data:
            return []
        baseline_paths = self._group_by_path(baseline_data)
        current_paths = self._group_by_path(current_data)
        anomalies = []
        for path_key, current_points in current_paths.items():
            baseline_points = baseline_paths.get(path_key, [])
            if len(current_points) < 1 or len(baseline_points) < 1:
                continue
            # Same pre-convergence guard as packet-loss: drop 100% df_loss_pct
            # startup samples so they don't inflate the baseline.
            clean_baseline = _filter_converged_loss(baseline_points)
            baseline_loss = statistics.mean([point["value"] for point in clean_baseline]) if clean_baseline else 0.0
            current_loss = statistics.mean([point["value"] for point in current_points])
            if current_loss >= 20.0 and (current_loss - baseline_loss) >= 15.0:
                first_point = current_points[0]
                anomalies.append(
                    self._anomaly_type(
                        type="mtu_or_fragmentation_suspect",
                        src_ip=first_point.get("src_ip", ""),
                        src_name=first_point.get("src_name", ""),
                        dst_ip=first_point.get("dst_ip", ""),
                        dst_name=first_point.get("dst_name", ""),
                        src_leaf=first_point.get("src_leaf", ""),
                        dst_leaf=first_point.get("dst_leaf", ""),
                        value=current_loss,
                        baseline=baseline_loss,
                        threshold=max(20.0, baseline_loss + 15.0),
                        severity="high" if current_loss >= 50.0 else "medium",
                        timestamp=_utcnow_iso(),
                    )
                )
        return anomalies
