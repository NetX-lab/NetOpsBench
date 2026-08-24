"""Translate structured observations into configured impairment signals."""

from __future__ import annotations

from ..config import HypothesisConfig
from ..models import Evidence


def evidence_signal(evidence: Evidence, config: HypothesisConfig) -> str | None:
    if evidence.category in {"tool_error", "missing_observation"} or evidence.reliability <= 0:
        return None
    if evidence.category == "packet_loss_rate":
        value = float(evidence.value or 0.0)
        if evidence.source == "ping_test_repeated" and 0.0 < value and evidence.metadata.get("warning") is False:
            # A few losses in one bounded sample can be random background
            # noise. Keep them visible in Evidence without promoting them to a
            # fault hypothesis or a healthy contradiction.
            return None
        return "random_packet_loss" if value >= config.loss_warning_threshold else "healthy_packet_delivery"
    if evidence.category == "payload_integrity_failure":
        return "payload_integrity_failure" if bool(evidence.value) else "payload_integrity_valid"
    if evidence.category == "packet_size_threshold" and isinstance(evidence.value, dict):
        return "size_dependent_failure" if evidence.value.get("size_dependent_failure") else "size_sweep_healthy"
    if evidence.category in {"latency_median", "latency_p95"}:
        if evidence.metadata.get("absolute_anomaly") or evidence.metadata.get("relative_anomaly"):
            return "latency_anomaly"
        threshold = evidence.metadata.get("threshold")
        if threshold is not None and float(evidence.value or 0.0) >= float(threshold):
            return "latency_anomaly"
        return "latency_anomaly" if float(evidence.value or 0.0) >= config.latency_threshold_ms else "latency_healthy"
    if evidence.category == "configuration_difference":
        # Semantic config observations belong to the route/ACL closure. They
        # must not accidentally provide impairment support merely because the
        # generic matrix has a configuration row (especially route-policy
        # evidence being counted as MTU/loss support).
        if evidence.metadata.get("semantic_family") in {
            "acl",
            "route_policy",
            "static_route",
            "blackhole_route",
        }:
            return None
        return "configuration_difference"
    return None


__all__ = ["evidence_signal"]
