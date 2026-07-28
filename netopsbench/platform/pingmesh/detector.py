"""Pingmesh anomaly detector."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from netopsbench.models.topology import TopologyManifest
from netopsbench.platform.topology.topology_utils import coerce_topology_manifest

from ._detector_analysis import DetectorAnalysisMixin
from ._detector_coverage import DetectorCoverageMixin
from ._detector_query import DetectorQueryMixin


@dataclass
class Anomaly:
    type: str
    src_ip: str
    src_name: str
    dst_ip: str
    dst_name: str
    src_leaf: str
    dst_leaf: str
    value: float
    baseline: float
    threshold: float
    severity: str
    timestamp: str
    samples_sent: int = 0
    samples_lost: int = 0
    sample_count: int = 0
    windows_observed: list[str] = field(default_factory=list)
    persistence: str | None = None


class AnomalyDetector(DetectorQueryMixin, DetectorCoverageMixin, DetectorAnalysisMixin):
    """Detect anomalies in Pingmesh data using statistical methods."""

    _anomaly_type = Anomaly

    def __init__(
        self,
        influxdb_url: str,
        token: str,
        org: str,
        bucket: str,
        topology_metadata: TopologyManifest | dict | None = None,
        topology_id: str | None = None,
        loss_pct_threshold: float = 5.0,
        loss_pct_delta: float = 5.0,
    ):
        self.influxdb_url = influxdb_url
        self.token = token
        self.org = org
        self.bucket = bucket
        self.client_to_leaf: dict[str, str] = {}
        self.loss_pct_threshold = float(loss_pct_threshold)
        self.loss_pct_delta = float(loss_pct_delta)
        self._pingmesh_clients: list[str] = []
        self._pingmesh_policy: dict = {}
        if topology_metadata is None:
            raise ValueError("Canonical topology_metadata is required for Pingmesh anomaly detection")
        manifest = coerce_topology_manifest(topology_metadata)
        self.topology_id = topology_id or manifest.topology_id
        projected_topology = manifest.to_agent_topology()
        self._pingmesh_clients = [device.name for device in manifest.clients()]
        self._pingmesh_policy = dict(projected_topology["pingmesh"])
        self._load_topology_metadata(projected_topology)

    def _anomaly_to_dict(self, anomaly: Anomaly) -> dict:
        return asdict(anomaly)

    @staticmethod
    def _anomaly_family(anomaly: Anomaly) -> str:
        if anomaly.type in {"packet_loss", "path_unreachable"}:
            return "loss"
        return anomaly.type

    def _aggregate_anomalies(self, anomalies: list[Anomaly]) -> dict:
        by_src_leaf: dict[str, dict[str, int]] = {}
        by_dst_leaf: dict[str, dict[str, int]] = {}
        keys = ("drop_count", "latency_spikes", "path_unreachable", "mtu_suspects")
        for anomaly in anomalies:
            src_leaf = self._resolve_leaf(anomaly.src_leaf, anomaly.src_name)
            dst_leaf = self._resolve_leaf(anomaly.dst_leaf, anomaly.dst_name)
            by_src_leaf.setdefault(src_leaf, dict.fromkeys(keys, 0))
            by_dst_leaf.setdefault(dst_leaf, dict.fromkeys(keys, 0))
            if anomaly.type in ("packet_loss", "path_unreachable"):
                key = "path_unreachable" if anomaly.type == "path_unreachable" else "drop_count"
                by_src_leaf[src_leaf][key] += 1
                by_dst_leaf[dst_leaf][key] += 1
            elif anomaly.type == "mtu_or_fragmentation_suspect":
                by_src_leaf[src_leaf]["mtu_suspects"] += 1
                by_dst_leaf[dst_leaf]["mtu_suspects"] += 1
            else:
                by_src_leaf[src_leaf]["latency_spikes"] += 1
                by_dst_leaf[dst_leaf]["latency_spikes"] += 1

        return {"by_src_leaf": by_src_leaf, "by_dst_leaf": by_dst_leaf}

    def _build_report(
        self,
        *,
        anomalies: list[Anomaly],
        baseline_start: str,
        baseline_end: str,
        current_start: str,
        current_end: str,
        query_status: dict,
        coverage: dict,
        quality: dict,
    ) -> dict:
        latency = [item for item in anomalies if item.type == "latency_spike"]
        regular_loss = [item for item in anomalies if item.type == "packet_loss"]
        unreachable = [item for item in anomalies if item.type == "path_unreachable"]
        mtu = [item for item in anomalies if item.type == "mtu_or_fragmentation_suspect"]
        now_utc = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S") + "Z"
        return {
            "timestamp": now_utc,
            "windows": {
                "baseline": {"start": baseline_start, "end": baseline_end},
                "current": {"start": current_start, "end": current_end},
            },
            "query_status": query_status,
            "coverage": coverage,
            "quality": quality,
            "summary": {
                "total_anomalies": len(anomalies),
                "latency_spikes": len(latency),
                "packet_loss_events": len(regular_loss),
                "path_unreachable_events": len(unreachable),
                "mtu_or_fragmentation_events": len(mtu),
            },
            "anomalies": [self._anomaly_to_dict(item) for item in anomalies],
            "returned_anomalies": len(anomalies),
            "truncated": False,
            "aggregated_anomalies": self._aggregate_anomalies(anomalies),
        }

    @staticmethod
    def _error_status(*results) -> dict:
        errors = [result.error for result in results if result.status != "ok" and result.error]
        failed = any(result.status != "ok" for result in results)
        return {
            "ok": not failed,
            "error": "; ".join(dict.fromkeys(errors)) if errors else ("query_failed" if failed else None),
        }

    @staticmethod
    def _slice_rows(rows: list[dict], start_time: str, end_time: str) -> list[dict]:
        start = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
        end = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
        selected = []
        for row in rows:
            raw_time = str(row.get("_time") or "")
            if not raw_time:
                continue
            try:
                timestamp = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
            except ValueError:
                continue
            if start <= timestamp < end:
                selected.append(row)
        return selected

    def _merge_window_anomalies(self, analyses: list[tuple[str, list[Anomaly]]]) -> list[Anomaly]:
        full: dict[tuple[str, str, str], Anomaly] = {}
        seen_windows: dict[tuple[str, str, str], set[str]] = {}
        for window_name, anomalies in analyses:
            if window_name != "full":
                continue
            for anomaly in anomalies:
                key = (self._anomaly_family(anomaly), anomaly.src_ip, anomaly.dst_ip)
                full[key] = anomaly
                seen_windows.setdefault(key, set()).add("full")
        for window_name, anomalies in analyses:
            if window_name == "full":
                continue
            for anomaly in anomalies:
                key = (self._anomaly_family(anomaly), anomaly.src_ip, anomaly.dst_ip)
                if key in full:
                    seen_windows.setdefault(key, set()).add(window_name)
        for key, anomaly in full.items():
            windows = sorted(seen_windows[key])
            anomaly.windows_observed = windows
            if "early" in windows and "steady" in windows:
                anomaly.persistence = "persistent"
            elif "early" in windows:
                anomaly.persistence = "early_only"
            elif "steady" in windows:
                anomaly.persistence = "steady_only"
            else:
                anomaly.persistence = "full_window"
        return sorted(full.values(), key=lambda item: (item.type, item.src_ip, item.dst_ip))

    def _query_covered_snapshot(self, start_time: str, end_time: str):
        snapshot = self._query_snapshot(start_time, end_time)
        if snapshot.status != "ok":
            return snapshot, {
                "status": "error",
                "coverage_status": "error",
                "error": snapshot.error or "query_failed",
            }
        coverage = self.summarize_coverage(snapshot.rows)
        expected_seconds = int(coverage.get("expected_epoch_cycles", 0)) * int(
            self._pingmesh_policy.get("cycle_interval_seconds", 1)
        )
        actual_seconds = max(
            0.0,
            (
                datetime.fromisoformat(end_time.replace("Z", "+00:00"))
                - datetime.fromisoformat(start_time.replace("Z", "+00:00"))
            ).total_seconds(),
        )
        for _attempt in range(2):
            if actual_seconds < expected_seconds or coverage.get("coverage_status") == "complete":
                break
            time.sleep(max(1, int(self._pingmesh_policy.get("cycle_interval_seconds", 1))))
            snapshot = self._query_snapshot(start_time, end_time)
            if snapshot.status != "ok":
                return snapshot, {
                    "status": "error",
                    "coverage_status": "error",
                    "error": snapshot.error or "query_failed",
                }
            coverage = self.summarize_coverage(snapshot.rows)
        return snapshot, coverage

    def generate_windowed_anomaly_report(
        self,
        *,
        baseline_start: str,
        baseline_end: str,
        current_start: str,
        current_end: str,
        windows: list[dict],
        include_internal_health: bool = False,
    ) -> dict:
        baseline, baseline_coverage = self._query_covered_snapshot(baseline_start, baseline_end)
        current, current_coverage = self._query_covered_snapshot(current_start, current_end)
        query_status = self._error_status(baseline, current)
        if not query_status["ok"]:
            return self._build_report(
                anomalies=[],
                baseline_start=baseline_start,
                baseline_end=baseline_end,
                current_start=current_start,
                current_end=current_end,
                query_status=query_status,
                coverage={"status": "error", "coverage_status": "error", "error": query_status["error"]},
                quality={},
            )

        full_analysis = self.analyze_snapshot_rows(baseline.rows, current.rows)
        window_analyses: list[tuple[str, list[Anomaly]]] = [("full", full_analysis.anomalies)]
        for window in windows:
            name = str(window.get("name") or "window")
            rows = self._slice_rows(current.rows, str(window["start_time"]), str(window["end_time"]))
            window_analyses.append((name, self.analyze_snapshot_rows(baseline.rows, rows).anomalies))
        anomalies = self._merge_window_anomalies(window_analyses)
        report = self._build_report(
            anomalies=anomalies,
            baseline_start=baseline_start,
            baseline_end=baseline_end,
            current_start=current_start,
            current_end=current_end,
            query_status=query_status,
            coverage=current_coverage,
            quality=full_analysis.quality,
        )
        absolute_health = {
            name: int(report["quality"].pop(name, 0) or 0)
            for name in (
                "absolute_unreachable_paths",
                "absolute_packet_loss_paths",
                "absolute_network_mtu_paths",
            )
        }
        if include_internal_health:
            report["_baseline_health"] = absolute_health
            report["_baseline_coverage"] = baseline_coverage
        return report
