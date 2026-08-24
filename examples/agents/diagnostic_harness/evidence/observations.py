"""Adapt public episode observations into structured harness Evidence."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..models import Evidence, EvidenceOrigin
from ..topology.semantics import with_attachment_aliases
from .time import parse_timestamp


def _reliability(item: Mapping[str, Any]) -> float:
    persistence = str(item.get("persistence") or "")
    sample_count = int(item.get("sample_count") or 0)
    base = 0.9 if persistence == "persistent" else 0.75 if persistence in {"steady_only", "full_window"} else 0.6
    return min(base, max(0.3, sample_count / 30.0))


def evidence_from_public_observations(context: Any, *, max_anomalies: int = 100) -> list[Evidence]:
    """Use only operator-visible observations; never scenario or injection data."""
    symptoms = getattr(context, "symptoms", {}) or {}
    observations = symptoms.get("observations", {}) if isinstance(symptoms, Mapping) else {}
    pingmesh = observations.get("pingmesh_metrics", {}) if isinstance(observations, Mapping) else {}
    if not isinstance(pingmesh, Mapping):
        return []
    evidence: list[Evidence] = []
    query_status = pingmesh.get("query_status")
    if isinstance(query_status, Mapping) and query_status.get("ok") is False:
        evidence.append(
            Evidence(
                evidence_id="episode-pingmesh-query-error",
                entity_type="observation_window",
                entity_id="pingmesh",
                category="tool_error",
                value={"error": query_status.get("error")},
                source="pingmesh_episode",
                timestamp=None,
                reliability=0.0,
                origin=EvidenceOrigin.PUBLIC_OBSERVATION,
                supports_submission=False,
            )
        )
    anomalies = pingmesh.get("anomalies")
    if not isinstance(anomalies, list):
        return evidence
    # A Pingmesh anomaly table is one query over one episode window. Row-level
    # timestamps describe samples within that query; they are not independent
    # observation sources and must not be counted repeatedly by the scorer.
    window_start = observations.get("start_time") or pingmesh.get("start_time")
    window_end = observations.get("end_time") or pingmesh.get("end_time")
    query_id = query_status.get("query_id") if isinstance(query_status, Mapping) else None
    independence_key = f"pingmesh-window:{query_id or window_start or 'episode'}:{window_end or 'episode'}"
    for index, item in enumerate((item for item in anomalies if isinstance(item, Mapping)), start=1):
        if index > max_anomalies:
            break
        anomaly_type = str(item.get("type") or "").lower()
        raw_value = float(item.get("value") or 0.0)
        if "latency" in anomaly_type or "rtt" in anomaly_type:
            category = "latency_p95"
            value: Any = raw_value
        elif "mtu" in anomaly_type or "fragment" in anomaly_type:
            category = "packet_size_threshold"
            value = {
                "size_dependent_failure": True,
                "observed_loss_rate": raw_value / 100.0,
                "threshold_payload_size": None,
            }
        elif "loss" in anomaly_type or "unreachable" in anomaly_type:
            category = "packet_loss_rate"
            value = raw_value / 100.0
        else:
            continue
        source = str(item.get("src_name") or item.get("src_ip") or "unknown")
        destination = str(item.get("dst_name") or item.get("dst_ip") or "unknown")
        evidence.append(
            Evidence(
                evidence_id=f"episode-pingmesh-{index}",
                entity_type="path",
                entity_id=f"{source}--{destination}",
                category=category,
                value=value,
                source="pingmesh_episode",
                timestamp=parse_timestamp(item.get("timestamp")),
                reliability=_reliability(item),
                origin=EvidenceOrigin.PUBLIC_OBSERVATION,
                # Pingmesh labels this row as an MTU/fragmentation *suspect*;
                # it has not performed the DF packet-size sweep needed to
                # prove a stable threshold.  Preserve it for branch planning
                # and path selection, but never let it satisfy the MTU gate.
                supports_submission=not (
                    category == "packet_size_threshold" and value.get("threshold_payload_size") is None
                ),
                # These rows come from one episode-window query. Keep them
                # separate for topology/path contrast, but treat the batch as
                # one independent probabilistic observation.
                independence_key=independence_key,
                metadata=with_attachment_aliases(
                    {
                        "anomaly_type": item.get("type"),
                        "src_name": item.get("src_name"),
                        "src_ip": item.get("src_ip"),
                        "dst_name": item.get("dst_name"),
                        "dst_ip": item.get("dst_ip"),
                        "src_leaf": item.get("src_leaf"),
                        "dst_leaf": item.get("dst_leaf"),
                        "src_attachment": item.get("src_attachment"),
                        "dst_attachment": item.get("dst_attachment"),
                        "baseline": item.get("baseline"),
                        "threshold": item.get("threshold"),
                        "severity": item.get("severity"),
                        "persistence": item.get("persistence"),
                        "samples_sent": item.get("samples_sent"),
                        "samples_lost": item.get("samples_lost"),
                        "sample_count": item.get("sample_count"),
                        "planning_only": (
                            category == "packet_size_threshold" and value.get("threshold_payload_size") is None
                        ),
                    }
                ),
            )
        )
    return evidence


__all__ = ["evidence_from_public_observations"]
