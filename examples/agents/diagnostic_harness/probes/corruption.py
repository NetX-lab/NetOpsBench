"""Payload-integrity capability contract with real telemetry fallback."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from netopsbench.platform.toolkit.mcp.registry import load_tool_specs

from ..config import CorruptionProbeConfig
from ..models import (
    Evidence,
    EvidenceDirection,
    EvidenceOrigin,
    ProbeOutcome,
    ProbePair,
    RankedInterfaceCandidate,
)
from .base import ProbeBudget, invoke_tool, tool_error_evidence

PAYLOAD_INTEGRITY_TOOL = "payload_integrity_test"
LINK_PAYLOAD_INTEGRITY_TOOL = "payload_integrity_link_test"


@dataclass(frozen=True)
class PayloadIntegrityResult:
    source: str
    destination: str
    supported: bool
    received: bool | None = None
    sequence: int | None = None
    checksum_valid: bool | None = None
    receive_timestamp: str | None = None
    reason: str | None = None


class PayloadIntegrityProbe:
    """Use a registered real receiver, or explicitly report unsupported."""

    def __init__(self, config: CorruptionProbeConfig | None = None):
        self.config = config or CorruptionProbeConfig()

    async def run(
        self,
        context,
        *,
        pair: ProbePair | None = None,
        pairs: list[ProbePair] | None = None,
        budget: ProbeBudget,
        target_device: str | None = None,
        target_interface: str | None = None,
        probe_id: str = "payload-integrity",
    ) -> ProbeOutcome:
        selected_pairs = list(pairs or ([pair] if pair is not None else []))
        if len(selected_pairs) > 1:
            outcomes: list[ProbeOutcome] = []
            for index, selected in enumerate(selected_pairs, start=1):
                if budget.remaining_tool_calls <= 0 or budget.remaining_active_probes <= 0:
                    break
                outcomes.append(
                    await self.run(
                        context,
                        pair=selected,
                        budget=budget,
                        target_device=target_device,
                        target_interface=target_interface,
                        probe_id=f"{probe_id}-{index}",
                    )
                )
            evidence = tuple(item for outcome in outcomes for item in outcome.evidence)
            observations = tuple(item for outcome in outcomes for item in outcome.observations)
            return ProbeOutcome(
                probe_id=probe_id,
                status=(
                    "completed"
                    if outcomes and all(outcome.status == "completed" for outcome in outcomes)
                    else "partial"
                    if outcomes
                    else "failed"
                ),
                evidence=evidence,
                observations=observations,
                tool_calls=sum(outcome.tool_calls for outcome in outcomes),
                probe_packets=sum(outcome.probe_packets for outcome in outcomes),
                metadata={"pairs_requested": len(selected_pairs), "pairs_completed": len(outcomes)},
            )
        if selected_pairs:
            pair = selected_pairs[0]
        starting = budget.snapshot()
        evidence: list[Evidence] = []
        observations: list[PayloadIntegrityResult] = []
        entity_id = (
            f"{pair.source}--{pair.destination}"
            if pair is not None
            else f"{target_device or 'unknown'}:{target_interface or 'unknown'}"
        )
        available = {spec.name for spec in load_tool_specs()}
        if PAYLOAD_INTEGRITY_TOOL not in available:
            result = PayloadIntegrityResult(
                source=pair.source if pair else "",
                destination=pair.destination if pair else "",
                supported=False,
                reason="No registered payload-integrity receiver/tool is available.",
            )
            observations.append(result)
            evidence.append(
                Evidence(
                    evidence_id=f"{probe_id}-unsupported",
                    entity_type="path",
                    entity_id=entity_id,
                    category="missing_observation",
                    value={"capability": PAYLOAD_INTEGRITY_TOOL, "supported": False},
                    source="tool_registry",
                    timestamp=datetime.now(UTC),
                    reliability=0.0,
                    probe_id=probe_id,
                    origin=EvidenceOrigin.UNKNOWN,
                    supports_submission=False,
                    metadata={"reason": result.reason},
                )
            )
            if self.config.allow_interface_counter_fallback and target_device and target_interface:
                await self._collect_counter_fallback(
                    context,
                    budget=budget,
                    probe_id=probe_id,
                    target_device=target_device,
                    target_interface=target_interface,
                    evidence=evidence,
                )
            return ProbeOutcome(
                probe_id=probe_id,
                status="unsupported",
                evidence=tuple(evidence),
                observations=tuple(observations),
                tool_calls=budget.tool_calls - starting["tool_calls"],
                probe_packets=budget.probe_packets - starting["probe_packets"],
                error=result.reason,
                metadata={"payload_integrity_supported": False, "counter_fallback": len(evidence) > 1},
            )

        if pair is None:
            error = "payload-integrity capability exists but no source/destination pair was selected"
            evidence.append(
                tool_error_evidence(
                    evidence_id=f"{probe_id}-missing-pair",
                    probe_id=probe_id,
                    entity_id=entity_id,
                    error=error,
                    source=PAYLOAD_INTEGRITY_TOOL,
                )
            )
            return ProbeOutcome(probe_id=probe_id, status="failed", evidence=tuple(evidence), error=error)

        gateway_method = getattr(getattr(context, "tools", None), PAYLOAD_INTEGRITY_TOOL, None)
        if not callable(gateway_method):
            reason = "The payload-integrity tool is registered but unavailable on this diagnostic tool gateway."
            evidence.append(
                Evidence(
                    evidence_id=f"{probe_id}-gateway-unsupported",
                    entity_type="path",
                    entity_id=entity_id,
                    category="missing_observation",
                    value={"capability": PAYLOAD_INTEGRITY_TOOL, "supported": False},
                    source="tool_gateway",
                    timestamp=datetime.now(UTC),
                    reliability=0.0,
                    probe_id=probe_id,
                    origin=EvidenceOrigin.UNKNOWN,
                    supports_submission=False,
                    metadata={"reason": reason},
                )
            )
            return ProbeOutcome(
                probe_id=probe_id,
                status="unsupported",
                evidence=tuple(evidence),
                observations=(
                    PayloadIntegrityResult(
                        source=pair.source,
                        destination=pair.destination,
                        supported=False,
                        reason=reason,
                    ),
                ),
                error=reason,
                metadata={"payload_integrity_supported": False, "counter_fallback": False},
            )

        try:
            budget.reserve_probe()
        except RuntimeError as exc:
            error = str(exc)
            evidence.append(
                tool_error_evidence(
                    evidence_id=f"{probe_id}-budget",
                    probe_id=probe_id,
                    entity_id=entity_id,
                    error=error,
                    source="harness_budget",
                )
            )
            return ProbeOutcome(probe_id=probe_id, status="failed", evidence=tuple(evidence), error=error)

        invocation = await invoke_tool(
            context,
            PAYLOAD_INTEGRITY_TOOL,
            {
                "src": pair.source,
                "dst_ip": pair.destination,
                "count": self.config.samples_per_pair,
            },
            budget=budget,
            timeout_seconds=self.config.timeout_seconds,
            packets=self.config.samples_per_pair,
        )
        if not invocation.success or invocation.data is None:
            error = invocation.error or "payload-integrity tool failed"
            evidence.append(
                tool_error_evidence(
                    evidence_id=f"{probe_id}-error",
                    probe_id=probe_id,
                    entity_id=entity_id,
                    error=error,
                    source=PAYLOAD_INTEGRITY_TOOL,
                )
            )
            return ProbeOutcome(
                probe_id=probe_id,
                status="failed",
                evidence=tuple(evidence),
                tool_calls=budget.tool_calls - starting["tool_calls"],
                probe_packets=budget.probe_packets - starting["probe_packets"],
                error=error,
            )

        if invocation.data.get("observation_complete") is False:
            evidence.append(
                Evidence(
                    evidence_id=f"{probe_id}-missing-capture",
                    entity_type="path",
                    entity_id=entity_id,
                    category="missing_observation",
                    value={
                        "reason": "No checksum-verifiable active packet was captured in the bounded window.",
                        "packets_observed": invocation.data.get("packets_observed", 0),
                    },
                    source=PAYLOAD_INTEGRITY_TOOL,
                    timestamp=datetime.now(UTC),
                    reliability=0.0,
                    probe_id=probe_id,
                    origin=EvidenceOrigin.ACTIVE_PROBE,
                    independence_key=f"probe:{probe_id}:{entity_id}",
                    supports_submission=False,
                )
            )
            return ProbeOutcome(
                probe_id=probe_id,
                status="partial",
                evidence=tuple(evidence),
                observations=(
                    PayloadIntegrityResult(
                        source=pair.source,
                        destination=pair.destination,
                        supported=True,
                        received=bool(invocation.data.get("received")),
                        sequence=None,
                        checksum_valid=None,
                        receive_timestamp=str(invocation.data.get("receive_timestamp") or "") or None,
                        reason="No checksum-verifiable active packet was captured.",
                    ),
                ),
                tool_calls=budget.tool_calls - starting["tool_calls"],
                probe_packets=budget.probe_packets - starting["probe_packets"],
                metadata={"payload_integrity_supported": True, "observation_complete": False},
            )

        return self._parse_integrity_payload(
            pair=pair,
            payload=invocation.data,
            evidence=evidence,
            probe_id=probe_id,
            starting=starting,
            budget=budget,
        )

    async def _collect_counter_fallback(
        self,
        context,
        *,
        budget: ProbeBudget,
        probe_id: str,
        target_device: str,
        target_interface: str,
        evidence: list[Evidence],
    ) -> None:
        try:
            budget.reserve_probe()
        except RuntimeError as exc:
            evidence.append(
                tool_error_evidence(
                    evidence_id=f"{probe_id}-counter-budget",
                    probe_id=probe_id,
                    entity_id=f"{target_device}:{target_interface}",
                    error=str(exc),
                    source="harness_budget",
                )
            )
            return
        invocation = await invoke_tool(
            context,
            "get_interface_metrics",
            {
                "device": target_device,
                "interface": target_interface,
                "time_range_minutes": 5,
                "metric_type": "errors",
                "view": "summary",
                "max_points": 120,
            },
            budget=budget,
            timeout_seconds=self.config.timeout_seconds,
        )
        if not invocation.success or invocation.data is None:
            evidence.append(
                tool_error_evidence(
                    evidence_id=f"{probe_id}-counter-error",
                    probe_id=probe_id,
                    entity_id=f"{target_device}:{target_interface}",
                    error=invocation.error or "interface counter query failed",
                    source="get_interface_metrics",
                )
            )
            return
        summary = invocation.data.get("summary")
        summary = summary if isinstance(summary, Mapping) else {}
        deltas = {
            field: float(values.get("window_delta") or 0.0)
            for field, values in summary.items()
            if isinstance(values, Mapping) and field in {"in_errors", "out_errors"}
        }
        evidence.append(
            Evidence(
                evidence_id=f"{probe_id}-counter-fallback",
                entity_type="interface",
                entity_id=f"{target_device}:{target_interface}",
                category="interface_counter_delta",
                value=deltas,
                source="get_interface_metrics",
                timestamp=datetime.now(UTC),
                reliability=0.6 if deltas else 0.2,
                probe_id=probe_id,
                origin=EvidenceOrigin.LIVE_TELEMETRY,
                independence_key=f"tool:get_interface_metrics:{target_device}:{target_interface}",
                metadata={
                    "device": target_device,
                    "interface": target_interface,
                    "metric_type": "errors",
                    "payload_integrity_supported": False,
                    "diagnostic_limit": "error counters cannot uniquely distinguish corruption from loss",
                },
            )
        )

    @staticmethod
    def _parse_integrity_payload(
        *,
        pair: ProbePair,
        payload: Mapping[str, Any],
        evidence: list[Evidence],
        probe_id: str,
        starting: dict[str, int],
        budget: ProbeBudget,
    ) -> ProbeOutcome:
        received = bool(payload.get("received"))
        checksum_valid = payload.get("checksum_valid")
        checksum_valid = bool(checksum_valid) if checksum_valid is not None else None
        result = PayloadIntegrityResult(
            source=pair.source,
            destination=pair.destination,
            supported=True,
            received=received,
            sequence=int(payload["sequence"]) if payload.get("sequence") is not None else None,
            checksum_valid=checksum_valid,
            receive_timestamp=str(payload.get("receive_timestamp") or "") or None,
        )
        sent = int(payload.get("packets_sent") or 0)
        observed = int(payload.get("packets_observed") or 0)
        missing = int(payload.get("missing_packets") or max(0, sent - observed))
        integrity_complete = bool(payload.get("integrity_complete", sent > 0 and observed >= sent))
        common_metadata = {
            "received": received,
            "sequence": result.sequence,
            "checksum_valid": checksum_valid,
            "receive_timestamp": result.receive_timestamp,
            "packets_sent": sent,
            "packets_observed": observed,
            "missing_packets": missing,
            "integrity_complete": integrity_complete,
            "checksum_failures": payload.get("checksum_failures"),
            "method": payload.get("method"),
            "source": pair.source,
            "destination": pair.destination,
            "source_attachment": pair.source_attachment,
            "destination_attachment": pair.destination_attachment,
            "source_leaf": pair.source_leaf,
            "destination_leaf": pair.destination_leaf,
        }
        # A bad checksum is decisive even if other packets were missing. A
        # healthy checksum is only decisive when every sent packet reached the
        # capture window; otherwise it describes only the observed subset.
        if received and checksum_valid is not None and (checksum_valid is False or integrity_complete):
            evidence.append(
                Evidence(
                    evidence_id=f"{probe_id}-result",
                    entity_type="path",
                    entity_id=f"{pair.source}--{pair.destination}",
                    category="payload_integrity_failure",
                    value=checksum_valid is False,
                    source=PAYLOAD_INTEGRITY_TOOL,
                    timestamp=datetime.now(UTC),
                    reliability=1.0,
                    probe_id=probe_id,
                    origin=EvidenceOrigin.ACTIVE_PROBE,
                    independence_key=f"probe:{probe_id}:{pair.source}:{pair.destination}",
                    metadata=common_metadata,
                )
            )
        if sent > 0 and missing > 0:
            evidence.append(
                Evidence(
                    evidence_id=f"{probe_id}-missing-sequences",
                    entity_type="path",
                    entity_id=f"{pair.source}--{pair.destination}",
                    category="packet_loss_rate",
                    value=missing / sent,
                    source=PAYLOAD_INTEGRITY_TOOL,
                    timestamp=datetime.now(UTC),
                    reliability=1.0,
                    probe_id=probe_id,
                    origin=EvidenceOrigin.ACTIVE_PROBE,
                    independence_key=f"probe:{probe_id}:{pair.source}:{pair.destination}",
                    metadata={
                        **common_metadata,
                        "sent": sent,
                        "received": observed,
                        "missing_sequences": missing,
                    },
                )
            )
        if not evidence:
            evidence.append(
                Evidence(
                    evidence_id=f"{probe_id}-missing-result",
                    entity_type="path",
                    entity_id=f"{pair.source}--{pair.destination}",
                    category="missing_observation",
                    value={"reason": "Active integrity sender produced no checksum-verifiable observation."},
                    source=PAYLOAD_INTEGRITY_TOOL,
                    timestamp=datetime.now(UTC),
                    reliability=0.0,
                    probe_id=probe_id,
                    origin=EvidenceOrigin.ACTIVE_PROBE,
                    independence_key=f"probe:{probe_id}:{pair.source}:{pair.destination}",
                    supports_submission=False,
                    metadata=common_metadata,
                )
            )
        return ProbeOutcome(
            probe_id=probe_id,
            status="completed" if any(item.reliability > 0 for item in evidence) else "partial",
            evidence=tuple(evidence),
            observations=(result,),
            tool_calls=budget.tool_calls - starting["tool_calls"],
            probe_packets=budget.probe_packets - starting["probe_packets"],
            metadata={"payload_integrity_supported": True},
        )


class LinkPayloadIntegrityProbe:
    """Collect checksum evidence on bounded topology-ranked physical links."""

    def __init__(self, config: CorruptionProbeConfig | None = None):
        self.config = config or CorruptionProbeConfig()

    async def run(
        self,
        context: Any,
        *,
        candidates: list[RankedInterfaceCandidate],
        budget: ProbeBudget,
        max_candidates: int,
        probe_id: str = "payload-integrity-links",
    ) -> ProbeOutcome:
        starting = budget.snapshot()
        evidence: list[Evidence] = []
        observations: list[Mapping[str, Any]] = []
        available = {spec.name for spec in load_tool_specs()}
        if LINK_PAYLOAD_INTEGRITY_TOOL not in available:
            reason = "No registered physical-link payload-integrity tool is available."
            return ProbeOutcome(
                probe_id=probe_id,
                status="unsupported",
                evidence=(
                    Evidence(
                        evidence_id=f"{probe_id}-unsupported",
                        entity_type="probe",
                        entity_id="physical-links",
                        category="missing_observation",
                        value={"capability": LINK_PAYLOAD_INTEGRITY_TOOL, "supported": False},
                        source="tool_registry",
                        timestamp=datetime.now(UTC),
                        reliability=0.0,
                        probe_id=probe_id,
                        origin=EvidenceOrigin.UNKNOWN,
                        supports_submission=False,
                        metadata={"reason": reason},
                    ),
                ),
                error=reason,
            )

        selected = candidates[: max(0, int(max_candidates))]
        for index, candidate in enumerate(selected, start=1):
            try:
                budget.reserve_probe()
            except RuntimeError as exc:
                evidence.append(
                    tool_error_evidence(
                        evidence_id=f"{probe_id}-budget-{index}",
                        probe_id=probe_id,
                        entity_id=candidate.link_id,
                        error=str(exc),
                        source="harness_budget",
                    )
                )
                break
            count = max(1, min(int(self.config.samples_per_link_direction), 60))
            invocation = await invoke_tool(
                context,
                LINK_PAYLOAD_INTEGRITY_TOOL,
                {
                    "device_a": candidate.primary_device,
                    "interface_a": candidate.primary_interface,
                    "device_b": candidate.peer_device,
                    "interface_b": candidate.peer_interface,
                    "count": count,
                },
                budget=budget,
                timeout_seconds=self.config.timeout_seconds,
                packets=count * 2,
            )
            if not invocation.success or invocation.data is None:
                evidence.append(
                    tool_error_evidence(
                        evidence_id=f"{probe_id}-error-{index}",
                        probe_id=probe_id,
                        entity_id=candidate.link_id,
                        error=invocation.error or "link payload-integrity tool failed",
                        source=LINK_PAYLOAD_INTEGRITY_TOOL,
                    )
                )
                continue
            payload = invocation.data
            raw_directions = payload.get("directions")
            directions = (
                [item for item in raw_directions if isinstance(item, Mapping)]
                if isinstance(raw_directions, list)
                else []
            )
            observations.append(payload)
            failures = sum(int(item.get("checksum_failures") or 0) for item in directions)
            checksum_observed = bool(directions) and all(
                int(item.get("packets_observed") or 0) > 0 and item.get("checksum_valid") is not None
                for item in directions
            )
            failing_directions = [item for item in directions if int(item.get("checksum_failures") or 0) > 0]
            missing_directions = [item for item in directions if int(item.get("missing_packets") or 0) > 0]
            directional = failing_directions or missing_directions
            fault_devices = {str(item.get("source")) for item in directional if item.get("source")}
            fault_interfaces = {
                str(item.get("source_interface")) for item in directional if item.get("source_interface")
            }
            fault_device = next(iter(fault_devices)) if len(fault_devices) == 1 else None
            fault_interface = next(iter(fault_interfaces)) if len(fault_interfaces) == 1 else None
            direction = EvidenceDirection.BIDIRECTIONAL
            if fault_device == candidate.primary_device:
                direction = EvidenceDirection.A_TO_B
            elif fault_device == candidate.peer_device:
                direction = EvidenceDirection.B_TO_A
            elif directional:
                direction = EvidenceDirection.UNKNOWN
            independence_key = f"probe:{probe_id}:{candidate.link_id}:{index}"
            metadata = {
                "selection": "link_integrity",
                "source": candidate.primary_device,
                "destination": candidate.peer_device,
                "packets_sent": sum(int(item.get("packets_sent") or 0) for item in directions),
                "packets_observed": sum(int(item.get("packets_observed") or 0) for item in directions),
                "missing_packets": sum(int(item.get("missing_packets") or 0) for item in directions),
                "checksum_failures": failures,
                "directions": directions,
                "method": payload.get("method"),
                "fault_endpoint_device": fault_device,
                "fault_endpoint_interface": fault_interface,
            }
            # On an exact one-hop physical link, clean delivered payloads plus
            # missing sequences are the discriminative loss pattern. Bad
            # checksums remain decisive corruption evidence.
            if failures > 0 or checksum_observed:
                evidence.append(
                    Evidence(
                        evidence_id=f"{probe_id}-result-{index}",
                        entity_type="path",
                        entity_id=f"{candidate.primary_device}--{candidate.peer_device}",
                        category="payload_integrity_failure",
                        value=failures > 0,
                        source=LINK_PAYLOAD_INTEGRITY_TOOL,
                        timestamp=datetime.now(UTC),
                        reliability=1.0,
                        probe_id=probe_id,
                        origin=EvidenceOrigin.ACTIVE_PROBE,
                        independence_key=independence_key,
                        direction=direction,
                        metadata=metadata,
                        observed_path=(candidate.link_id,),
                        possible_paths=((candidate.link_id,),),
                        covered_links=(candidate.link_id,),
                        path_observation_confidence=1.0,
                    )
                )
            sent = metadata["packets_sent"]
            missing = metadata["missing_packets"]
            has_missing_sequences = sent > 0 and missing > 0
            if has_missing_sequences:
                evidence.append(
                    Evidence(
                        evidence_id=f"{probe_id}-missing-sequences-{index}",
                        entity_type="path",
                        entity_id=f"{candidate.primary_device}--{candidate.peer_device}",
                        category="packet_loss_rate",
                        value=missing / sent,
                        source=LINK_PAYLOAD_INTEGRITY_TOOL,
                        timestamp=datetime.now(UTC),
                        reliability=1.0,
                        probe_id=probe_id,
                        origin=EvidenceOrigin.ACTIVE_PROBE,
                        independence_key=independence_key,
                        direction=direction,
                        supports_submission=False,
                        metadata={
                            **metadata,
                            "sent": sent,
                            "received": sent - missing,
                            "planning_only": True,
                            "reason": "One bounded checksum sample cannot establish persistent packet loss.",
                        },
                        observed_path=(candidate.link_id,),
                        possible_paths=((candidate.link_id,),),
                        covered_links=(candidate.link_id,),
                        path_observation_confidence=1.0,
                    )
                )
            if failures == 0 and not checksum_observed and not has_missing_sequences:
                evidence.append(
                    Evidence(
                        evidence_id=f"{probe_id}-missing-{index}",
                        entity_type="path",
                        entity_id=f"{candidate.primary_device}--{candidate.peer_device}",
                        category="missing_observation",
                        value={"reason": "No checksum-verifiable packet was captured in both directions."},
                        source=LINK_PAYLOAD_INTEGRITY_TOOL,
                        timestamp=datetime.now(UTC),
                        reliability=0.0,
                        probe_id=probe_id,
                        origin=EvidenceOrigin.ACTIVE_PROBE,
                        independence_key=independence_key,
                        supports_submission=False,
                        metadata=metadata,
                        observed_path=(candidate.link_id,),
                        possible_paths=((candidate.link_id,),),
                        covered_links=(candidate.link_id,),
                        path_observation_confidence=1.0,
                    )
                )

        successful = any(item.reliability > 0 for item in evidence)
        return ProbeOutcome(
            probe_id=probe_id,
            status="completed" if successful else "failed",
            evidence=tuple(evidence),
            observations=tuple(observations),
            tool_calls=budget.tool_calls - starting["tool_calls"],
            probe_packets=budget.probe_packets - starting["probe_packets"],
            metadata={"links_requested": len(selected), "links_completed": len(observations)},
        )


__all__ = [
    "LINK_PAYLOAD_INTEGRITY_TOOL",
    "PAYLOAD_INTEGRITY_TOOL",
    "LinkPayloadIntegrityProbe",
    "PayloadIntegrityProbe",
    "PayloadIntegrityResult",
]
