"""Declarative evidence requirements shared by fast path and final submission."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from netopsbench.sdk.agents import DiagnosisResult

from ..evidence.validator import can_support_fault
from ..models import DiagnosisView, Evidence, EvidenceOrigin, RankedInterfaceCandidate


@dataclass(frozen=True)
class ContractDecision:
    satisfied: bool
    missing_requirements: tuple[str, ...] = ()
    contradictions: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()


def _group_key(item: Evidence) -> str:
    if item.independence_key:
        return item.independence_key
    if item.probe_id:
        return f"probe:{item.source}:{item.probe_id}"
    return f"observation:{item.source}"


def _coverage_count_at_least(value: object, minimum: object) -> bool:
    """Compare certificate counts without letting malformed data open the gate."""
    try:
        return int(value) >= max(1, int(minimum))
    except (TypeError, ValueError, OverflowError):
        return False


def _live(items: Sequence[Evidence]) -> list[Evidence]:
    return [item for item in items if can_support_fault(item) and item.origin is not EvidenceOrigin.BASE_CLAIM]


def _semantic(item: Evidence, family: str) -> bool:
    return str(item.metadata.get("semantic_family") or "") == family


def _is_down(item: Evidence) -> bool:
    return item.category in {"interface_admin_state", "interface_oper_state"} and (
        item.value is False or str(item.value).lower() in {"down", "false", "0", "x"}
    )


def _is_healthy_value(item: Evidence) -> bool:
    if item.category == "packet_loss_rate":
        try:
            return float(item.value) <= 0.01
        except (TypeError, ValueError):
            return False
    if item.category in {"latency_median", "latency_p95"}:
        return not bool(
            item.metadata.get("category_anomaly")
            or item.metadata.get("absolute_anomaly")
            or item.metadata.get("relative_anomaly")
        )
    if item.category == "payload_integrity_failure":
        return item.value is False
    if item.category in {"interface_admin_state", "interface_oper_state"}:
        return not _is_down(item)
    if item.category == "bgp_neighbor_state":
        if isinstance(item.value, Mapping):
            return not bool(item.value.get("temporal_transition"))
        return str(item.value).upper() in {"ESTABLISHED", "UP", "HEALTHY", "NORMAL", "NO_EVENTS"}
    return item.category == "route_presence"


class EvidenceContractEvaluator:
    """Evaluate observable requirements without trusting a model's prose."""

    def evaluate(
        self,
        result: DiagnosisResult,
        evidence: Sequence[Evidence],
        *,
        candidates: Sequence[RankedInterfaceCandidate] = (),
        require_location: bool = True,
    ) -> ContractDecision:
        items = _live(evidence)
        view = DiagnosisView.from_result(result)
        fault_type = view.fault_type
        location = view.location
        missing: list[str] = []
        capabilities: list[str] = []

        if result.verdict == "network_healthy":
            healthy = [item for item in items if _is_healthy_value(item)]
            categories = {item.category for item in healthy}
            requirements = (
                ("packet_loss_rate", "healthy_connectivity_observation", "connectivity_check"),
                ("payload_integrity_failure", "healthy_payload_integrity_observation", "payload_integrity"),
                ("bgp_neighbor_state", "healthy_control_plane_observation", "bgp_state"),
                ("route_presence", "healthy_route_observation", "route_state"),
            )
            for category, name, capability in requirements:
                if category not in categories:
                    missing.append(name)
                    capabilities.append(capability)
            if not categories.intersection({"interface_admin_state", "interface_oper_state"}):
                missing.append("healthy_interface_observation")
                capabilities.append("interface_state")
            if len({_group_key(item) for item in healthy}) < 2:
                missing.append("independent_healthy_observations")
            coverage = next(
                (
                    item
                    for item in evidence
                    if item.category == "coverage_certificate"
                    and isinstance(item.value, Mapping)
                    and item.origin is EvidenceOrigin.PUBLIC_OBSERVATION
                    and item.source == "healthy_verification"
                    and not item.supports_submission
                    and item.metadata.get("coverage_complete") is True
                    and item.metadata.get("missing_data_is_healthy") is False
                    and item.value.get("healthy_scope")
                    == "service_reachability_with_stratified_integrity_sampling"
                ),
                None,
            )
            if coverage is None:
                missing.append("healthy_scope_coverage_certificate")
                capabilities.append("coverage_accounting")
            elif not (
                coverage.value.get("global_reachability_complete") is True
                and coverage.value.get("integrity_sampling_sufficient") is True
                and _coverage_count_at_least(
                    coverage.value.get("integrity_pairs_completed"),
                    coverage.value.get("minimum_integrity_pairs"),
                )
                and _coverage_count_at_least(
                    coverage.value.get("attachment_domains_sampled_for_integrity"),
                    coverage.value.get("minimum_attachment_domains"),
                )
            ):
                missing.append("healthy_integrity_coverage_incomplete")
                capabilities.append("payload_integrity")
            return ContractDecision(
                not missing,
                tuple(dict.fromkeys(missing)),
                required_capabilities=tuple(dict.fromkeys(capabilities)),
            )

        if result.verdict != "fault_detected" or not fault_type:
            return ContractDecision(False, ("canonical_fault_claim",), required_capabilities=("triage",))

        categories = {item.category for item in items}
        if fault_type == "link_down":
            down_interfaces = [item for item in items if _is_down(item) and item.entity_type == "interface"]
            if not down_interfaces:
                missing.append("direct_interface_state")
                capabilities.append("interface_state")
            # One down interface cannot distinguish a failed physical link
            # from an unreachable peer device: both present identically on
            # the surviving endpoint.  Require a second, live observation of
            # the peer scope before allowing the zero-cost fast path.  The
            # operational closure can obtain this with a bounded peer query
            # or multi-neighbour liveness probes.
            down_link_ids = {
                str(item.metadata.get("link_id")) for item in down_interfaces if item.metadata.get("link_id")
            }
            # An explicit local administrative-down state already proves the
            # queried device is alive and the scope is the interface. Oper-
            # down/admin-up remains ambiguous until the peer is observed.
            observed_link_devices: dict[str, set[str]] = {}
            for item in items:
                link_id = str(item.metadata.get("link_id") or "")
                if (
                    link_id in down_link_ids
                    and item.entity_type == "interface"
                    and ":" in item.entity_id
                    and item.category in {"interface_admin_state", "interface_oper_state"}
                ):
                    observed_link_devices.setdefault(link_id, set()).add(item.entity_id.split(":", 1)[0])
            peer_scope_observed = (
                any(item.category == "interface_admin_state" and _is_down(item) for item in down_interfaces)
                or any(len(devices) >= 2 for devices in observed_link_devices.values())
                or any(
                    (
                        item.category == "device_liveness"
                        and isinstance(item.value, Mapping)
                        and item.value.get("reachable") is True
                        and bool(down_link_ids.intersection({str(item.metadata.get("link_id"))}))
                    )
                    for item in items
                )
            )
            if not peer_scope_observed:
                missing.append("peer_scope_liveness")
                capabilities.append("interface_state")
        elif fault_type == "device_down":
            target = str(location.get("device") or "")
            down_entities = {
                item.entity_id
                for item in items
                if _is_down(item) and (not target or item.entity_id.startswith(f"{target}:"))
            }
            failed_peer_links = {
                str(item.metadata.get("link_id"))
                for item in items
                if item.category == "device_liveness"
                and item.entity_id == target
                and isinstance(item.value, Mapping)
                and item.value.get("reachable") is False
                and item.metadata.get("link_id")
            }
            if len(down_entities) < 2 and len(failed_peer_links) < 2:
                missing.append("device_scope_operational_evidence")
                capabilities.append("interface_state")
        elif fault_type == "link_flapping":
            temporal = [item for item in items if item.category in {"syslog_event", "bgp_neighbor_state"}]
            if not temporal:
                missing.append("temporal_transition_evidence")
                capabilities.append("temporal_events")
            elif len({_group_key(item) for item in temporal}) < 2:
                missing.append("independent_temporal_confirmation")
                capabilities.append("temporal_events")
        elif fault_type == "acl_misconfig":
            if not any(item.category == "configuration_difference" and _semantic(item, "acl") for item in items):
                missing.append("direct_acl_configuration")
                capabilities.append("acl_config")
            if "interface_counter_delta" not in categories:
                missing.append("acl_dataplane_consequence")
                capabilities.append("acl_counters")
        elif fault_type == "route_policy_misconfig":
            if not any(
                item.category == "configuration_difference" and _semantic(item, "route_policy") for item in items
            ):
                missing.append("direct_route_policy_configuration")
                capabilities.append("route_config")
            if not categories.intersection({"route_presence", "observed_routing_entry"}):
                missing.append("route_policy_consequence")
                capabilities.append("route_state")
        elif fault_type in {"static_route_misconfig", "blackhole_route"}:
            direct_route = [
                item
                for item in items
                if item.category
                in {"configured_static_route", "configuration_difference", "route_presence", "observed_routing_entry"}
            ]
            if not direct_route:
                missing.append("direct_route_observation")
                capabilities.append("route_config")
            if fault_type == "blackhole_route":
                discard = any(
                    isinstance(item.value, Mapping)
                    and (
                        bool(item.value.get("is_discard"))
                        or str(
                            item.value.get("next_hop")
                            or item.value.get("configured_next_hop")
                            or item.value.get("observed_next_hop")
                            or ""
                        ).lower()
                        in {"null0", "blackhole", "discard"}
                    )
                    for item in direct_route
                )
                if not discard:
                    missing.append("selected_discard_route")
                    capabilities.append("route_state")
        elif fault_type == "bgp_neighbor_misconfig":
            bgp_state = [item for item in items if item.category == "bgp_neighbor_state"]
            configuration = [
                item
                for item in items
                if (
                    item.category == "configuration_difference" and _semantic(item, "bgp")
                )
                or (
                    item.category == "bgp_neighbor_state"
                    and bool(item.metadata.get("direct_bgp_configuration_evidence"))
                )
            ]
            if not bgp_state:
                missing.append("direct_bgp_state_observation")
                capabilities.append("bgp_state")
            if not configuration:
                missing.append("direct_bgp_configuration_attribution")
                capabilities.append("bgp_config")
        elif fault_type == "packet_loss":
            if not any(
                item.category == "packet_loss_rate"
                and float(item.value or 0.0) >= 0.10
                and (int(item.metadata.get("rounds") or 0) >= 2 or item.path_observation_confidence >= 1.0)
                for item in items
            ):
                missing.append("repeated_loss_observation")
                capabilities.append("packet_loss_probe")
        elif fault_type == "packet_corruption":
            if not any(item.category == "payload_integrity_failure" and bool(item.value) for item in items):
                missing.append("invalid_payload_observation")
                capabilities.append("payload_integrity")
        elif fault_type == "mtu_mismatch":
            if not any(
                item.category == "packet_size_threshold"
                and isinstance(item.value, Mapping)
                and item.value.get("size_dependent_failure")
                for item in items
            ):
                missing.append("size_dependent_observation")
                capabilities.append("mtu_sweep")
            if "configuration_difference" not in categories:
                missing.append("peer_mtu_difference")
                capabilities.append("peer_config")
        elif fault_type == "high_latency":
            if not any(
                item.category in {"latency_median", "latency_p95"}
                and (
                    item.metadata.get("category_anomaly")
                    or item.metadata.get("absolute_anomaly")
                    or item.metadata.get("relative_anomaly")
                )
                for item in items
            ):
                missing.append("latency_contrast_observation")
                capabilities.append("rtt_probe")

        interface_faults = {
            "acl_misconfig",
            "high_latency",
            "link_down",
            "link_flapping",
            "mtu_mismatch",
            "packet_corruption",
            "packet_loss",
        }
        if require_location and fault_type in interface_faults:
            if not location.get("device") or not location.get("interface"):
                missing.append("complete_interface_location")
            if candidates and candidates[0].score <= 0:
                missing.append("ranked_physical_link")

        if (
            fault_type not in {"bgp_neighbor_misconfig", "link_down", "device_down", "link_flapping"}
            and len({_group_key(item) for item in items}) < 2
        ):
            missing.append("independent_observation_groups")

        return ContractDecision(
            not missing,
            tuple(dict.fromkeys(missing)),
            required_capabilities=tuple(dict.fromkeys(capabilities)),
        )


__all__ = ["ContractDecision", "EvidenceContractEvaluator"]
