"""Small deterministic action planner for bounded hard-case diagnosis."""

from __future__ import annotations

from dataclasses import dataclass

from ..evidence.validator import can_support_fault
from ..models import Evidence


@dataclass(frozen=True)
class PlannedAction:
    action_id: str
    reason: str
    expected_discrimination: float = 1.0
    expected_tool_cost: int = 1


class DeterministicProbePlanner:
    def plan(
        self,
        *,
        family: str,
        evidence: list[Evidence],
        executed: set[str] | None = None,
    ) -> list[PlannedAction]:
        completed = executed or set()
        actions: list[PlannedAction] = []
        primary = {
            "packet_loss": "repeated_packet_loss",
            "packet_corruption": "payload_integrity",
            "mtu": "mtu_sweep",
            "high_latency": "rtt_matrix",
        }.get(family)
        if primary and primary not in completed and not self._primary_observed(family, evidence):
            actions.append(
                PlannedAction(
                    primary,
                    f"Collect discriminative {family} evidence.",
                    expected_discrimination=1.0,
                )
            )
        if (
            family == "packet_loss"
            and "payload_integrity" not in completed
            and not any(item.category == "payload_integrity_failure" and can_support_fault(item) for item in evidence)
        ):
            actions.append(
                PlannedAction(
                    "payload_integrity",
                    "Packet loss alone cannot exclude payload corruption.",
                    expected_discrimination=0.9,
                )
            )
        size_suspect = any(
            item.category == "packet_size_threshold"
            and isinstance(item.value, dict)
            and item.value.get("size_dependent_failure")
            for item in evidence
        )
        if (
            family == "mtu"
            and size_suspect
            and "peer_consistency" not in completed
            and not any(item.category == "configuration_difference" and can_support_fault(item) for item in evidence)
        ):
            actions.append(
                PlannedAction(
                    "peer_consistency",
                    "Compare MTU on both endpoints of topology-ranked links.",
                    expected_discrimination=1.0,
                    expected_tool_cost=2,
                )
            )
        return sorted(
            actions,
            key=lambda item: (-item.expected_discrimination / max(1, item.expected_tool_cost), item.action_id),
        )

    @staticmethod
    def _primary_observed(family: str, evidence: list[Evidence]) -> bool:
        categories = {
            "packet_loss": {"packet_loss_rate"},
            "packet_corruption": {"payload_integrity_failure"},
            "mtu": {"packet_size_threshold"},
            "high_latency": {"latency_median", "latency_p95"},
        }.get(family, set())
        return any(
            item.category in categories
            and can_support_fault(item)
            # A wrapped agent's real tool result is valuable evidence, but it
            # is not an independent confirmation. Keep one bounded harness
            # action available so the final two-source contract can close.
            and not str(item.source).startswith("base_tool:")
            for item in evidence
        )


__all__ = ["DeterministicProbePlanner", "PlannedAction"]
