"""Bounded public-tool closures for healthy, link-state, and temporal routing."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from math import ceil
from typing import Any

from netopsbench.sdk.agents import DiagnosisResult

from ..config import OperationalVerificationConfig
from ..evidence.bgp import bgp_configuration_fault_reason
from ..evidence.temporal import classify_log_signal, is_repeated_flap_sequence, is_temporal_signal, signal_from_value
from ..evidence.time import parse_timestamp
from ..evidence.validator import can_support_fault
from ..models import DiagnosisView, Evidence, EvidenceOrigin, ProbeOutcome
from ..normalization.interface import PhysicalLink, TopologyIndex
from ..probes.base import ProbeBudget, invoke_link_ping, invoke_tool, select_probe_pairs, tool_error_evidence
from ..probes.corruption import PayloadIntegrityProbe
from ..topology.graph import TopologyGraph
from ..topology.path_analysis import scope_path_evidence
from ..topology.semantics import attachment_from_metadata

_PORT_RE = re.compile(r"\b(?:Port|interface)\s+(Ethernet\d+)\b", re.IGNORECASE)


class HealthyVerificationStatus(StrEnum):
    VERIFIED_HEALTHY = "verified_healthy"
    INSUFFICIENT_OBSERVATION = "insufficient_observation"
    FAULT_SUSPECTED = "fault_suspected"


@dataclass(frozen=True)
class OperationalClosureResult:
    result: DiagnosisResult | None
    outcome: ProbeOutcome
    status: str
    next_family: str | None = None


def device_down_from_link_probes(
    base_result: DiagnosisResult,
    *,
    topology: TopologyIndex,
    evidence: Sequence[Evidence],
) -> OperationalClosureResult | None:
    """Close a device-wide outage from independent one-hop observations.

    A failed tool call is deliberately insufficient.  The same network node
    must be unreachable over at least two distinct physical links, no direct
    probe may reach it over another link, and each affected peer must have a
    healthy one-hop control through a different network device.  This
    separates a device outage from one bad link or a generally unhealthy host.
    """
    links = {link.link_id: link for link in topology.links}
    failed: dict[str, Evidence] = {}
    healthy: dict[str, Evidence] = {}
    for item in evidence:
        if (
            item.category != "packet_loss_rate"
            or item.origin is not EvidenceOrigin.ACTIVE_PROBE
            or item.path_observation_confidence < 1.0
            or len(item.covered_links) != 1
        ):
            continue
        link_id = item.covered_links[0]
        if link_id not in links:
            continue
        try:
            loss_rate = float(item.value)
        except (TypeError, ValueError):
            continue
        if loss_rate >= 0.80:
            failed[link_id] = item
        elif loss_rate <= 0.01:
            healthy[link_id] = item
    if len(failed) < 2:
        return None

    failed_by_device: dict[str, set[str]] = {}
    for link_id in failed:
        link = links[link_id]
        for endpoint in (link.endpoint_a, link.endpoint_b):
            if topology.devices.get(endpoint.device) != "client":
                failed_by_device.setdefault(endpoint.device, set()).add(link_id)

    for device, failed_links in sorted(failed_by_device.items(), key=lambda item: (-len(item[1]), item[0])):
        if len(failed_links) < 2:
            continue
        if any(device in {links[link_id].endpoint_a.device, links[link_id].endpoint_b.device} for link_id in healthy):
            continue
        peers: dict[str, str] = {}
        for link_id in failed_links:
            link = links[link_id]
            peers[link_id] = link.endpoint_b.device if link.endpoint_a.device == device else link.endpoint_a.device
        controlled_peers = {
            peer
            for peer in peers.values()
            if any(
                peer in {links[link_id].endpoint_a.device, links[link_id].endpoint_b.device}
                and device not in {links[link_id].endpoint_a.device, links[link_id].endpoint_b.device}
                for link_id in healthy
            )
        }
        if len(controlled_peers) < 2:
            continue
        derived = tuple(
            Evidence(
                evidence_id=f"derived-device-liveness-{device}-{index}",
                entity_type="device",
                entity_id=device,
                category="device_liveness",
                value={"reachable": False, "peer": peers[link_id], "loss_rate": float(failed[link_id].value)},
                source="derived_link_liveness",
                timestamp=datetime.now(UTC),
                reliability=min(1.0, failed[link_id].reliability),
                probe_id="device-scope-link-correlation",
                origin=EvidenceOrigin.ACTIVE_PROBE,
                independence_key=f"link-liveness:{link_id}",
                covered_links=(link_id,),
                path_observation_confidence=1.0,
                metadata={
                    "link_id": link_id,
                    "peer": peers[link_id],
                    "derived_from": failed[link_id].evidence_id,
                    "healthy_peer_control": True,
                    "direct_device_liveness": True,
                },
            )
            for index, link_id in enumerate(sorted(failed_links), start=1)
        )
        result = _build_fault_result(
            base_result,
            fault_type="device_down",
            device=device,
            interface=None,
            evidence=derived,
            confidence=0.95,
            status="device_scope_link_correlation",
        )
        return OperationalClosureResult(
            result=result,
            outcome=ProbeOutcome(
                probe_id="device-scope-link-correlation",
                status="verified",
                evidence=derived,
                tool_calls=0,
                probe_packets=0,
                metadata={"derived_from_existing_active_probes": True},
            ),
            status="device_scope_link_correlation",
        )
    return None


def _candidate_devices(
    topology: TopologyIndex,
    evidence: Sequence[Evidence],
    initial_device: str | None,
    *,
    limit: int = 2,
) -> list[str]:
    counts: Counter[str] = Counter()
    investigation_calls: dict[str, set[str]] = {}
    resolved = topology.resolve_device(initial_device)
    if resolved:
        counts[resolved] += 10_000
    outage_rows: Counter[str] = Counter()
    outage_peers: dict[str, set[str]] = {}
    for item in evidence:
        if item.reliability <= 0:
            continue
        if item.category in {"interface_admin_state", "interface_oper_state"} and ":" in item.entity_id:
            device = topology.resolve_device(item.entity_id.split(":", 1)[0])
            value = item.value
            if device and (value is False or str(value).lower() in {"down", "false", "0", "x"}):
                counts[device] += 30_000
        if item.category == "packet_loss_rate":
            try:
                strong_outage = float(item.value) >= 0.80
            except (TypeError, ValueError):
                strong_outage = False
            source = topology.resolve_device(attachment_from_metadata(item.metadata, "source"))
            destination = topology.resolve_device(attachment_from_metadata(item.metadata, "destination"))
            if strong_outage and source and destination and source != destination:
                for device, peer in ((source, destination), (destination, source)):
                    outage_rows[device] += 1
                    outage_peers.setdefault(device, set()).add(peer)
        for side in ("source", "destination"):
            device = topology.resolve_device(attachment_from_metadata(item.metadata, side))
            if device:
                counts[device] += 1
        # A tool's target is useful as a bounded probe-planning hint even when
        # its observation is healthy.  Deduplicate by invocation so large
        # route tables do not overpower real path symptoms.  These weights
        # only order subsequent live checks; they never satisfy a contract or
        # support a diagnosis.
        if item.origin in {EvidenceOrigin.LIVE_TELEMETRY, EvidenceOrigin.CONFIG_READ}:
            raw_device = item.entity_id.split(":", 1)[0] if item.entity_id else ""
            device = topology.resolve_device(raw_device)
            if device and topology.devices.get(device) != "client" and str(item.source).startswith("base_tool:"):
                investigation_calls.setdefault(device, set()).add(
                    str(item.independence_key or item.probe_id or item.evidence_id)
                )
    # Several near-total-loss rows sharing one attachment switch are more useful for
    # operational triage than a model-suggested packet-loss endpoint.  The
    # large bonus affects only query ordering; it cannot satisfy a contract.
    for device, row_count in outage_rows.items():
        if row_count >= 3 and len(outage_peers.get(device, ())) >= 2:
            counts[device] += 20_000
    for device, calls in investigation_calls.items():
        counts[device] += min(len(calls), 8)
    if counts:
        return [item[0] for item in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]]
    attachments = sorted(topology.attachment_devices)
    if attachments:
        if len(attachments) <= limit:
            return attachments
        if limit == 1:
            return attachments[:1]
        return [*attachments[: limit - 1], attachments[-1]]
    return list(topology.routing_devices()[:limit])


def _episode_window(context: Any) -> tuple[str | None, str | None]:
    symptoms = getattr(context, "symptoms", {}) or {}
    observations = symptoms.get("observations", {}) if isinstance(symptoms, Mapping) else {}
    if not isinstance(observations, Mapping):
        return None, None
    return observations.get("start_time"), observations.get("end_time")


def _with_lookback(start_time: str | None, seconds: int) -> str | None:
    if not start_time or seconds <= 0:
        return start_time
    start = parse_timestamp(start_time)
    if start is None:
        return start_time
    return (start - timedelta(seconds=int(seconds))).isoformat().replace("+00:00", "Z")


def _diverse_clean_integrity_evidence(
    evidence: Sequence[Evidence],
) -> list[Evidence]:
    """Keep one clean active observation per unordered attachment pair."""
    selected: list[Evidence] = []
    seen_pairs: set[tuple[str, ...]] = set()
    for item in evidence:
        if (
            item.category != "payload_integrity_failure"
            or item.value is not False
            or item.reliability <= 0
            or item.origin is not EvidenceOrigin.ACTIVE_PROBE
        ):
            continue
        try:
            missing_packets = int(item.metadata.get("missing_packets") or 0)
        except (TypeError, ValueError):
            continue
        if missing_packets != 0:
            continue
        attachments = tuple(
            sorted(
                {
                    attachment
                    for attachment in (
                        attachment_from_metadata(item.metadata, "source"),
                        attachment_from_metadata(item.metadata, "destination"),
                    )
                    if attachment
                }
            )
        )
        # Older evidence can lack attachment metadata. It may still satisfy a
        # non-scoped integrity check, but repeated rows for the same path must
        # not be counted as diverse controls.
        pair_key = attachments if len(attachments) == 2 else ("entity", item.entity_id)
        if pair_key in seen_pairs:
            continue
        seen_pairs.add(pair_key)
        selected.append(item)
    return selected


def _neighbor_has_recent_transition(row: Mapping[str, Any], start_time: str | None, end_time: str | None) -> bool:
    """Recognize a bounded current-state corroboration of a temporal event."""
    state = str(row.get("state") or row.get("session_state") or "").upper()
    if state and state != "ESTABLISHED":
        return True
    # Cumulative reset/flap counters are not temporal evidence unless the
    # backend explicitly declares that they were scoped to this episode.
    if row.get("window_scoped") is True:
        for key in ("state_changes", "reset_count", "flap_count"):
            try:
                if int(row.get(key) or 0) > 0:
                    return True
            except (TypeError, ValueError):
                pass
    start = parse_timestamp(start_time)
    end = parse_timestamp(end_time)
    try:
        uptime = float(row.get("uptime_seconds") or row.get("up_seconds") or -1)
        if uptime >= 0 and start is not None and end is not None:
            if uptime <= max(30.0, (end - start).total_seconds() + 30.0):
                return True
    except (TypeError, ValueError):
        pass
    for key in ("last_reset", "last_state_change", "last_event"):
        raw = row.get(key)
        if not raw or start is None or end is None:
            continue
        observed = parse_timestamp(raw)
        if observed is None:
            continue
        if start <= observed <= end:
            return True
    return False


def _build_fault_result(
    base: DiagnosisResult,
    *,
    fault_type: str,
    device: str,
    interface: str | None,
    evidence: Sequence[Evidence],
    confidence: float,
    status: str,
) -> DiagnosisResult:
    findings = dict(base.findings or {})
    findings["fault_type"] = fault_type
    findings["location"] = {"device": device, "interface": interface}
    findings["evidence"] = [f"{item.source}: {item.category} on {item.entity_id}" for item in evidence]
    metadata = dict(base.metadata or {})
    harness = dict(metadata.get("diagnostic_harness") or {})
    harness["hard_path_status"] = status
    harness["operational_closure"] = {
        "status": status,
        "evidence_ids": [item.evidence_id for item in evidence],
        "public_tools_only": True,
    }
    metadata["diagnostic_harness"] = harness
    return replace(
        base,
        verdict="fault_detected",
        findings=findings,
        confidence=confidence,
        reasoning=(
            f"Direct public observations identified {fault_type} on {device}{':' + interface if interface else ''}."
        ),
        metadata=metadata,
    )


def _build_healthy_result(base: DiagnosisResult, evidence: Sequence[Evidence]) -> DiagnosisResult:
    findings = dict(base.findings or {})
    findings["fault_type"] = None
    findings["location"] = {"device": None, "interface": None}
    findings["evidence"] = [f"{item.source}: verified {item.category}" for item in evidence]
    metadata = dict(base.metadata or {})
    harness = dict(metadata.get("diagnostic_harness") or {})
    harness["hard_path_status"] = "verified_healthy_closure"
    harness["healthy_verification"] = {
        "status": HealthyVerificationStatus.VERIFIED_HEALTHY.value,
        "evidence_ids": [item.evidence_id for item in evidence],
        "public_tools_only": True,
    }
    metadata["diagnostic_harness"] = harness
    return replace(
        base,
        verdict="network_healthy",
        findings=findings,
        confidence=0.90,
        reasoning="Bounded connectivity, control-plane, interface, and route observations are healthy.",
        metadata=metadata,
    )


def _evidence_indicates_fault(item: Evidence) -> bool:
    """Interpret structured values; a healthy zero is not a fault signal."""
    if not can_support_fault(item) or item.origin is EvidenceOrigin.BASE_CLAIM:
        return False
    value = item.value
    if item.category == "packet_loss_rate":
        try:
            return float(value) >= 0.10
        except (TypeError, ValueError):
            return False
    if item.category in {"latency_median", "latency_p95"}:
        if item.metadata.get("category_anomaly") or item.metadata.get("absolute_anomaly"):
            return True
        try:
            return float(value) >= 30.0
        except (TypeError, ValueError):
            return False
    if item.category == "packet_size_threshold":
        return isinstance(value, Mapping) and bool(value.get("size_dependent_failure"))
    if item.category == "payload_integrity_failure":
        return bool(value)
    if item.category in {"interface_admin_state", "interface_oper_state"}:
        return str(value).lower() in {"down", "false", "0", "x"} or value is False
    if item.category in {"syslog_event", "bgp_neighbor_state"}:
        if isinstance(value, Mapping):
            return bool(value.get("temporal_transition"))
        return str(value).upper() not in {"ESTABLISHED", "UP", "HEALTHY", "NORMAL"}
    if item.category == "configuration_difference":
        return bool(item.metadata.get("direct_configuration_evidence"))
    return False


class OperationalClosure:
    def __init__(
        self,
        config: OperationalVerificationConfig | None = None,
        *,
        timeout_seconds: float = 20.0,
    ):
        self.config = config or OperationalVerificationConfig()
        self.timeout_seconds = timeout_seconds

    def healthy_verification_budget(self, topology: TopologyIndex) -> dict[str, int]:
        """Return a bounded worst-case budget for a healthy certificate.

        This is used only after exact-link active coverage has refuted the
        original performance symptom.  Four calls cover the episode BGP event
        window and one representative device's interfaces, neighbors, and
        routes.  Integrity probes then scale with the same topology-relative
        minimum enforced by :meth:`verify_healthy`, rather than total inventory
        size or a Small-specific constant.
        """
        minimum_integrity_pairs, _minimum_attachment_domains = self._healthy_integrity_requirements(topology)
        samples_per_pair = max(1, int(PayloadIntegrityProbe().config.samples_per_pair))
        return {
            "tool_calls": 4 + minimum_integrity_pairs,
            "active_probes": minimum_integrity_pairs,
            "probe_packets": minimum_integrity_pairs * samples_per_pair,
        }

    def _healthy_integrity_requirements(self, topology: TopologyIndex) -> tuple[int, int]:
        attachment_count = len(topology.attachment_devices)
        desired_integrity_pairs = max(1, (attachment_count + 1) // 2)
        coverage_ratio = min(1.0, max(0.0, float(self.config.healthy_min_attachment_coverage_ratio)))
        ratio_pairs = ceil(attachment_count * coverage_ratio / 2)
        minimum_integrity_pairs = min(
            desired_integrity_pairs,
            max(1, int(self.config.healthy_min_integrity_pairs), ratio_pairs),
        )
        minimum_attachment_domains = min(
            attachment_count,
            max(
                2 if attachment_count > 1 else attachment_count,
                ceil(attachment_count * coverage_ratio),
                minimum_integrity_pairs * 2,
            ),
        )
        return minimum_integrity_pairs, minimum_attachment_domains

    async def verify_bgp(
        self,
        context: Any,
        *,
        base_result: DiagnosisResult,
        topology: TopologyIndex,
        evidence: Sequence[Evidence],
        budget: ProbeBudget,
    ) -> OperationalClosureResult:
        """Confirm a BGP claim from live neighbor state, never from prose."""
        candidates = _candidate_devices(
            topology,
            evidence,
            str(DiagnosisView.from_result(base_result).device or ""),
            limit=2,
        )
        collected: list[Evidence] = []
        errors: list[Evidence] = []
        for device in candidates:
            invocation = await invoke_tool(
                context,
                "get_bgp_neighbors",
                {"device": device},
                budget=budget,
                timeout_seconds=self.timeout_seconds,
            )
            if not invocation.success or invocation.data is None:
                errors.append(
                    tool_error_evidence(
                        evidence_id=f"bgp-verification-{device}-error",
                        probe_id="bgp-verification",
                        entity_id=device,
                        error=invocation.error or "get_bgp_neighbors failed",
                        source="get_bgp_neighbors",
                    )
                )
                continue
            neighbors = invocation.data.get("neighbors")
            rows = [item for item in neighbors if isinstance(item, Mapping)] if isinstance(neighbors, list) else []
            abnormal = [
                item
                for item in rows
                if str(item.get("state") or item.get("session_state") or "").upper() != "ESTABLISHED"
            ]
            if not abnormal:
                continue
            configuration_reason = bgp_configuration_fault_reason(abnormal)
            observation = Evidence(
                evidence_id=f"bgp-verification-{device}",
                entity_type="device",
                entity_id=device,
                category="bgp_neighbor_state",
                value={"neighbors": abnormal, "healthy": False},
                source="get_bgp_neighbors",
                timestamp=datetime.now(UTC),
                reliability=1.0,
                probe_id="bgp-verification",
                origin=EvidenceOrigin.LIVE_TELEMETRY,
                independence_key=f"tool:get_bgp_neighbors:{device}",
                metadata={
                    "semantic_family": "bgp",
                    "direct_bgp_evidence": True,
                    "direct_bgp_configuration_evidence": configuration_reason is not None,
                    "bgp_configuration_fault_reason": configuration_reason,
                },
            )
            collected.append(observation)
            if configuration_reason is None:
                # Preserve the live state as a planning symptom, but do not
                # over-attribute a generic session outage to configuration.
                continue
            result = _build_fault_result(
                base_result,
                fault_type="bgp_neighbor_misconfig",
                device=device,
                interface=None,
                evidence=(observation,),
                confidence=0.95,
                status="direct_bgp_configuration_closure",
            )
            return OperationalClosureResult(
                result,
                self._outcome("bgp-verification", budget, (*collected, *errors), "completed"),
                "fault_suspected",
                "bgp_neighbor_misconfig",
            )
        return OperationalClosureResult(
            None,
            self._outcome("bgp-verification", budget, (*collected, *errors), "inconclusive"),
            "bgp_state_without_configuration_attribution" if collected else "insufficient_observation",
        )

    async def verify_link_state(
        self,
        context: Any,
        *,
        base_result: DiagnosisResult,
        topology: TopologyIndex,
        evidence: Sequence[Evidence],
        budget: ProbeBudget,
    ) -> OperationalClosureResult:
        location = DiagnosisView.from_result(base_result).location
        base_device = str(location.get("device") or "")
        base_interface = str(location.get("interface") or "")
        candidates = _candidate_devices(
            topology,
            evidence,
            base_device,
            limit=2,
        )
        # A live down state is normally reported from one endpoint even when
        # the peer device itself is unavailable.  Validate that topology peer
        # first: one failed interface query followed by two independent
        # one-link liveness probes fits the existing three-call triage cap and
        # distinguishes device scope without trusting the base label.
        peer_validation_targets: set[str] = set()
        base_fault_type = str((base_result.findings or {}).get("fault_type") or "")
        if base_device and base_interface:
            peer = topology.peer(base_device, base_interface)
            if peer is not None and topology.devices.get(peer.device) != "client":
                peer_validation_targets.add(peer.device)
                candidates = [peer.device, *(item for item in candidates if item != peer.device)]
        elif base_device and base_fault_type in {"device_down", "link_down"}:
            # The label is only a planning hint. If querying the claimed
            # device fails, validate it from independent physical neighbors
            # instead of treating the tool error as network evidence.
            peer_validation_targets.add(base_device)
        collected: list[Evidence] = []
        errors: list[Evidence] = []
        # A base-agent tool failure is never network-fault evidence, but its
        # failed action signature is useful for avoiding the same doomed query
        # in a three-call triage window. Spend that unchanged budget on
        # topology-disjoint peer liveness observations instead.
        prior_target_query_failed = bool(
            base_device
            and not base_interface
            and any(
                item.category == "tool_error"
                and item.entity_id == base_device
                and str(item.source).removeprefix("base_tool:") == "get_device_interfaces"
                for item in evidence
            )
        )
        if prior_target_query_failed:
            peer_liveness = await self._collect_peer_liveness(
                context,
                topology=topology,
                target_device=base_device,
                budget=budget,
                max_peers=3,
            )
            collected.extend(peer_liveness)
            failed_links = {item.metadata.get("link_id") for item in peer_liveness}
            if len(failed_links) >= 2:
                result = _build_fault_result(
                    base_result,
                    fault_type="device_down",
                    device=base_device,
                    interface=None,
                    evidence=peer_liveness,
                    confidence=0.95,
                    status="device_scope_peer_liveness_closure",
                )
                return OperationalClosureResult(
                    result,
                    self._outcome("link-state-verification", budget, collected, "completed"),
                    "fault_suspected",
                    "device_down",
                )
        queue = list(candidates)
        queried: set[str] = set()
        link_faults: list[tuple[str, str, list[Evidence], str | None]] = []
        unresolved_peers: set[str] = set()
        while queue:
            device = queue.pop(0)
            if device in queried:
                continue
            queried.add(device)
            invocation = await invoke_tool(
                context,
                "get_device_interfaces",
                {"device": device},
                budget=budget,
                timeout_seconds=self.timeout_seconds,
            )
            if not invocation.success or invocation.data is None:
                errors.append(
                    tool_error_evidence(
                        evidence_id=f"link-state-{device}-error",
                        probe_id="link-state-verification",
                        entity_id=device,
                        error=invocation.error or "interface query failed",
                        source="get_device_interfaces",
                    )
                )
                if any(peer == device for *_rest, peer in link_faults):
                    unresolved_peers.add(device)
                device_scope = self._device_scope_outage(evidence, device)
                peer_scope_target = device in peer_validation_targets or any(
                    peer == device for *_rest, peer in link_faults
                )
                if device_scope or peer_scope_target:
                    peer_liveness = await self._collect_peer_liveness(
                        context,
                        topology=topology,
                        target_device=device,
                        budget=budget,
                    )
                    collected.extend(peer_liveness)
                    failed_links = {
                        item.metadata.get("link_id")
                        for item in peer_liveness
                        if isinstance(item.value, Mapping) and item.value.get("reachable") is False
                    }
                    if len(failed_links) >= 2:
                        direct = (*device_scope, *peer_liveness)
                        result = _build_fault_result(
                            base_result,
                            fault_type="device_down",
                            device=device,
                            interface=None,
                            evidence=direct,
                            confidence=0.95,
                            status="device_scope_peer_liveness_closure",
                        )
                        return OperationalClosureResult(
                            result,
                            self._outcome(
                                "link-state-verification",
                                budget,
                                (*collected, *errors),
                                "completed",
                            ),
                            "fault_suspected",
                            "device_down",
                        )
                continue
            interfaces = invocation.data.get("interfaces")
            if not isinstance(interfaces, list):
                continue
            down_pairs: list[tuple[str, list[Evidence]]] = []
            for item in interfaces:
                if not isinstance(item, Mapping) or not item.get("name"):
                    continue
                interface = str(item["name"])
                endpoint = topology.resolve_interface(device, interface)
                if endpoint is None:
                    continue
                physical = topology.physical_link(device, interface)
                metadata = {"link_id": physical.link_id if physical else None}
                states = (
                    ("interface_admin_state", item.get("admin")),
                    ("interface_oper_state", item.get("oper")),
                )
                pair = [
                    Evidence(
                        evidence_id=f"link-state-{device}-{interface}-{category}",
                        entity_type="interface",
                        entity_id=f"{device}:{endpoint.canonical_interface}",
                        category=category,
                        value=value,
                        source="get_device_interfaces",
                        timestamp=datetime.now(UTC),
                        reliability=1.0,
                        probe_id="link-state-verification",
                        origin=EvidenceOrigin.LIVE_TELEMETRY,
                        independence_key=f"tool:get_device_interfaces:{device}",
                        metadata=metadata,
                    )
                    for category, value in states
                ]
                collected.extend(pair)
                if str(item.get("admin")).lower() == "down" or str(item.get("oper")).lower() == "down":
                    down_pairs.append((endpoint.canonical_interface, pair))
            device_scope = self._device_scope_outage(evidence, device)
            network_rows = [
                item
                for item in interfaces
                if isinstance(item, Mapping)
                and item.get("name")
                and topology.resolve_interface(device, str(item["name"])) is not None
            ]
            device_wide_state = len(down_pairs) >= 2 and len(down_pairs) >= max(2, len(network_rows) // 2)
            if len(down_pairs) >= 2 and (device_scope or device_wide_state):
                result = _build_fault_result(
                    base_result,
                    fault_type="device_down",
                    device=device,
                    interface=None,
                    evidence=(*device_scope, *(item for _, pair in down_pairs for item in pair)),
                    confidence=0.95,
                    status="device_scope_outage_closure",
                )
                return OperationalClosureResult(
                    result,
                    self._outcome("link-state-verification", budget, (*collected, *errors), "completed"),
                    "fault_suspected",
                    "device_down",
                )
            if down_pairs:
                for interface, pair in down_pairs:
                    peer = topology.peer(device, interface)
                    if peer is None:
                        physical = topology.physical_link(device, interface)
                        if physical is not None:
                            peer = physical.endpoint_b if physical.endpoint_a.device == device else physical.endpoint_a
                    admin_down = any(
                        item.category == "interface_admin_state" and str(item.value).lower() == "down" for item in pair
                    )
                    peer_device = (
                        peer.device
                        if not admin_down and peer and topology.devices.get(peer.device) != "client"
                        else None
                    )
                    link_faults.append((device, interface, pair, peer_device))
                    if peer_device is None:
                        result = _build_fault_result(
                            base_result,
                            fault_type="link_down",
                            device=device,
                            interface=interface,
                            evidence=pair,
                            confidence=0.95,
                            status="direct_link_state_closure",
                        )
                        return OperationalClosureResult(
                            result,
                            self._outcome("link-state-verification", budget, (*collected, *errors), "completed"),
                            "fault_suspected",
                            "link_down",
                        )
                    if peer_device and peer_device not in queried and peer_device not in queue:
                        # Validate the peer scope before collapsing a device
                        # outage into a single link fault. The existing stage
                        # budget still bounds this to the next safe query.
                        queue.insert(0, peer_device)

        for device, interface, pair, peer_device in link_faults:
            if peer_device in unresolved_peers:
                continue
            result = _build_fault_result(
                base_result,
                fault_type="link_down",
                device=device,
                interface=interface,
                evidence=pair,
                confidence=0.95,
                status="direct_link_state_closure",
            )
            return OperationalClosureResult(
                result,
                self._outcome("link-state-verification", budget, (*collected, *errors), "completed"),
                "fault_suspected",
                "link_down",
            )
        loss_values: list[float] = []
        for item in evidence:
            if item.category != "packet_loss_rate" or not can_support_fault(item):
                continue
            try:
                loss_values.append(float(item.value))
            except (TypeError, ValueError):
                continue
        # Healthy interfaces under a concentrated outage call for bounded
        # route/config inspection before random-loss isolation.  Partial loss
        # remains a packet-loss problem.
        next_family = None
        if loss_values:
            next_family = "runtime_semantic" if max(loss_values) >= 0.80 else "packet_loss"
        return OperationalClosureResult(
            None,
            self._outcome("link-state-verification", budget, (*collected, *errors), "inconclusive"),
            "insufficient_observation",
            next_family,
        )

    async def _collect_peer_liveness(
        self,
        context: Any,
        *,
        topology: TopologyIndex,
        target_device: str,
        budget: ProbeBudget,
        max_peers: int = 2,
    ) -> tuple[Evidence, ...]:
        """Verify device scope from independent physical neighbors.

        A failed query against the target is infrastructure/missing evidence.
        Two topology-validated, single-link probes from distinct peers provide
        the independent network observation needed to distinguish a device
        outage from one failed interface.
        """
        observations: list[Evidence] = []
        links = []
        for link in topology.links:
            endpoints = (link.endpoint_a, link.endpoint_b)
            target = next((item for item in endpoints if item.device == target_device), None)
            peer = next((item for item in endpoints if item.device != target_device), None)
            if target is None or peer is None or topology.devices.get(peer.device) == "client":
                continue
            links.append((link, target, peer))
        for index, (link, target, peer) in enumerate(links[:max_peers], start=1):
            try:
                budget.reserve_probe()
            except RuntimeError:
                break
            observation, _error = await invoke_link_ping(
                context,
                source=peer.device,
                target_device=target_device,
                source_interface=peer.canonical_interface,
                target_interface=target.canonical_interface,
                count=5,
                budget=budget,
                timeout_seconds=self.timeout_seconds,
            )
            if observation is None or observation.loss_rate < 0.80:
                continue
            observations.append(
                Evidence(
                    evidence_id=f"device-liveness-{target_device}-{index}",
                    entity_type="device",
                    entity_id=target_device,
                    category="device_liveness",
                    value={
                        "reachable": False,
                        "sent": observation.sent,
                        "received": observation.received,
                        "loss_rate": observation.loss_rate,
                    },
                    source="ping_link_test",
                    timestamp=datetime.now(UTC),
                    reliability=1.0,
                    probe_id="device-peer-liveness",
                    origin=EvidenceOrigin.ACTIVE_PROBE,
                    independence_key=f"probe:device-peer-liveness:{link.link_id}",
                    observed_path=(link.link_id,),
                    possible_paths=((link.link_id,),),
                    covered_links=(link.link_id,),
                    path_observation_confidence=1.0,
                    metadata={
                        "link_id": link.link_id,
                        "source_peer": peer.device,
                        "target_device": target_device,
                        "direct_device_liveness": True,
                    },
                )
            )
        return tuple(observations)

    @staticmethod
    def _device_scope_outage(evidence: Sequence[Evidence], device: str) -> tuple[Evidence, ...]:
        """Require multiple endpoint/path observations before device-down closure."""
        matching: list[Evidence] = []
        endpoints: set[str] = set()
        peers: set[str] = set()
        independent_rows: set[str] = set()
        for item in evidence:
            if (
                item.category != "packet_loss_rate"
                or not can_support_fault(item)
                or item.origin is EvidenceOrigin.BASE_CLAIM
            ):
                continue
            try:
                loss = float(item.value)
            except (TypeError, ValueError):
                continue
            if loss < 0.80:
                continue
            source_attachment = attachment_from_metadata(item.metadata, "source") or ""
            destination_attachment = attachment_from_metadata(item.metadata, "destination") or ""
            if device not in {source_attachment, destination_attachment}:
                continue
            local_name = item.metadata.get("src_name") if source_attachment == device else item.metadata.get("dst_name")
            peer_attachment = destination_attachment if source_attachment == device else source_attachment
            if local_name:
                endpoints.add(str(local_name))
            if peer_attachment:
                peers.add(peer_attachment)
            independent_rows.add(item.independence_key or item.evidence_id)
            matching.append(item)
        if len(matching) < 4 or len(peers) < 2 or (len(endpoints) < 2 and len(independent_rows) < 4):
            return ()
        return tuple(matching)

    async def verify_temporal(
        self,
        context: Any,
        *,
        base_result: DiagnosisResult,
        topology: TopologyIndex,
        evidence: Sequence[Evidence],
        budget: ProbeBudget,
    ) -> OperationalClosureResult:
        episode_start, end_time = _episode_window(context)
        start_time = _with_lookback(episode_start, self.config.temporal_lookback_seconds)
        arguments = {key: value for key, value in (("start_time", start_time), ("end_time", end_time)) if value}
        existing_temporal = [
            item
            for item in evidence
            if can_support_fault(item)
            and item.origin is not EvidenceOrigin.BASE_CLAIM
            and item.category in {"bgp_neighbor_state", "syslog_event"}
            and isinstance(item.value, Mapping)
            and item.value.get("temporal_transition")
        ]
        collected: list[Evidence] = list(existing_temporal)
        errors: list[Evidence] = []
        event_devices: set[str] = set()
        log_interfaces: list[tuple[str, str, Evidence]] = []
        for item in existing_temporal:
            entity_device, _, entity_interface = item.entity_id.partition(":")
            device = topology.resolve_device(str(item.metadata.get("device") or entity_device))
            if item.category == "bgp_neighbor_state" and device:
                event_devices.add(device)
            if item.category == "syslog_event" and device:
                interface = str(item.metadata.get("interface") or entity_interface or "")
                endpoint = topology.resolve_interface(device, interface) if interface else None
                if endpoint is not None:
                    log_interfaces.append((device, endpoint.canonical_interface, item))

        invocation = await invoke_tool(
            context,
            "query_bgp_events",
            arguments,
            budget=budget,
            timeout_seconds=self.timeout_seconds,
        )
        if not invocation.success or invocation.data is None:
            errors.append(
                tool_error_evidence(
                    evidence_id="temporal-bgp-error",
                    probe_id="temporal-verification",
                    entity_id="bgp",
                    error=invocation.error or "BGP event query failed",
                    source="query_bgp_events",
                )
            )
        else:
            events = invocation.data.get("events")
            for index, event in enumerate(events if isinstance(events, list) else [], start=1):
                if not isinstance(event, Mapping):
                    continue
                states = event.get("states_observed")
                transition = str(event.get("event_type") or "").lower() == "session_flap" or (
                    isinstance(states, list) and len({str(item) for item in states}) > 1
                )
                if not transition:
                    continue
                device = topology.resolve_device(str(event.get("device") or ""))
                if device:
                    event_devices.add(device)
                collected.append(
                    Evidence(
                        evidence_id=f"temporal-bgp-{index}",
                        entity_type="device",
                        entity_id=device or str(event.get("device") or "bgp"),
                        category="bgp_neighbor_state",
                        value={"temporal_transition": True, "states_observed": states},
                        source="query_bgp_events",
                        timestamp=datetime.now(UTC),
                        reliability=1.0,
                        probe_id="temporal-verification",
                        origin=EvidenceOrigin.LIVE_TELEMETRY,
                        independence_key="tool:query_bgp_events:episode",
                        metadata={"peer": event.get("peer"), "event_type": event.get("event_type")},
                    )
                )

        candidates = _candidate_devices(
            topology,
            evidence,
            str(DiagnosisView.from_result(base_result).device or ""),
            limit=2,
        )
        preferred_event_devices = sorted(
            event_devices,
            key=lambda device: (not topology.is_attachment_device(device), device),
        )
        candidates = [
            *preferred_event_devices,
            *(device for device in candidates if device not in preferred_event_devices),
        ]
        for device in candidates[:2]:
            logs_before = len(log_interfaces)
            log_args: dict[str, Any] = {"device": device}
            if start_time:
                log_args["start_time"] = start_time
            if end_time:
                log_args["end_time"] = end_time
            logs = await invoke_tool(
                context,
                "get_device_logs",
                log_args,
                budget=budget,
                timeout_seconds=self.timeout_seconds,
            )
            if not logs.success or logs.data is None:
                errors.append(
                    tool_error_evidence(
                        evidence_id=f"temporal-log-{device}-error",
                        probe_id="temporal-verification",
                        entity_id=device,
                        error=logs.error or "device log query failed",
                        source="get_device_logs",
                    )
                )
                continue
            raw_logs = logs.data.get("logs")
            for index, item in enumerate(raw_logs if isinstance(raw_logs, list) else [], start=1):
                message = str(item.get("message") if isinstance(item, Mapping) else item)
                match = _PORT_RE.search(message)
                signal = classify_log_signal(message)
                if match is None or signal is None:
                    continue
                endpoint = topology.resolve_interface(device, match.group(1))
                if endpoint is None:
                    continue
                physical = topology.physical_link(device, endpoint.canonical_interface)
                event_time = str(
                    (item.get("timestamp") or item.get("time") or item.get("datetime") or index)
                    if isinstance(item, Mapping)
                    else index
                )
                event = Evidence(
                    evidence_id=f"temporal-log-{device}-{index}",
                    entity_type="interface",
                    entity_id=f"{device}:{endpoint.canonical_interface}",
                    category="syslog_event",
                    value={
                        "temporal_transition": is_temporal_signal(signal),
                        "temporal_signal": signal,
                        "message": message[:240],
                    },
                    source="get_device_logs",
                    timestamp=datetime.now(UTC),
                    reliability=1.0,
                    probe_id="temporal-verification",
                    origin=EvidenceOrigin.LIVE_TELEMETRY,
                    # Distinct timestamped state transitions are independent
                    # temporal samples even when read through one log API.
                    # The final closure below still requires at least two
                    # distinct events on the same physical link.
                    independence_key=(f"event:get_device_logs:{device}:{endpoint.canonical_interface}:{event_time}"),
                    metadata={
                        "link_id": physical.link_id if physical else None,
                        "event_time": event_time,
                        "temporal_signal": signal,
                    },
                )
                collected.append(event)
                log_interfaces.append((device, endpoint.canonical_interface, event))
            # Once a topology-bound transition is found, preserve the final
            # triage call for independent BGP/reset corroboration instead of
            # querying a second speculative device.
            if len(log_interfaces) > logs_before:
                break

        # Some telemetry backends retain interface flap syslog records but do
        # not materialize an episode-level BGP event row.  Corroborate the
        # first topology-bound log with current neighbor reset/uptime state,
        # using at most one remaining read-only call.  A stable established
        # neighbor with no recent transition is not supporting evidence.
        log_groups: dict[str, set[str | None]] = {}
        for log_device, log_interface, item in log_interfaces:
            link = topology.physical_link(log_device, log_interface)
            if link is not None:
                log_groups.setdefault(link.link_id, set()).add(item.independence_key)
        has_direct_log_sequence = any(
            is_repeated_flap_sequence(
                signal_from_value(item.value)
                for log_device, log_interface, item in log_interfaces
                if topology.physical_link(log_device, log_interface) is not None
                and topology.physical_link(log_device, log_interface).link_id == link_id
            )
            for link_id in log_groups
        )
        if (
            log_interfaces
            and not has_direct_log_sequence
            and not any(
                self._link_has_temporal_bgp(topology.physical_link(device, interface), event_devices)
                for device, interface, _event in log_interfaces
                if topology.physical_link(device, interface) is not None
            )
            and budget.remaining_tool_calls > 0
        ):
            device = log_interfaces[0][0]
            neighbors = await invoke_tool(
                context,
                "get_bgp_neighbors",
                {"device": device},
                budget=budget,
                timeout_seconds=self.timeout_seconds,
            )
            if neighbors.success and neighbors.data is not None:
                rows = neighbors.data.get("neighbors")
                recent = [
                    dict(row)
                    for row in (rows if isinstance(rows, list) else [])
                    if isinstance(row, Mapping)
                    if _neighbor_has_recent_transition(row, start_time, end_time)
                ]
                if recent:
                    event_devices.add(device)
                    collected.append(
                        Evidence(
                            evidence_id=f"temporal-bgp-state-{device}",
                            entity_type="device",
                            entity_id=device,
                            category="bgp_neighbor_state",
                            value={"temporal_transition": True, "neighbors": recent},
                            source="get_bgp_neighbors",
                            timestamp=datetime.now(UTC),
                            reliability=1.0,
                            probe_id="temporal-verification",
                            origin=EvidenceOrigin.LIVE_TELEMETRY,
                            independence_key=f"tool:get_bgp_neighbors:{device}",
                            metadata={"temporal_transition": True, "corroborates_syslog": True},
                        )
                    )
            elif neighbors.error:
                errors.append(
                    tool_error_evidence(
                        evidence_id=f"temporal-bgp-state-{device}-error",
                        probe_id="temporal-verification",
                        entity_id=device,
                        error=neighbors.error,
                        source="get_bgp_neighbors",
                    )
                )

        for device, interface, log_evidence in log_interfaces:
            physical = topology.physical_link(device, interface)
            if physical is None:
                continue
            same_link_logs = [
                item
                for log_device, log_interface, item in log_interfaces
                if topology.physical_link(log_device, log_interface) is not None
                and topology.physical_link(log_device, log_interface).link_id == physical.link_id
            ]
            direct_temporal_sequence = is_repeated_flap_sequence(
                signal_from_value(item.value) for item in same_link_logs
            )
            if not direct_temporal_sequence and not self._link_has_temporal_bgp(physical, event_devices):
                continue
            result = _build_fault_result(
                base_result,
                fault_type="link_flapping",
                device=device,
                interface=interface,
                evidence=[
                    *(item for item in collected if item.category == "bgp_neighbor_state"),
                    *(same_link_logs if direct_temporal_sequence else (log_evidence,)),
                ],
                confidence=0.90,
                status="temporal_link_flapping_closure",
            )
            return OperationalClosureResult(
                result,
                self._outcome("temporal-verification", budget, (*collected, *errors), "completed"),
                "fault_suspected",
                "link_flapping",
            )
        # Once temporal evidence was observed, do not silently reinterpret the
        # case as random packet loss.  A caller may still return bounded
        # inconclusive, but it must retain the temporal family for a possible
        # second discriminator.
        next_family = None
        if not any(
            item.category == "bgp_neighbor_state"
            or (item.category == "syslog_event" and is_temporal_signal(signal_from_value(item.value)))
            for item in collected
        ):
            has_positive_loss = False
            for item in evidence:
                if item.category != "packet_loss_rate" or not can_support_fault(item):
                    continue
                try:
                    if float(item.value) > 0:
                        has_positive_loss = True
                        break
                except (TypeError, ValueError):
                    continue
            next_family = "packet_loss" if has_positive_loss else None
        return OperationalClosureResult(
            None,
            self._outcome("temporal-verification", budget, (*collected, *errors), "inconclusive"),
            "insufficient_observation",
            next_family,
        )

    async def verify_healthy(
        self,
        context: Any,
        *,
        base_result: DiagnosisResult,
        topology: TopologyIndex,
        evidence: Sequence[Evidence],
        budget: ProbeBudget,
    ) -> OperationalClosureResult:
        harness_metadata = (
            base_result.metadata.get("diagnostic_harness", {}) if isinstance(base_result.metadata, Mapping) else {}
        )
        base_reliability = (
            harness_metadata.get("base_agent_reliability", {}) if isinstance(harness_metadata, Mapping) else {}
        )
        require_scoped_integrity = bool(
            isinstance(base_reliability, Mapping) and base_reliability.get("semantic_conflict")
        )
        coverage_certificate = next(
            (
                item
                for item in evidence
                if item.category == "coverage_certificate"
                and item.reliability > 0
                and item.origin is EvidenceOrigin.ACTIVE_PROBE
                and item.source == "evidence_reconciliation"
                and isinstance(item.value, Mapping)
                and bool(item.metadata.get("coverage_complete"))
                and item.metadata.get("missing_data_is_healthy") is False
            ),
            None,
        )
        if any(_evidence_indicates_fault(item) for item in evidence):
            return OperationalClosureResult(
                None,
                self._outcome("healthy-verification", budget, (), "fault_suspected"),
                HealthyVerificationStatus.FAULT_SUSPECTED.value,
            )
        episode_connectivity_healthy = self._episode_connectivity_healthy(context)
        if not episode_connectivity_healthy and coverage_certificate is None:
            return OperationalClosureResult(
                None,
                self._outcome("healthy-verification", budget, (), "insufficient"),
                HealthyVerificationStatus.INSUFFICIENT_OBSERVATION.value,
            )

        connectivity_source = (
            "scenario_episode_observation" if episode_connectivity_healthy else "evidence_reconciliation"
        )
        collected: list[Evidence] = [
            Evidence(
                evidence_id="healthy-connectivity-window",
                entity_type="observation_window",
                entity_id="pingmesh",
                category="packet_loss_rate",
                value=0.0,
                source=connectivity_source,
                timestamp=datetime.now(UTC),
                reliability=1.0,
                probe_id="healthy-verification",
                origin=EvidenceOrigin.PUBLIC_OBSERVATION,
                independence_key=(
                    "public:pingmesh:episode" if episode_connectivity_healthy else "coverage:active-exact-links"
                ),
                metadata={
                    "verified_healthy": True,
                    "coverage_complete": True,
                    "reconciled_active_coverage": not episode_connectivity_healthy,
                },
            )
        ]
        if coverage_certificate is not None:
            collected.append(coverage_certificate)
        errors: list[Evidence] = []
        nonblocking_integrity_errors: list[Evidence] = []
        start_time, end_time = _episode_window(context)
        event_args = {key: value for key, value in (("start_time", start_time), ("end_time", end_time)) if value}
        events = await invoke_tool(
            context,
            "query_bgp_events",
            event_args,
            budget=budget,
            timeout_seconds=self.timeout_seconds,
        )
        if not events.success or events.data is None:
            errors.append(
                tool_error_evidence(
                    evidence_id="healthy-bgp-events-error",
                    probe_id="healthy-verification",
                    entity_id="bgp",
                    error=events.error or "BGP event query failed",
                    source="query_bgp_events",
                )
            )
        elif events.data.get("events"):
            return OperationalClosureResult(
                None,
                self._outcome("healthy-verification", budget, (*collected, *errors), "fault_suspected"),
                HealthyVerificationStatus.FAULT_SUSPECTED.value,
                "temporal_verification",
            )
        else:
            collected.append(self._healthy_evidence("healthy-bgp-events", "bgp_neighbor_state", "bgp", "no_events"))

        # Pingmesh gives fabric-wide reachability coverage. One representative
        # device supplies bounded control-plane/interface/route confirmation;
        # the remaining budget is reserved for an integrity sentinel so a
        # corruption case cannot be declared healthy merely because ping and
        # control-plane state look normal.
        for device in _candidate_devices(topology, evidence, None, limit=1):
            for tool, arguments, category in (
                ("get_device_interfaces", {"device": device}, "interface_oper_state"),
                ("get_bgp_neighbors", {"device": device}, "bgp_neighbor_state"),
                ("get_route_table", {"device": device, "max_routes": 20}, "route_presence"),
            ):
                existing = self._existing_healthy_observation(evidence, category, device=device)
                if existing is not None:
                    collected.append(existing)
                    continue
                invocation = await invoke_tool(
                    context,
                    tool,
                    arguments,
                    budget=budget,
                    timeout_seconds=self.timeout_seconds,
                )
                if not invocation.success or invocation.data is None:
                    errors.append(
                        tool_error_evidence(
                            evidence_id=f"healthy-{tool}-{device}-error",
                            probe_id="healthy-verification",
                            entity_id=device,
                            error=invocation.error or f"{tool} failed",
                            source=tool,
                        )
                    )
                    continue
                state = self._healthy_tool_state(tool, invocation.data)
                if state is False:
                    return OperationalClosureResult(
                        None,
                        self._outcome("healthy-verification", budget, (*collected, *errors), "fault_suspected"),
                        HealthyVerificationStatus.FAULT_SUSPECTED.value,
                        "generic_verification",
                    )
                if state is True:
                    collected.append(self._healthy_evidence(f"healthy-{tool}-{device}", category, device, "normal"))
                else:
                    errors.append(
                        tool_error_evidence(
                            evidence_id=f"healthy-{tool}-{device}-missing",
                            probe_id="healthy-verification",
                            entity_id=device,
                            error="required observation is missing",
                            source=tool,
                        )
                    )

        attachment_count = len(topology.attachment_devices)
        integrity_probe = PayloadIntegrityProbe()
        integrity_graph = None
        if require_scoped_integrity:
            try:
                integrity_graph = TopologyGraph.from_context(context)
            except (OSError, TypeError, ValueError):
                pass
        # Scale negative integrity coverage with the topology, but only inside
        # the existing per-case tool/active-probe/packet budget.  The previous
        # fixed three pairs covered six attachment domains regardless of topology
        # size and could incorrectly certify a larger fabric after observing less
        # than half of its failure domains.
        desired_integrity_pairs = max(1, (attachment_count + 1) // 2)
        minimum_integrity_pairs, minimum_attachment_domains = self._healthy_integrity_requirements(topology)
        existing_integrity = _diverse_clean_integrity_evidence(evidence)
        collected.extend(existing_integrity)
        existing_covered_attachments = {
            attachment
            for item in existing_integrity
            for attachment in (
                attachment_from_metadata(item.metadata, "source"),
                attachment_from_metadata(item.metadata, "destination"),
            )
            if attachment and topology.is_attachment_device(attachment)
        }
        remaining_integrity_capacity = min(
            budget.remaining_stage_tool_calls,
            budget.remaining_active_probes,
            budget.remaining_probe_packets // max(1, integrity_probe.config.samples_per_pair),
        )
        planned_integrity_pairs = min(
            desired_integrity_pairs,
            len(existing_integrity) + remaining_integrity_capacity,
        )
        pairs_needed_for_count = max(0, planned_integrity_pairs - len(existing_integrity))
        pairs_needed_for_domains = ceil(
            max(0, minimum_attachment_domains - len(existing_covered_attachments)) / 2
        )
        new_integrity_pairs = min(
            remaining_integrity_capacity,
            max(pairs_needed_for_count, pairs_needed_for_domains),
        )
        required_integrity_pairs = len(existing_integrity) + new_integrity_pairs
        pairs = select_probe_pairs(
            context,
            family="packet_corruption",
            max_pairs=new_integrity_pairs,
            evidence=evidence,
            anomaly_pairs=required_integrity_pairs,
            stratified_attachment_coverage=True,
            covered_attachment_domains=tuple(existing_covered_attachments),
            preferred_leaves=tuple(
                device
                for device in _candidate_devices(topology, evidence, None, limit=2)
                if topology.is_attachment_device(device)
            ),
        )
        completed_integrity_pairs = len(existing_integrity)
        unscoped_integrity_pairs = sum(
            1
            for item in existing_integrity
            if require_scoped_integrity and not (item.covered_links or item.possible_paths)
        )
        if new_integrity_pairs and not pairs:
            errors.append(
                tool_error_evidence(
                    evidence_id="healthy-integrity-pair-missing",
                    probe_id="healthy-verification",
                    entity_id="payload-integrity",
                    error="no safe integrity probe pair is available",
                    source="payload_integrity_test",
                )
            )
        elif pairs:
            for index, pair in enumerate(pairs, start=1):
                integrity = await integrity_probe.run(
                    context,
                    pair=pair,
                    budget=budget,
                    probe_id=f"healthy-integrity-sentinel-{index}",
                )
                reliable_integrity = [item for item in integrity.evidence if item.reliability > 0]
                if integrity_graph is not None:
                    reliable_integrity = [scope_path_evidence(integrity_graph, item) for item in reliable_integrity]
                collected.extend(reliable_integrity)
                nonblocking_integrity_errors.extend(item for item in integrity.evidence if item.reliability <= 0)
                if any(
                    item.category == "payload_integrity_failure" and bool(item.value) for item in reliable_integrity
                ):
                    return OperationalClosureResult(
                        None,
                        self._outcome("healthy-verification", budget, (*collected, *errors), "fault_suspected"),
                        HealthyVerificationStatus.FAULT_SUSPECTED.value,
                        "packet_corruption",
                    )
                if any(
                    item.category == "packet_loss_rate" and float(item.value or 0.0) > 0 for item in reliable_integrity
                ):
                    return OperationalClosureResult(
                        None,
                        self._outcome("healthy-verification", budget, (*collected, *errors), "fault_suspected"),
                        HealthyVerificationStatus.FAULT_SUSPECTED.value,
                        "packet_loss",
                    )
                if any(
                    item.category == "payload_integrity_failure" and item.value is False for item in reliable_integrity
                ):
                    completed_integrity_pairs += 1
                    if require_scoped_integrity and not any(
                        item.category == "payload_integrity_failure"
                        and item.value is False
                        and (item.covered_links or item.possible_paths)
                        for item in reliable_integrity
                    ):
                        unscoped_integrity_pairs += 1
            if completed_integrity_pairs < minimum_integrity_pairs:
                errors.append(
                    tool_error_evidence(
                        evidence_id="healthy-integrity-coverage-incomplete",
                        probe_id="healthy-verification",
                        entity_id="payload-integrity",
                        error=(
                            f"only {completed_integrity_pairs}/{minimum_integrity_pairs} "
                            "minimum diverse integrity pairs completed "
                            f"(planned breadth={required_integrity_pairs})"
                        ),
                        source="payload_integrity_test",
                    )
                )
            if unscoped_integrity_pairs:
                errors.append(
                    tool_error_evidence(
                        evidence_id="healthy-integrity-path-scope-missing",
                        probe_id="healthy-verification",
                        entity_id="payload-integrity",
                        error=(
                            f"{unscoped_integrity_pairs} healthy integrity observations lacked "
                            "a topology path scope while public observations conflicted"
                        ),
                        source="payload_integrity_test",
                    )
                )

        covered_attachments = {
            attachment
            for item in collected
            if item.category == "payload_integrity_failure" and item.value is False
            for attachment in (
                attachment_from_metadata(item.metadata, "source"),
                attachment_from_metadata(item.metadata, "destination"),
            )
            if attachment and topology.is_attachment_device(attachment)
        }
        if len(covered_attachments) < minimum_attachment_domains:
            errors.append(
                tool_error_evidence(
                    evidence_id="healthy-integrity-attachment-coverage-incomplete",
                    probe_id="healthy-verification",
                    entity_id="payload-integrity",
                    error=(
                        f"only {len(covered_attachments)}/{minimum_attachment_domains} required attachment "
                        "domains received a scoped integrity sentinel"
                    ),
                    source="payload_integrity_test",
                )
            )

        required = {
            "packet_loss_rate",
            "payload_integrity_failure",
            "bgp_neighbor_state",
            "interface_oper_state",
            "route_presence",
        }
        observed = {item.category for item in collected if item.reliability > 0}
        if errors or not required.issubset(observed):
            return OperationalClosureResult(
                None,
                self._outcome("healthy-verification", budget, (*collected, *errors), "inconclusive"),
                HealthyVerificationStatus.INSUFFICIENT_OBSERVATION.value,
            )
        integrity_sampling_sufficient = (
            completed_integrity_pairs >= minimum_integrity_pairs
            and len(covered_attachments) >= minimum_attachment_domains
        )
        collected.append(
            Evidence(
                evidence_id="healthy-coverage-certificate",
                entity_type="topology",
                entity_id="network",
                category="coverage_certificate",
                value={
                    "global_reachability_complete": True,
                    "attachment_domains_total": attachment_count,
                    "attachment_domains_sampled_for_integrity": len(covered_attachments),
                    "integrity_pairs_completed": completed_integrity_pairs,
                    "minimum_integrity_pairs": minimum_integrity_pairs,
                    "minimum_attachment_domains": minimum_attachment_domains,
                    "attachment_coverage_ratio": (
                        len(covered_attachments) / attachment_count if attachment_count else 1.0
                    ),
                    "integrity_sampling_sufficient": integrity_sampling_sufficient,
                    "healthy_scope": "service_reachability_with_stratified_integrity_sampling",
                },
                source="healthy_verification",
                timestamp=datetime.now(UTC),
                reliability=1.0,
                probe_id="healthy-verification",
                origin=EvidenceOrigin.PUBLIC_OBSERVATION,
                independence_key="coverage:pingmesh-plus-stratified-integrity",
                # A coverage certificate explains why bounded sampling is
                # adequate; it is not itself evidence of normal or faulty
                # network behavior and therefore cannot support a hypothesis.
                supports_submission=False,
                metadata={
                    "sampling_policy": "global_pingmesh_plus_diverse_integrity_sentinels",
                    "coverage_complete": True,
                    "healthy_scope": "service_reachability_with_stratified_integrity_sampling",
                    "device_state_scope": "representative_device_plus_global_bgp_event_window",
                    "missing_data_is_healthy": False,
                },
            )
        )
        result = _build_healthy_result(base_result, collected)
        return OperationalClosureResult(
            result,
            self._outcome(
                "healthy-verification",
                budget,
                (*collected, *nonblocking_integrity_errors),
                "completed",
            ),
            HealthyVerificationStatus.VERIFIED_HEALTHY.value,
        )

    @staticmethod
    def _link_has_temporal_bgp(link: PhysicalLink, devices: set[str]) -> bool:
        # A BGP reset is commonly recorded only on one side of a flapping
        # adjacency.  The syslog event already binds the other side to the
        # physical link, so requiring both devices here loses real flaps at
        # larger scale.  One endpoint's transition plus the direct interface
        # log is sufficient; final submission still requires the gate's
        # independent evidence checks.
        return bool({link.endpoint_a.device, link.endpoint_b.device}.intersection(devices))

    @staticmethod
    def _episode_connectivity_healthy(context: Any) -> bool:
        symptoms = getattr(context, "symptoms", {}) or {}
        observations = symptoms.get("observations", {}) if isinstance(symptoms, Mapping) else {}
        if not isinstance(observations, Mapping):
            return False
        if observations.get("data_source_status") not in {None, "ok"}:
            return False
        if observations.get("coverage_status") not in {None, "complete"}:
            return False
        if observations.get("anomalies_detected") is True:
            return False
        pingmesh = observations.get("pingmesh_metrics")
        if not isinstance(pingmesh, Mapping):
            return False
        status = pingmesh.get("query_status")
        if isinstance(status, Mapping) and status.get("ok") is False:
            return False
        anomalies = pingmesh.get("anomalies")
        return isinstance(anomalies, list) and not anomalies

    @staticmethod
    def _healthy_tool_state(tool: str, payload: Mapping[str, Any]) -> bool | None:
        if payload.get("error"):
            return None
        if tool == "get_device_interfaces":
            items = payload.get("interfaces")
            if not isinstance(items, list) or not items:
                return None
            return all(
                str(item.get("admin")).lower() == "up" and str(item.get("oper")).lower() == "up"
                for item in items
                if isinstance(item, Mapping)
            )
        if tool == "get_bgp_neighbors":
            items = payload.get("neighbors")
            if not isinstance(items, list) or not items:
                return None
            return all(str(item.get("state") or item.get("session_state")).upper() == "ESTABLISHED" for item in items)
        if tool == "get_route_table":
            routes = payload.get("routes")
            return bool(routes) if isinstance(routes, list) else None
        return None

    @staticmethod
    def _existing_healthy_observation(
        evidence: Sequence[Evidence],
        category: str,
        *,
        device: str,
    ) -> Evidence | None:
        """Reuse fresh structured observations before spending another call."""
        for item in evidence:
            if (
                item.category != category
                # Reuse must obey the same eligibility boundary as the final
                # evidence contract.  Planning-only base-tool observations may
                # guide the query target, but cannot silently replace the live
                # observation that the healthy result later depends on.
                or not can_support_fault(item)
                or item.origin is EvidenceOrigin.BASE_CLAIM
                or not item.entity_id.startswith(device)
                or _evidence_indicates_fault(item)
            ):
                continue
            value = item.value
            if category == "interface_oper_state" and str(value).lower() == "up":
                return item
            if category == "bgp_neighbor_state" and str(value).upper() in {"ESTABLISHED", "UP", "HEALTHY"}:
                return item
            if category == "route_presence" and isinstance(value, Mapping):
                if value.get("is_discard"):
                    continue
                if value.get("present") is True or int(value.get("route_count") or 0) > 0:
                    return item
        return None

    @staticmethod
    def _healthy_evidence(evidence_id: str, category: str, entity_id: str, value: Any) -> Evidence:
        return Evidence(
            evidence_id=evidence_id,
            entity_type="device",
            entity_id=entity_id,
            category=category,
            value=value,
            source="healthy_verification",
            timestamp=datetime.now(UTC),
            reliability=1.0,
            probe_id="healthy-verification",
            origin=EvidenceOrigin.LIVE_TELEMETRY,
            independence_key=f"healthy:{evidence_id}",
            metadata={"verified_healthy": True},
        )

    @staticmethod
    def _outcome(
        probe_id: str,
        budget: ProbeBudget,
        evidence: Sequence[Evidence],
        status: str,
    ) -> ProbeOutcome:
        return ProbeOutcome(
            probe_id=probe_id,
            status=status,
            evidence=tuple(evidence),
            tool_calls=budget.tool_calls,
            probe_packets=0,
            metadata={"runtime_error_used_as_fault_evidence": False},
        )


__all__ = [
    "device_down_from_link_probes",
    "HealthyVerificationStatus",
    "OperationalClosure",
    "OperationalClosureResult",
]
