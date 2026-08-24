"""Repeated packet-loss statistics over the real ping toolkit."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from ..config import PacketLossProbeConfig
from ..models import Evidence, EvidenceOrigin, PingObservation, ProbeOutcome, ProbePair
from .base import ProbeBudget, invoke_link_ping, invoke_ping, tool_error_evidence


@dataclass(frozen=True)
class PacketLossResult:
    source: str
    destination: str
    sent: int
    received: int
    loss_rate: float
    rounds: int
    protocol: str = "icmp"
    packet_size: int | None = None
    selection: str | None = None


class RepeatedPacketLossProbe:
    def __init__(self, config: PacketLossProbeConfig | None = None):
        self.config = config or PacketLossProbeConfig()

    async def run(
        self,
        context,
        *,
        pairs: list[ProbePair],
        budget: ProbeBudget,
        probe_id: str = "repeated-packet-loss",
    ) -> ProbeOutcome:
        starting = budget.snapshot()
        evidence: list[Evidence] = []
        results: list[PacketLossResult] = []
        raw_observations: list[PingObservation] = []

        for pair_index, pair in enumerate(pairs[: self.config.max_pairs], start=1):
            try:
                budget.reserve_probe()
            except RuntimeError as exc:
                evidence.append(
                    tool_error_evidence(
                        evidence_id=f"{probe_id}-budget-{pair_index}",
                        probe_id=probe_id,
                        entity_id=f"{pair.source}--{pair.destination}",
                        error=str(exc),
                        source="harness_budget",
                    )
                )
                break

            pair_observations: list[PingObservation] = []
            successful_rounds = 0
            stop_for_budget = False
            packets_per_round = _positive_int(
                pair.metadata.get("packets_per_round"),
                default=self.config.packets_per_pair,
            )
            requested_rounds = _positive_int(
                pair.metadata.get("repeat_rounds"),
                default=self.config.repeat_rounds,
            )
            for round_index in range(1, requested_rounds + 1):
                remaining = packets_per_round
                round_success = True
                while remaining > 0:
                    count = min(20, remaining)
                    if pair.metadata.get("link_probe"):
                        observation, error = await invoke_link_ping(
                            context,
                            source=pair.source,
                            target_device=str(pair.metadata["target_device"]),
                            source_interface=str(pair.metadata["source_interface"]),
                            target_interface=str(pair.metadata["target_interface"]),
                            count=count,
                            budget=budget,
                            timeout_seconds=self.config.timeout_seconds,
                        )
                        probe_source = "ping_link_test"
                    else:
                        observation, error = await invoke_ping(
                            context,
                            source=pair.source,
                            destination=pair.destination,
                            count=count,
                            budget=budget,
                            timeout_seconds=self.config.timeout_seconds,
                            source_interface=pair.metadata.get("source_interface"),
                        )
                        probe_source = "ping_test"
                    if observation is None:
                        evidence.append(
                            tool_error_evidence(
                                evidence_id=f"{probe_id}-error-{pair_index}-{round_index}-{remaining}",
                                probe_id=probe_id,
                                entity_id=f"{pair.source}--{pair.destination}",
                                error=error or "unknown ping failure",
                                source=probe_source,
                            )
                        )
                        round_success = False
                        if error and "budget exhausted" in error:
                            stop_for_budget = True
                        break
                    pair_observations.append(observation)
                    raw_observations.append(observation)
                    remaining -= observation.requested_count
                if round_success:
                    successful_rounds += 1
                if stop_for_budget:
                    break

            sent = sum(item.sent for item in pair_observations)
            received = sum(item.received for item in pair_observations)
            if sent:
                loss_rate = max(0.0, min(1.0, (sent - received) / sent))
                result = PacketLossResult(
                    source=pair.source,
                    destination=pair.destination,
                    sent=sent,
                    received=received,
                    loss_rate=loss_rate,
                    rounds=successful_rounds,
                    selection=str(pair.metadata.get("selection") or "") or None,
                )
                results.append(result)
                evidence.append(
                    Evidence(
                        evidence_id=f"{probe_id}-pair-{pair_index}",
                        entity_type="path",
                        entity_id=f"{pair.source}--{pair.destination}",
                        category="packet_loss_rate",
                        value=loss_rate,
                        source="ping_test_repeated",
                        timestamp=datetime.now(UTC),
                        reliability=min(1.0, sent / (packets_per_round * requested_rounds)),
                        probe_id=probe_id,
                        origin=EvidenceOrigin.ACTIVE_PROBE,
                        independence_key=f"probe:{probe_id}:pair:{pair_index}",
                        metadata={
                            "sent": sent,
                            "received": received,
                            "loss_rate": loss_rate,
                            "rounds": successful_rounds,
                            "requested_rounds": requested_rounds,
                            "packets_per_round": packets_per_round,
                            "source": pair.source,
                            "destination": pair.destination,
                            "protocol": "icmp",
                            "packet_size": None,
                            "warning": loss_rate >= self.config.warning_threshold,
                            "strong": loss_rate >= self.config.strong_threshold,
                            "source_leaf": pair.source_leaf,
                            "destination_leaf": pair.destination_leaf,
                            "source_attachment": pair.source_attachment,
                            "destination_attachment": pair.destination_attachment,
                            "selection": pair.metadata.get("selection"),
                            "suspect_attachment": pair.metadata.get("suspect_attachment"),
                            "suspect_leaf": pair.metadata.get("suspect_leaf"),
                            "link_probe": bool(pair.metadata.get("link_probe")),
                            "source_interface": pair.metadata.get("source_interface"),
                            "target_interface": pair.metadata.get("target_interface"),
                        },
                    )
                )
            if stop_for_budget:
                break

        delta_calls = budget.tool_calls - starting["tool_calls"]
        delta_packets = budget.probe_packets - starting["probe_packets"]
        errors = [item for item in evidence if item.category == "tool_error"]
        status = "completed" if results and not errors else "partial" if results else "failed"
        return ProbeOutcome(
            probe_id=probe_id,
            status=status,
            evidence=tuple(evidence),
            observations=tuple(results),
            tool_calls=delta_calls,
            probe_packets=delta_packets,
            error=errors[0].value["error"] if not results and errors else None,
            metadata={
                "raw_ping_observations": len(raw_observations),
                "pairs_completed": len(results),
                "anomaly_pairs": sum(
                    item.metadata.get("selection") == "anomaly"
                    for item in evidence
                    if item.category == "packet_loss_rate"
                ),
                "control_pairs": sum(
                    item.metadata.get("selection") == "healthy_control"
                    for item in evidence
                    if item.category == "packet_loss_rate"
                ),
                "link_isolation_pairs": sum(
                    item.metadata.get("selection") == "link_isolation"
                    for item in evidence
                    if item.category == "packet_loss_rate"
                ),
            },
        )


def _positive_int(value, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


__all__ = ["PacketLossResult", "RepeatedPacketLossProbe"]
