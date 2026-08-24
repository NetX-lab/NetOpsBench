"""Conservative extraction of independently supported secondary faults."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass

from ..evidence.validator import can_support_fault
from ..models import Evidence, EvidenceOrigin, RankedInterfaceCandidate


@dataclass(frozen=True)
class IndependentFault:
    fault_type: str
    device: str | None
    interface: str | None
    link_id: str | None
    confidence: float
    evidence_ids: tuple[str, ...]

    def as_finding(self) -> dict[str, object]:
        return asdict(self)


class MultiFaultAnalyzer:
    """Report secondary root causes only when their own contract closes."""

    def detect(
        self,
        evidence: Sequence[Evidence],
        candidates: Sequence[RankedInterfaceCandidate],
        *,
        primary_fault_type: str | None,
        primary_device: str | None,
        primary_interface: str | None,
        primary_link_id: str | None = None,
    ) -> tuple[IndependentFault, ...]:
        usable = [item for item in evidence if can_support_fault(item) and item.origin is not EvidenceOrigin.BASE_CLAIM]
        found = [*self._impairments(usable, {item.link_id: item for item in candidates}), *self._semantic(usable)]
        primary = (primary_fault_type, primary_device, primary_interface)
        unique: dict[tuple[str, str | None, str | None], IndependentFault] = {}
        for item in found:
            key = (item.fault_type, item.device, item.interface)
            if key == primary:
                continue
            # Different symptoms on one physical failure domain are competing
            # explanations, not independent faults. Loss, for example, is a
            # normal consequence of corruption. Removing the checksum evidence
            # as "secondary" would make the weaker loss label win re-scoring.
            if item.link_id and primary_link_id and item.link_id == primary_link_id:
                continue
            current = unique.get(key)
            if current is None or item.confidence > current.confidence:
                unique[key] = item
        return tuple(sorted(unique.values(), key=lambda item: (-item.confidence, item.fault_type, item.link_id or "")))

    @staticmethod
    def explained_evidence_ids(faults: Sequence[IndependentFault]) -> frozenset[str]:
        return frozenset(evidence_id for fault in faults for evidence_id in fault.evidence_ids)

    def _impairments(
        self,
        evidence: Sequence[Evidence],
        by_link: Mapping[str, RankedInterfaceCandidate],
    ) -> list[IndependentFault]:
        grouped: dict[tuple[str, str], list[Evidence]] = defaultdict(list)
        for item in evidence:
            for link_id in self._exact_links(item):
                if item.category == "packet_size_threshold" and self._size_failure(item):
                    grouped[("mtu_mismatch", link_id)].append(item)
                elif item.category == "configuration_difference" and self._mtu_difference(item):
                    grouped[("mtu_mismatch", link_id)].append(item)
                elif item.category == "payload_integrity_failure" and bool(item.value):
                    grouped[("packet_corruption", link_id)].append(item)
                elif item.category == "packet_loss_rate" and self._strong_loss(item):
                    grouped[("packet_loss", link_id)].append(item)
                elif item.category in {"latency_median", "latency_p95"} and self._latency_anomaly(item):
                    grouped[("high_latency", link_id)].append(item)
                elif item.category in {"interface_admin_state", "interface_oper_state"} and self._down(item):
                    grouped[("link_down", link_id)].append(item)
                elif item.category in {"syslog_event", "bgp_neighbor_state"} and self._transition(item):
                    grouped[("link_flapping", link_id)].append(item)

        result: list[IndependentFault] = []
        for (fault_type, link_id), items in grouped.items():
            candidate = by_link.get(link_id)
            if candidate is None:
                continue
            keys = {self._key(item) for item in items}
            if not self._contract(fault_type, items, keys):
                continue
            device, interface = self._endpoint(items, candidate)
            result.append(
                IndependentFault(
                    fault_type=fault_type,
                    device=device,
                    interface=interface,
                    link_id=link_id,
                    confidence=min(0.99, 0.85 + 0.03 * max(0, len(keys) - 2)),
                    evidence_ids=tuple(dict.fromkeys(item.evidence_id for item in items)),
                )
            )
        return result

    def _semantic(self, evidence: Sequence[Evidence]) -> list[IndependentFault]:
        grouped: dict[tuple[str, str], list[Evidence]] = defaultdict(list)
        family_map = {
            "acl": "acl_misconfig",
            "route_policy": "route_policy_misconfig",
            "static_route": "static_route_misconfig",
            "bgp": "bgp_neighbor_misconfig",
        }
        for item in evidence:
            fault_type = family_map.get(str(item.metadata.get("semantic_family") or ""))
            device = item.entity_id.split(":", 1)[0]
            if fault_type:
                grouped[(fault_type, device)].append(item)
            if item.category in {"configured_static_route", "unexpected_next_hop", "observed_routing_entry"}:
                grouped[("static_route_misconfig", device)].append(item)

        result: list[IndependentFault] = []
        for (fault_type, device), items in grouped.items():
            keys = {self._key(item) for item in items}
            direct = any(
                item.category in {"configuration_difference", "configured_static_route", "unexpected_next_hop"}
                for item in items
            )
            consequence = any(
                item.category
                in {"route_presence", "observed_routing_entry", "interface_counter_delta", "bgp_neighbor_state"}
                for item in items
            )
            if not direct or not consequence or len(keys) < 2:
                continue
            result.append(
                IndependentFault(
                    fault_type=fault_type,
                    device=device or None,
                    interface=None,
                    link_id=None,
                    confidence=min(0.99, 0.85 + 0.03 * max(0, len(keys) - 2)),
                    evidence_ids=tuple(dict.fromkeys(item.evidence_id for item in items)),
                )
            )
        return result

    @staticmethod
    def _exact_links(item: Evidence) -> tuple[str, ...]:
        link_id = str(item.metadata.get("link_id") or "")
        if link_id:
            return (link_id,)
        if item.path_observation_confidence >= 1.0 and len(item.covered_links) == 1:
            return item.covered_links
        return ()

    @staticmethod
    def _size_failure(item: Evidence) -> bool:
        return isinstance(item.value, Mapping) and bool(item.value.get("size_dependent_failure"))

    @staticmethod
    def _mtu_difference(item: Evidence) -> bool:
        return (
            isinstance(item.value, Mapping) and item.value.get("field") == "mtu" and bool(item.value.get("different"))
        )

    @staticmethod
    def _strong_loss(item: Evidence) -> bool:
        try:
            rate = float(item.value)
        except (TypeError, ValueError):
            return False
        return rate >= 0.10 and (
            int(item.metadata.get("rounds") or 0) >= 2
            or int(item.metadata.get("sent") or 0) >= 20
            or item.source == "access_path_contrast"
        )

    @staticmethod
    def _latency_anomaly(item: Evidence) -> bool:
        return bool(
            item.metadata.get("category_anomaly")
            or item.metadata.get("absolute_anomaly")
            or item.metadata.get("relative_anomaly")
        )

    @staticmethod
    def _down(item: Evidence) -> bool:
        return item.value is False or str(item.value).lower() in {"down", "false", "0", "x"}

    @staticmethod
    def _transition(item: Evidence) -> bool:
        return isinstance(item.value, Mapping) and bool(item.value.get("temporal_transition"))

    @staticmethod
    def _key(item: Evidence) -> str:
        return item.independence_key or f"{item.source}:{item.probe_id or item.category}"

    @staticmethod
    def _contract(fault_type: str, items: Sequence[Evidence], keys: set[str]) -> bool:
        if fault_type == "packet_corruption":
            return any(item.origin is EvidenceOrigin.ACTIVE_PROBE for item in items)
        if len(keys) < 2:
            return False
        if fault_type == "mtu_mismatch":
            return any(MultiFaultAnalyzer._size_failure(item) for item in items) and any(
                MultiFaultAnalyzer._mtu_difference(item) for item in items
            )
        if fault_type == "high_latency":
            return any(item.metadata.get("fault_endpoint_device") for item in items)
        if fault_type == "link_down":
            devices = {item.entity_id.split(":", 1)[0] for item in items}
            return len(devices) >= 2 or any(item.category == "interface_admin_state" for item in items)
        return True

    @staticmethod
    def _endpoint(items: Sequence[Evidence], candidate: RankedInterfaceCandidate) -> tuple[str | None, str | None]:
        for item in items:
            device = str(item.metadata.get("fault_endpoint_device") or "")
            interface = str(item.metadata.get("fault_endpoint_interface") or "")
            if device and interface:
                return device, interface
            if item.entity_type == "interface" and ":" in item.entity_id:
                entity_device, entity_interface = item.entity_id.split(":", 1)
                if entity_device in {candidate.primary_device, candidate.peer_device}:
                    return entity_device, entity_interface
        return candidate.primary_device, candidate.primary_interface


__all__ = ["IndependentFault", "MultiFaultAnalyzer"]
