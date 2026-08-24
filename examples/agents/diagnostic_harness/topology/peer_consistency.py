"""Read-only peer interface collection and configuration comparison."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from ..evidence.cache import TTLToolCache
from ..models import Evidence, EvidenceOrigin, RankedInterfaceCandidate
from ..probes.base import ProbeBudget, invoke_tool, tool_error_evidence


class PeerConsistencyCollector:
    def __init__(self, *, ttl_seconds: float = 10.0, timeout_seconds: float = 15.0):
        self.ttl_seconds = ttl_seconds
        self.timeout_seconds = timeout_seconds

    async def collect(
        self,
        context: Any,
        *,
        candidates: list[RankedInterfaceCandidate],
        budget: ProbeBudget,
        cache: TTLToolCache,
        max_candidates: int = 8,
        evidence: Sequence[Evidence] = (),
    ) -> list[Evidence]:
        diagnosis_evidence = tuple(evidence)
        selected = candidates[:max_candidates]
        devices: list[str] = []
        for candidate in selected:
            for device in (candidate.primary_device, candidate.peer_device):
                if device not in devices:
                    devices.append(device)
        snapshots: dict[str, dict[str, Mapping[str, Any]]] = {}
        collected: list[Evidence] = []
        for device in devices:
            key = cache.key("get_device_interfaces", device=device, parameters={})
            payload = cache.get(key)
            if payload is None:
                invocation = await invoke_tool(
                    context,
                    "get_device_interfaces",
                    {"device": device},
                    budget=budget,
                    timeout_seconds=self.timeout_seconds,
                )
                if not invocation.success or invocation.data is None:
                    collected.append(
                        tool_error_evidence(
                            evidence_id=f"peer-interface-query-{device}",
                            probe_id="peer-consistency",
                            entity_id=device,
                            error=invocation.error or "interface inventory query failed",
                            source="get_device_interfaces",
                        )
                    )
                    if invocation.error and "budget exhausted" in invocation.error:
                        break
                    continue
                payload = invocation.data
                cache.set(key, payload, ttl_seconds=self.ttl_seconds)
            interfaces = payload.get("interfaces") if isinstance(payload, Mapping) else None
            if not isinstance(interfaces, list):
                continue
            snapshots[device] = {
                str(item.get("name")): item for item in interfaces if isinstance(item, Mapping) and item.get("name")
            }

        for candidate in selected:
            left = snapshots.get(candidate.primary_device, {}).get(candidate.primary_interface)
            right = snapshots.get(candidate.peer_device, {}).get(candidate.peer_interface)
            for device, interface, item in (
                (candidate.primary_device, candidate.primary_interface, left),
                (candidate.peer_device, candidate.peer_interface, right),
            ):
                if item is None:
                    continue
                collected.extend(self._state_evidence(device, interface, item, candidate.link_id))
            if left is None or right is None:
                continue
            left_mtu = _optional_int(left.get("mtu"))
            right_mtu = _optional_int(right.get("mtu"))
            if left_mtu is None or right_mtu is None or left_mtu == right_mtu:
                continue
            if not _mtu_difference_is_diagnostic(
                left_mtu,
                right_mtu,
                layer=candidate.layer,
                evidence=diagnosis_evidence,
            ):
                continue
            if left_mtu < right_mtu:
                device, interface, local, peer = (
                    candidate.primary_device,
                    candidate.primary_interface,
                    left_mtu,
                    right_mtu,
                )
            else:
                device, interface, local, peer = (
                    candidate.peer_device,
                    candidate.peer_interface,
                    right_mtu,
                    left_mtu,
                )
            collected.append(
                Evidence(
                    evidence_id=f"peer-mtu-difference-{candidate.link_id}",
                    entity_type="interface",
                    entity_id=f"{device}:{interface}",
                    category="configuration_difference",
                    value={
                        "different": True,
                        "field": "mtu",
                        "local_mtu": local,
                        "peer_mtu": peer,
                    },
                    source="get_device_interfaces",
                    timestamp=datetime.now(UTC),
                    reliability=1.0,
                    probe_id="peer-consistency",
                    origin=EvidenceOrigin.CONFIG_READ,
                    independence_key=f"tool:get_device_interfaces:{device}",
                    metadata={
                        "link_id": candidate.link_id,
                        "peer_consistency": True,
                        "link_layer": candidate.layer,
                        "threshold_consistent": _mtu_threshold_consistent(min(local, peer), diagnosis_evidence),
                    },
                )
            )
        return collected

    @staticmethod
    def _state_evidence(device: str, interface: str, item: Mapping[str, Any], link_id: str) -> list[Evidence]:
        now = datetime.now(UTC)
        return [
            Evidence(
                evidence_id=f"peer-admin-{device}-{interface}",
                entity_type="interface",
                entity_id=f"{device}:{interface}",
                category="interface_admin_state",
                value=item.get("admin"),
                source="get_device_interfaces",
                timestamp=now,
                reliability=1.0,
                probe_id="peer-consistency",
                origin=EvidenceOrigin.LIVE_TELEMETRY,
                independence_key=f"tool:get_device_interfaces:{device}",
                metadata={"link_id": link_id},
            ),
            Evidence(
                evidence_id=f"peer-oper-{device}-{interface}",
                entity_type="interface",
                entity_id=f"{device}:{interface}",
                category="interface_oper_state",
                value=item.get("oper"),
                source="get_device_interfaces",
                timestamp=now,
                reliability=1.0,
                probe_id="peer-consistency",
                origin=EvidenceOrigin.LIVE_TELEMETRY,
                independence_key=f"tool:get_device_interfaces:{device}",
                metadata={"link_id": link_id},
            ),
        ]


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _mtu_difference_is_diagnostic(
    left: int,
    right: int,
    *,
    layer: str,
    evidence: Sequence[Evidence],
) -> bool:
    """Reject role-normal access MTUs that cannot explain the sweep cutoff."""
    lower = min(left, right)
    # Linux/container endpoints may expose the topology link MTU (for example
    # 9232) while SONiC reports its IP MTU (commonly 9100).  Both are jumbo and
    # this small, role-specific delta is not an MTU fault.
    if layer == "access" and lower >= 9_000 and abs(left - right) <= 512:
        return False
    thresholds = _size_thresholds(evidence)
    if not thresholds:
        return True
    return _mtu_threshold_consistent(lower, evidence)


def _mtu_threshold_consistent(mtu: int, evidence: Sequence[Evidence]) -> bool:
    thresholds = _size_thresholds(evidence)
    if not thresholds:
        return True
    # ping payload excludes the 20-byte IPv4 and 8-byte ICMP headers.  Permit
    # a bounded tolerance for platform encapsulation and reported-IP-MTU
    # differences, but reject jumbo candidates for a 1400/1500-byte cutoff.
    payload_limit = int(mtu) - 28
    return any(success - 96 <= payload_limit <= failure + 96 for success, failure in thresholds)


def _size_thresholds(evidence: Sequence[Evidence]) -> list[tuple[int, int]]:
    result: list[tuple[int, int]] = []
    for item in evidence:
        if item.category != "packet_size_threshold" or not isinstance(item.value, Mapping):
            continue
        if not item.value.get("size_dependent_failure"):
            continue
        success = _optional_int(item.value.get("largest_successful_payload_size"))
        failure = _optional_int(item.value.get("smallest_failed_payload_size"))
        if success is not None and failure is not None and success <= failure:
            result.append((success, failure))
    return result


__all__ = ["PeerConsistencyCollector"]
