"""Hypothesis construction and evidence requirements."""

from __future__ import annotations

from ..models import Hypothesis, RankedInterfaceCandidate

IMPAIRMENT_TYPES = ("packet_loss", "packet_corruption", "mtu_mismatch", "high_latency")

REQUIRED_SIGNALS = {
    "packet_loss": ("random_packet_loss", "payload_integrity_valid"),
    "packet_corruption": ("payload_integrity_failure",),
    "mtu_mismatch": ("size_dependent_failure", "configuration_difference"),
    "high_latency": ("latency_anomaly",),
}


def new_hypotheses(candidate: RankedInterfaceCandidate | None = None) -> dict[str, Hypothesis]:
    return {
        fault_type: Hypothesis(
            hypothesis_id=f"H-{fault_type}",
            fault_type=fault_type,
            device=candidate.primary_device if candidate else None,
            interface=candidate.primary_interface if candidate else None,
            link_id=candidate.link_id if candidate else None,
            required_evidence=list(REQUIRED_SIGNALS[fault_type]),
        )
        for fault_type in IMPAIRMENT_TYPES
    }


__all__ = ["IMPAIRMENT_TYPES", "REQUIRED_SIGNALS", "new_hypotheses"]
