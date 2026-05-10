"""Pingmesh anomaly detector."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from ._detector_analysis import DetectorAnalysisMixin
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


class AnomalyDetector(DetectorQueryMixin, DetectorAnalysisMixin):
    """Detect anomalies in Pingmesh data using statistical methods."""

    _anomaly_type = Anomaly

    def __init__(
        self,
        influxdb_url: str,
        token: str,
        org: str,
        bucket: str,
        topology_metadata: dict | None = None,
        topology_id: str | None = None,
        loss_pct_threshold: float = 5.0,
        loss_pct_delta: float = 5.0,
    ):
        self.influxdb_url = influxdb_url
        self.token = token
        self.org = org
        self.bucket = bucket
        self.last_query_error: str | None = None
        self.client_to_leaf: dict[str, str] = {}
        self.leaf_to_spines: dict[str, list[str]] = {}
        self.topology_id = topology_id
        self.loss_pct_threshold = float(loss_pct_threshold)
        self.loss_pct_delta = float(loss_pct_delta)
        if isinstance(topology_metadata, dict):
            self._load_topology_metadata(topology_metadata)
        else:
            auto_metadata = self._load_topology_metadata_from_disk()
            if isinstance(auto_metadata, dict):
                self._load_topology_metadata(auto_metadata)

    def _anomaly_to_dict(self, anomaly: Anomaly) -> dict:
        return asdict(anomaly)

    def _infer_spines_for_cross_rack(self, src_leaf: str, dst_leaf: str) -> list[str]:
        """Return the set of spines shared between two leafs (for cross-rack paths)."""
        if not self.leaf_to_spines:
            return []
        src_spines = set(self.leaf_to_spines.get(src_leaf, []))
        dst_spines = set(self.leaf_to_spines.get(dst_leaf, []))
        return sorted(src_spines & dst_spines) if (src_spines and dst_spines) else sorted(src_spines | dst_spines)

    def generate_anomaly_report(
        self, baseline_start: str, baseline_end: str, current_start: str, current_end: str
    ) -> dict:
        latency_anomalies = self.detect_latency_anomalies(baseline_start, baseline_end, current_start, current_end)
        loss_anomalies = self.detect_packet_loss(baseline_start, baseline_end, current_start, current_end)
        df_anomalies = self.detect_df_fragmentation_anomalies(baseline_start, baseline_end, current_start, current_end)
        jitter_anomalies = self.detect_jitter_anomalies(baseline_start, baseline_end, current_start, current_end)
        all_anomalies = latency_anomalies + loss_anomalies + df_anomalies + jitter_anomalies

        # Separate path_unreachable from regular packet_loss for summary counts
        path_unreachable = [a for a in loss_anomalies if a.type == "path_unreachable"]
        regular_loss = [a for a in loss_anomalies if a.type == "packet_loss"]

        by_src_leaf: dict[str, dict[str, int]] = {}
        by_dst_leaf: dict[str, dict[str, int]] = {}
        by_spine: dict[str, dict[str, int]] = {}
        for anomaly in all_anomalies:
            src_leaf = self._resolve_leaf(anomaly.src_leaf, anomaly.src_name)
            dst_leaf = self._resolve_leaf(anomaly.dst_leaf, anomaly.dst_name)
            by_src_leaf.setdefault(
                src_leaf, {"drop_count": 0, "latency_spikes": 0, "jitter_spikes": 0, "path_unreachable": 0}
            )
            by_dst_leaf.setdefault(
                dst_leaf, {"drop_count": 0, "latency_spikes": 0, "jitter_spikes": 0, "path_unreachable": 0}
            )
            if anomaly.type in ("packet_loss", "path_unreachable"):
                key = "path_unreachable" if anomaly.type == "path_unreachable" else "drop_count"
                by_src_leaf[src_leaf][key] += 1
                by_dst_leaf[dst_leaf][key] += 1
            elif anomaly.type == "jitter_spike":
                by_src_leaf[src_leaf]["jitter_spikes"] += 1
                by_dst_leaf[dst_leaf]["jitter_spikes"] += 1
            else:
                by_src_leaf[src_leaf]["latency_spikes"] += 1
                by_dst_leaf[dst_leaf]["latency_spikes"] += 1

            # Spine aggregation for cross-rack anomalies
            if src_leaf != dst_leaf and src_leaf != "unknown" and dst_leaf != "unknown":
                inferred_spines = self._infer_spines_for_cross_rack(src_leaf, dst_leaf)
                for spine in inferred_spines:
                    bucket = by_spine.setdefault(
                        spine, {"drop_count": 0, "latency_spikes": 0, "jitter_spikes": 0, "path_unreachable": 0}
                    )
                    if anomaly.type in ("packet_loss", "path_unreachable"):
                        key = "path_unreachable" if anomaly.type == "path_unreachable" else "drop_count"
                        bucket[key] += 1
                    elif anomaly.type == "jitter_spike":
                        bucket["jitter_spikes"] += 1
                    else:
                        bucket["latency_spikes"] += 1

        now_utc = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S") + "Z"
        return {
            "timestamp": now_utc,
            "windows": {
                "baseline": {"start": baseline_start, "end": baseline_end},
                "current": {"start": current_start, "end": current_end},
            },
            "query_status": {"ok": self.last_query_error is None, "error": self.last_query_error},
            "summary": {
                "total_anomalies": len(all_anomalies),
                "latency_spikes": len(latency_anomalies),
                "packet_loss_events": len(regular_loss),
                "path_unreachable_events": len(path_unreachable),
                "mtu_or_fragmentation_events": len(df_anomalies),
                "jitter_spikes": len(jitter_anomalies),
            },
            "anomalies": [self._anomaly_to_dict(a) for a in all_anomalies],
            "aggregated_anomalies": {
                "by_src_leaf": by_src_leaf,
                "by_dst_leaf": by_dst_leaf,
                "by_spine": by_spine,
            },
        }


def main() -> int:
    from netopsbench.config import config

    detector = AnomalyDetector(config.influxdb_url, config.influxdb_token, config.influxdb_org, config.influxdb_bucket)
    now = datetime.now(UTC)

    def _utc_iso(dt: datetime) -> str:
        s = dt.replace(microsecond=0).isoformat()
        return s[:-6] + "Z" if s.endswith("+00:00") else (s if s.endswith("Z") else s + "Z")

    baseline_end = _utc_iso(now)
    baseline_start = _utc_iso(now - timedelta(minutes=10))
    current_start = _utc_iso(now - timedelta(minutes=5))
    current_end = baseline_end
    report = detector.generate_anomaly_report(
        baseline_start=baseline_start,
        baseline_end=current_start,
        current_start=current_start,
        current_end=current_end,
    )
    print(json.dumps(report, indent=2))
    return 0
