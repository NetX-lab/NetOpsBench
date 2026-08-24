"""Don't-fragment ICMP payload-size sweep over the real ping toolkit."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from ..config import MTUProbeConfig
from ..models import Evidence, EvidenceOrigin, ProbeOutcome, ProbePair, RankedInterfaceCandidate
from .base import ProbeBudget, invoke_link_ping, invoke_ping, tool_error_evidence


@dataclass(frozen=True)
class PacketSizeObservation:
    payload_size: int
    ip_packet_size: int
    sent: int
    received: int
    loss_rate: float
    success: bool
    return_code: int


@dataclass(frozen=True)
class MTUProbeResult:
    source: str
    destination: str
    observations: tuple[PacketSizeObservation, ...]
    largest_successful_size: int | None
    smallest_failed_size: int | None
    size_dependent_failure: bool


class MTUPacketSizeSweepProbe:
    def __init__(self, config: MTUProbeConfig | None = None):
        self.config = config or MTUProbeConfig()

    async def run(
        self,
        context,
        *,
        pairs: list[ProbePair],
        budget: ProbeBudget,
        probe_id: str = "mtu-packet-size-sweep",
    ) -> ProbeOutcome:
        starting = budget.snapshot()
        evidence: list[Evidence] = []
        results: list[MTUProbeResult] = []

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

            size_observations: list[PacketSizeObservation] = []
            for size in sorted(set(self.config.payload_sizes)):
                observation, error = await invoke_ping(
                    context,
                    source=pair.source,
                    destination=pair.destination,
                    count=self.config.packets_per_size,
                    payload_size=size,
                    dont_fragment=True,
                    budget=budget,
                    timeout_seconds=self.config.timeout_seconds,
                )
                if observation is None:
                    evidence.append(
                        tool_error_evidence(
                            evidence_id=f"{probe_id}-error-{pair_index}-{size}",
                            probe_id=probe_id,
                            entity_id=f"{pair.source}--{pair.destination}",
                            error=error or "unknown ping failure",
                            source="ping_test",
                        )
                    )
                    if error and "budget exhausted" in error:
                        break
                    continue

                success = (
                    observation.return_code == 0 and observation.sent > 0 and observation.received == observation.sent
                )
                size_observation = PacketSizeObservation(
                    payload_size=size,
                    ip_packet_size=size + 28,
                    sent=observation.sent,
                    received=observation.received,
                    loss_rate=observation.loss_rate,
                    success=success,
                    return_code=observation.return_code,
                )
                size_observations.append(size_observation)
                evidence.append(
                    Evidence(
                        evidence_id=f"{probe_id}-size-{pair_index}-{size}",
                        entity_type="path",
                        entity_id=f"{pair.source}--{pair.destination}",
                        category="packet_loss_rate",
                        value=observation.loss_rate,
                        source="ping_test_df_size_sweep",
                        timestamp=datetime.now(UTC),
                        reliability=1.0 if observation.sent >= self.config.packets_per_size else 0.5,
                        probe_id=probe_id,
                        origin=EvidenceOrigin.ACTIVE_PROBE,
                        independence_key=f"probe:{probe_id}:pair:{pair_index}",
                        metadata={
                            "payload_size": size,
                            "ip_packet_size": size + 28,
                            "size_semantics": "icmp_payload_bytes",
                            "dont_fragment": True,
                            "sent": observation.sent,
                            "received": observation.received,
                            "return_code": observation.return_code,
                            "success": success,
                        },
                    )
                )

            if not size_observations:
                continue
            successful = [item.payload_size for item in size_observations if item.success]
            failed = [item.payload_size for item in size_observations if not item.success]
            largest_success = max(successful, default=None)
            smallest_failure = min(failed, default=None)
            size_dependent = bool(
                largest_success is not None
                and smallest_failure is not None
                and largest_success < smallest_failure
                and all(item.success for item in size_observations if item.payload_size <= largest_success)
                and all(not item.success for item in size_observations if item.payload_size >= smallest_failure)
            )
            result = MTUProbeResult(
                source=pair.source,
                destination=pair.destination,
                observations=tuple(size_observations),
                largest_successful_size=largest_success,
                smallest_failed_size=smallest_failure,
                size_dependent_failure=size_dependent,
            )
            results.append(result)
            evidence.append(
                Evidence(
                    evidence_id=f"{probe_id}-threshold-{pair_index}",
                    entity_type="path",
                    entity_id=f"{pair.source}--{pair.destination}",
                    category="packet_size_threshold",
                    value={
                        "largest_successful_payload_size": largest_success,
                        "smallest_failed_payload_size": smallest_failure,
                        "size_dependent_failure": size_dependent,
                    },
                    source="ping_test_df_size_sweep",
                    timestamp=datetime.now(UTC),
                    reliability=min(1.0, len(size_observations) / len(set(self.config.payload_sizes))),
                    probe_id=probe_id,
                    origin=EvidenceOrigin.ACTIVE_PROBE,
                    independence_key=f"probe:{probe_id}:pair:{pair_index}",
                    metadata={
                        "source": pair.source,
                        "destination": pair.destination,
                        "size_semantics": "icmp_payload_bytes",
                        "header_bytes": 28,
                        "tested_payload_sizes": [item.payload_size for item in size_observations],
                        "source_leaf": pair.source_leaf,
                        "destination_leaf": pair.destination_leaf,
                        "source_attachment": pair.source_attachment,
                        "destination_attachment": pair.destination_attachment,
                    },
                )
            )

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
            metadata={"pairs_completed": len(results), "size_semantics": "icmp_payload_bytes"},
        )


class MTULinkSweepProbe:
    """Confirm a peer-MTU difference on its exact physical link.

    Inventory comparison supplies configuration evidence. This probe supplies
    an independent dataplane observation by sending from the lower-MTU
    endpoint with DF set, avoiding any dependence on end-to-end ECMP hashing.
    """

    def __init__(self, config: MTUProbeConfig | None = None):
        self.config = config or MTUProbeConfig()

    async def run(
        self,
        context,
        *,
        candidates: Sequence[RankedInterfaceCandidate],
        evidence: Sequence[Evidence],
        budget: ProbeBudget,
        max_candidates: int = 1,
        probe_id: str = "mtu-physical-link-sweep",
    ) -> ProbeOutcome:
        starting = budget.snapshot()
        collected: list[Evidence] = []
        results: list[MTUProbeResult] = []
        differences = self._differences_by_link(evidence)
        selected = [item for item in candidates if item.link_id in differences][: max(0, int(max_candidates))]

        for index, candidate in enumerate(selected, start=1):
            difference = differences[candidate.link_id]
            source_device, source_interface = difference.entity_id.split(":", 1)
            if source_device == candidate.primary_device:
                target_device, target_interface = candidate.peer_device, candidate.peer_interface
            elif source_device == candidate.peer_device:
                target_device, target_interface = candidate.primary_device, candidate.primary_interface
            else:
                continue
            lower_mtu = int(difference.value["local_mtu"])
            small_payload = min(64, max(0, lower_mtu - 28))
            failing_payload = min(65_507, max(small_payload + 1, lower_mtu - 27))
            try:
                budget.reserve_probe()
            except RuntimeError as exc:
                collected.append(
                    tool_error_evidence(
                        evidence_id=f"{probe_id}-budget-{index}",
                        probe_id=probe_id,
                        entity_id=candidate.link_id,
                        error=str(exc),
                        source="harness_budget",
                    )
                )
                break

            observations: list[PacketSizeObservation] = []
            for payload_size in (small_payload, failing_payload):
                observation, error = await invoke_link_ping(
                    context,
                    source=source_device,
                    target_device=target_device,
                    source_interface=source_interface,
                    target_interface=target_interface,
                    count=self.config.packets_per_size,
                    payload_size=payload_size,
                    dont_fragment=True,
                    budget=budget,
                    timeout_seconds=self.config.timeout_seconds,
                )
                if observation is None:
                    collected.append(
                        tool_error_evidence(
                            evidence_id=f"{probe_id}-error-{index}-{payload_size}",
                            probe_id=probe_id,
                            entity_id=candidate.link_id,
                            error=error or "unknown link ping failure",
                            source="ping_link_test",
                        )
                    )
                    continue
                success = (
                    observation.return_code == 0 and observation.sent > 0 and observation.received == observation.sent
                )
                observations.append(
                    PacketSizeObservation(
                        payload_size=payload_size,
                        ip_packet_size=payload_size + 28,
                        sent=observation.sent,
                        received=observation.received,
                        loss_rate=observation.loss_rate,
                        success=success,
                        return_code=observation.return_code,
                    )
                )

            if len(observations) != 2:
                continue
            successful = [item.payload_size for item in observations if item.success]
            failed = [item.payload_size for item in observations if not item.success]
            largest_success = max(successful, default=None)
            smallest_failure = min(failed, default=None)
            size_dependent = largest_success is not None and smallest_failure is not None
            results.append(
                MTUProbeResult(
                    source=source_device,
                    destination=target_device,
                    observations=tuple(observations),
                    largest_successful_size=largest_success,
                    smallest_failed_size=smallest_failure,
                    size_dependent_failure=size_dependent,
                )
            )
            collected.append(
                Evidence(
                    evidence_id=f"{probe_id}-threshold-{index}",
                    entity_type="interface",
                    entity_id=f"{source_device}:{source_interface}",
                    category="packet_size_threshold",
                    value={
                        "largest_successful_payload_size": largest_success,
                        "smallest_failed_payload_size": smallest_failure,
                        "size_dependent_failure": size_dependent,
                    },
                    source="ping_link_test_df_size_sweep",
                    timestamp=datetime.now(UTC),
                    reliability=1.0,
                    probe_id=probe_id,
                    origin=EvidenceOrigin.ACTIVE_PROBE,
                    independence_key=f"probe:{probe_id}:{candidate.link_id}",
                    observed_path=(candidate.link_id,),
                    possible_paths=((candidate.link_id,),),
                    covered_links=(candidate.link_id,),
                    path_observation_confidence=1.0,
                    metadata={
                        "source": source_device,
                        "destination": target_device,
                        "source_interface": source_interface,
                        "target_interface": target_interface,
                        "size_semantics": "icmp_payload_bytes",
                        "header_bytes": 28,
                        "dont_fragment": True,
                        "configured_lower_mtu": lower_mtu,
                        "fault_endpoint_device": source_device,
                        "fault_endpoint_interface": source_interface,
                    },
                )
            )

        errors = [item for item in collected if item.category == "tool_error"]
        status = "completed" if results and not errors else "partial" if results else "failed"
        return ProbeOutcome(
            probe_id=probe_id,
            status=status,
            evidence=tuple(collected),
            observations=tuple(results),
            tool_calls=budget.tool_calls - starting["tool_calls"],
            probe_packets=budget.probe_packets - starting["probe_packets"],
            error=errors[0].value["error"] if not results and errors else None,
            metadata={"links_completed": len(results), "size_semantics": "icmp_payload_bytes"},
        )

    @staticmethod
    def _differences_by_link(evidence: Sequence[Evidence]) -> dict[str, Evidence]:
        differences: dict[str, Evidence] = {}
        for item in evidence:
            if (
                item.category != "configuration_difference"
                or not isinstance(item.value, Mapping)
                or item.value.get("field") != "mtu"
                or not item.value.get("different")
                or not item.metadata.get("link_id")
                or ":" not in item.entity_id
            ):
                continue
            try:
                int(item.value["local_mtu"])
            except (KeyError, TypeError, ValueError):
                continue
            differences[str(item.metadata["link_id"])] = item
        return differences


__all__ = ["MTULinkSweepProbe", "MTUPacketSizeSweepProbe", "MTUProbeResult", "PacketSizeObservation"]
