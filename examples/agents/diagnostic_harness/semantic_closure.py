"""Budgeted closure for base-agent runtime failures using public tools only."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from ipaddress import ip_address, ip_network
from typing import Any

from netopsbench.sdk.agents import DiagnosisResult

from .evidence.base_tools import explicit_route_policy_denies, parse_active_acl_drop
from .evidence.validator import can_plan_from, can_support_fault
from .models import DiagnosisView, Evidence, EvidenceOrigin, ProbeOutcome
from .normalization.fault_type import FaultTypeNormalizer
from .normalization.interface import InterfaceNameNormalizer, TopologyIndex
from .probes.base import ProbeBudget, invoke_tool, tool_error_evidence
from .topology.graph import TopologyGraph
from .topology.semantics import attachment_from_metadata

_STATIC_ROUTE_RE = re.compile(
    r"^\s*ip route\s+(?P<prefix>\S+)\s+(?P<nexthop>\S+)(?:\s+.*)?$",
    re.IGNORECASE | re.MULTILINE,
)
_ROUTE_POLICY_OBJECT_RE = re.compile(
    r"\b(?:route[- ]map|prefix[- ](?:list|filter)|network statement)\b",
    re.IGNORECASE,
)


def _route_policy_configuration_difference(config: str, prefix: str | None) -> str | None:
    """Validate a concrete prefix-level policy difference in live config.

    The mere presence of a route-map is normal on every fabric device and is
    not a configuration difference.  A closure needs either the claimed
    prefix to be absent from configuration or an explicit deny/reject rule for
    that exact prefix.
    """
    if not prefix:
        return None
    policy_context = bool(
        _ROUTE_POLICY_OBJECT_RE.search(config)
        or (
            re.search(r"^\s*router\s+bgp\b", config, re.IGNORECASE | re.MULTILINE)
            and re.search(r"^\s*network\s+\S+", config, re.IGNORECASE | re.MULTILINE)
        )
    )
    if not policy_context:
        return None
    normalized_prefix = str(prefix).strip()
    try:
        target = ip_network(normalized_prefix, strict=False)
    except ValueError:
        target = None

    def overlaps_target(candidate: str) -> bool:
        if target is None:
            return candidate == normalized_prefix
        try:
            network = ip_network(candidate, strict=False)
        except ValueError:
            return False
        return network.subnet_of(target) or target.subnet_of(network)

    configured = {match.group(1) for match in re.finditer(r"^\s*network\s+(\S+)", config, re.IGNORECASE | re.MULTILINE)}
    denied = explicit_route_policy_denies(config)
    if any(overlaps_target(candidate) for candidate in denied):
        return "explicit_prefix_deny"
    if not any(overlaps_target(candidate) for candidate in configured):
        return "missing_prefix_configuration"
    return None


def _route_policy_target_scope(
    context: Any,
    prefix: str,
    *,
    preferred_owner: str | None = None,
) -> tuple[str, str | None]:
    """Resolve a possibly inexact prefix hint to a public client address/owner.

    Agents often describe an affected client subnet as a host, /24, or /32
    while the live routing table contains a /30.  Querying the concrete public
    client address lets the device return its real longest-prefix match.  A
    missing network statement is admissible only on the attachment switch
    that owns that address; absence on unrelated nodes is expected.
    """
    try:
        hinted = ip_network(str(prefix).strip(), strict=False)
    except ValueError:
        return str(prefix), None
    try:
        graph = TopologyGraph.from_context(context)
    except (OSError, TypeError, ValueError):
        return str(prefix), None
    matches: list[tuple[str, str]] = []
    for address, device in graph.addresses.items():
        try:
            if ip_address(address) not in hinted:
                continue
        except ValueError:
            continue
        owner = graph.attachments.get(device)
        if owner:
            matches.append((address, owner))
    if preferred_owner:
        preferred_matches = [item for item in matches if item[1] == preferred_owner]
        if preferred_matches:
            matches = preferred_matches
    owners = {owner for _address, owner in matches}
    if len(owners) != 1 or not matches:
        return str(prefix), None
    return sorted(matches)[0][0], next(iter(owners))


def _rank_route_policy_prefixes(
    context: Any,
    evidence: Sequence[Evidence],
    *,
    preferred_owner: str | None,
    limit: int = 2,
) -> list[str]:
    """Rank policy targets by semantic specificity, not observation order.

    A troubleshooting model commonly checks several RIB destinations before
    describing the actual policy object.  Every absent RIB lookup is useful as
    a consequence, but it must not outrank a concrete configuration hint.
    Owner alignment and independent repetition break ties without depending on
    topology size, device names, or a provider's tool-call order.
    """
    ranked: dict[str, dict[str, Any]] = {}
    for item in evidence:
        if item.reliability <= 0 or item.metadata.get("semantic_family") != "route_policy":
            continue
        raw_prefix = item.metadata.get("prefix")
        if not raw_prefix and isinstance(item.value, Mapping):
            raw_prefix = item.value.get("prefix")
        if not raw_prefix:
            continue
        raw_prefix = str(raw_prefix).strip()
        try:
            prefix = str(ip_network(raw_prefix, strict=False))
        except ValueError:
            prefix = raw_prefix

        direct_configuration = bool(item.metadata.get("direct_configuration_evidence"))
        value = item.value if isinstance(item.value, Mapping) else {}
        explicit_difference = str(value.get("difference") or "") in {
            "explicit_prefix_deny",
            "missing_prefix_configuration",
        }
        priority = 10
        if item.category == "configuration_difference":
            priority += 50
        if direct_configuration:
            priority += 40
        if item.origin is EvidenceOrigin.CONFIG_READ:
            priority += 30
        if explicit_difference:
            priority += 20

        _query_target, owner = _route_policy_target_scope(
            context,
            prefix,
            preferred_owner=preferred_owner,
        )
        owner_match = int(bool(preferred_owner and owner == preferred_owner))
        entity_match = int(bool(preferred_owner and item.entity_id == preferred_owner))
        group = str(item.independence_key or item.probe_id or item.evidence_id)
        record = ranked.setdefault(
            prefix,
            {
                "priority": 0,
                "owner_match": 0,
                "entity_match": 0,
                "groups": set(),
            },
        )
        record["priority"] = max(int(record["priority"]), priority)
        record["owner_match"] = max(int(record["owner_match"]), owner_match)
        record["entity_match"] = max(int(record["entity_match"]), entity_match)
        record["groups"].add(group)

    ordered = sorted(
        ranked,
        key=lambda prefix: (
            -int(ranked[prefix]["priority"]),
            -int(ranked[prefix]["owner_match"]),
            -int(ranked[prefix]["entity_match"]),
            -len(ranked[prefix]["groups"]),
            prefix,
        ),
    )
    return ordered[: max(0, int(limit))]


def _connected_prefix_for_target(config: str, target: str) -> str | None:
    """Return the most-specific directly configured interface network."""
    candidates: list[tuple[int, str]] = []
    raw_target = str(target).strip()
    try:
        target_network = ip_network(raw_target, strict=False)
    except ValueError:
        return None
    for match in re.finditer(r"^\s*ip\s+address\s+(\S+)", config, re.IGNORECASE | re.MULTILINE):
        value = match.group(1)
        try:
            interface_network = ip_network(value, strict=False)
        except ValueError:
            continue
        if "/" in raw_target:
            overlaps = target_network.subnet_of(interface_network) or interface_network.subnet_of(target_network)
        else:
            # A mask-less network address is commonly emitted by a provider as
            # shorthand for the connected subnet. Match either an address in
            # the network or the network address itself.
            overlaps = ip_address(raw_target) in interface_network
        if overlaps:
            candidates.append((interface_network.prefixlen, str(interface_network)))
    return max(candidates, default=(0, None))[1]


def _route_payload_proves_owner(payload: Mapping[str, Any], target: str) -> bool:
    """Return whether live RIB data directly binds ``target`` to this device.

    Learned BGP/OSPF routes show reachability, not ownership.  Only connected,
    local, kernel-link, or explicitly direct routes are accepted, so an
    incomplete topology cannot make the first queried fabric node look like
    the prefix owner.
    """
    direct_protocols = {"connected", "local", "direct", "kernel", "c", "l"}
    routes = payload.get("routes")
    for route in routes if isinstance(routes, list) else ():
        if not isinstance(route, Mapping) or not _route_covers_destination(route, target):
            continue
        protocol = (
            str(
                route.get("protocol")
                or route.get("source_protocol")
                or route.get("route_type")
                or route.get("type")
                or ""
            )
            .strip()
            .lower()
        )
        scope = str(route.get("scope") or "").strip().lower()
        if protocol in direct_protocols or scope in {"host", "link"}:
            return True
    return False


@dataclass(frozen=True)
class SemanticClosureResult:
    result: DiagnosisResult | None
    outcome: ProbeOutcome
    family: str | None = None


def _candidate_devices(
    evidence: Sequence[Evidence],
    topology: TopologyIndex,
    *,
    initial_device: str | None,
    limit: int = 3,
) -> list[str]:
    counts: Counter[str] = Counter()
    if initial_device and topology.resolve_device(initial_device):
        counts[initial_device] += 10_000
    for item in evidence:
        if item.reliability <= 0 or item.category not in {
            "packet_loss_rate",
            "route_presence",
            "configured_static_route",
            "observed_routing_entry",
            "configuration_difference",
        }:
            continue
        if item.category in {
            "route_presence",
            "configured_static_route",
            "observed_routing_entry",
            "configuration_difference",
        }:
            device = topology.resolve_device(item.entity_id)
            if device:
                counts[device] += 5_000
            continue
        if item.category == "packet_loss_rate":
            try:
                if float(item.value) <= 0:
                    continue
            except (TypeError, ValueError):
                continue
        for side in ("source", "destination"):
            device = attachment_from_metadata(item.metadata, side)
            if device and topology.resolve_device(str(device)):
                counts[str(device)] += 1
    if not counts:
        counts.update(topology.attachment_devices)
    if not counts:
        counts.update(topology.routing_devices())
    return [device for device, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]]


def _concentrated_route_queries(
    evidence: Sequence[Evidence],
    topology: TopologyIndex,
    *,
    limit: int = 2,
    allow_multileaf: bool = False,
) -> list[tuple[str, str, Evidence]]:
    """Find bounded source-device/destination-IP route checks.

    A discard route commonly appears as symmetric reachability loss between
    two attachment domains while their individual uplinks remain healthy. Query the
    public routing table from each endpoint towards its peer IP before treating
    this pattern as random link loss.  Requiring at least two strong rows and
    exactly two endpoint attachments keeps broad many-to-one outages and noisy
    loss matrices out of the normal semantic check. An adaptive, still bounded
    escalation may cover more attachments, but only after the base result is marked
    unreliable and the normal route-query set is insufficient.
    """
    rows: list[Evidence] = []
    endpoint_attachments: set[str] = set()
    for item in evidence:
        if item.category != "packet_loss_rate" or item.reliability <= 0:
            continue
        try:
            if float(item.value) < 0.80:
                continue
        except (TypeError, ValueError):
            continue
        source_attachment = attachment_from_metadata(item.metadata, "source") or ""
        destination_attachment = attachment_from_metadata(item.metadata, "destination") or ""
        if not source_attachment or not destination_attachment or source_attachment == destination_attachment:
            continue
        if not topology.resolve_device(source_attachment) or not topology.resolve_device(destination_attachment):
            continue
        endpoint_attachments.update((source_attachment, destination_attachment))
        rows.append(item)
    if len(rows) < 2 or len(endpoint_attachments) < 2:
        rows = []
    elif not allow_multileaf and len(endpoint_attachments) != 2:
        rows = []

    counts: Counter[tuple[str, str]] = Counter()
    support: dict[tuple[str, str], Evidence] = {}
    for item in rows:
        candidates = (
            (attachment_from_metadata(item.metadata, "source") or "", str(item.metadata.get("dst_ip") or "")),
            (attachment_from_metadata(item.metadata, "destination") or "", str(item.metadata.get("src_ip") or "")),
        )
        for device, destination in candidates:
            if not device or not destination:
                continue
            key = (device, destination)
            counts[key] += 1
            support.setdefault(key, item)
    ranked = sorted(counts, key=lambda item: (-counts[item], item[0], item[1]))
    if ranked:
        return [(device, destination, support[(device, destination)]) for device, destination in ranked[:limit]]

    if not allow_multileaf:
        return []

    # Under ECMP, a discard route on one spine produces persistent partial
    # loss from many source leaves to the same endpoint rather than 80-100%
    # loss on any one flow. Query that destination on each fabric member. A
    # clean contrast simply falls through to packet-loss probing; a selected
    # discard route is direct semantic evidence.
    endpoint_support: dict[str, tuple[set[str], Evidence, str]] = {}
    for item in evidence:
        if item.category != "packet_loss_rate" or item.reliability <= 0:
            continue
        try:
            loss = float(item.value)
        except (TypeError, ValueError):
            continue
        if not 0.10 <= loss < 0.80:
            continue
        for address_key, peer_key, endpoint_leaf_key in (
            ("dst_ip", "source", "destination"),
            ("src_ip", "destination", "source"),
        ):
            address = str(item.metadata.get(address_key) or "")
            peer_leaf = attachment_from_metadata(item.metadata, peer_key) or ""
            endpoint_leaf = attachment_from_metadata(item.metadata, endpoint_leaf_key) or ""
            if not address or not peer_leaf:
                continue
            peers, _support, _leaf = endpoint_support.setdefault(address, (set(), item, endpoint_leaf))
            peers.add(peer_leaf)
    concentrated = [
        (address, peers, support_item, endpoint_leaf)
        for address, (peers, support_item, endpoint_leaf) in endpoint_support.items()
        if len(peers) >= 3
    ]
    if not concentrated:
        return []
    concentrated.sort(key=lambda item: (-len(item[1]), item[0]))
    destination, _peers, consequence, endpoint_leaf = concentrated[0]
    # A selected discard route may affect either direction of a symmetric
    # anomaly.  Preserve the bounded public-observation candidates so the
    # closure can inspect all of them from one routing-table snapshot per
    # fabric member.  This avoids spending one tool call per destination and
    # does not expose scenario or injection metadata.
    consequence = replace(
        consequence,
        metadata={
            **consequence.metadata,
            "route_candidate_destinations": tuple(item[0] for item in concentrated[:8]),
        },
    )
    resolved_endpoint = topology.resolve_device(endpoint_leaf)
    devices = list(topology.routing_scope(resolved_endpoint, limit=limit))
    return [(device, destination, consequence) for device in devices[:limit]]


def _route_covers_destination(route: Mapping[str, Any], destination: str) -> bool:
    """Whether a structured route is an exact or longest-prefix match."""
    prefix = str(route.get("prefix") or "").strip()
    if not prefix or not destination:
        return False
    try:
        route_network = ip_network(prefix, strict=False)
        if "/" in destination:
            return ip_network(destination, strict=False).subnet_of(route_network)
        return ip_address(destination) in route_network
    except ValueError:
        return prefix == destination


def _route_prefix_length(route: Mapping[str, Any]) -> int:
    try:
        return ip_network(str(route.get("prefix") or ""), strict=False).prefixlen
    except ValueError:
        return -1


def _route_consequence_for_destination(
    evidence: Sequence[Evidence],
    destination: str,
    *,
    fallback: Evidence,
) -> Evidence:
    """Bind a route finding to an independently observed affected endpoint."""
    for item in evidence:
        if item.category != "packet_loss_rate" or item.reliability <= 0:
            continue
        observed = {str(item.metadata.get(key) or "") for key in ("src_ip", "dst_ip")}
        if destination in observed:
            return item
    return fallback


def _route_payload_requires_fallback(payload: Mapping[str, Any]) -> bool:
    """Whether an exact query produced no structured route to evaluate."""
    routes = payload.get("routes")
    return not isinstance(routes, Sequence) or isinstance(routes, (str, bytes)) or not routes


def _selected_discard_route(
    payload: Mapping[str, Any],
    *,
    destination: str | None = None,
) -> Mapping[str, Any] | None:
    candidates: list[Mapping[str, Any]] = []
    for item in payload.get("routes", []):
        if not isinstance(item, Mapping) or item.get("selected") is False:
            continue
        if destination and not _route_covers_destination(item, destination):
            continue
        tokens = {str(item.get(key) or "").strip().lower() for key in ("protocol", "type", "next_hop", "nexthop")}
        nexthops = item.get("nexthops")
        if isinstance(nexthops, Sequence) and not isinstance(nexthops, (str, bytes)):
            for nexthop in nexthops:
                if isinstance(nexthop, Mapping):
                    tokens.update(str(value or "").strip().lower() for value in nexthop.values())
                else:
                    tokens.add(str(nexthop or "").strip().lower())
        if bool(item.get("is_discard")) or tokens.intersection({"null0", "blackhole", "discard"}):
            candidates.append(item)
    return max(candidates, key=_route_prefix_length, default=None)


def _topology_role_for_address(context: Any, address: str) -> tuple[str | None, str | None]:
    """Resolve a public topology address to ``(device, role)``."""
    topology = getattr(context, "topology", {}) or {}
    devices = topology.get("devices") if isinstance(topology, Mapping) else None
    rows: list[Mapping[str, Any]] = []
    if isinstance(devices, list):
        rows = [item for item in devices if isinstance(item, Mapping)]
    elif isinstance(devices, Mapping):
        for group, entries in devices.items():
            if not isinstance(entries, list):
                continue
            for item in entries:
                if isinstance(item, Mapping):
                    rows.append({**item, "role": item.get("role") or str(group).rstrip("s")})
    normalized = str(address or "").split("/", 1)[0]
    for item in rows:
        addresses = (item.get("data_ip"), item.get("ipv4"), item.get("ip"), item.get("mgmt_ip"))
        if normalized in {str(value or "").split("/", 1)[0] for value in addresses}:
            return str(item.get("name") or "") or None, str(item.get("role") or "") or None
    # The agent-facing projection can be deliberately minimal.  The harness
    # may still use the public runtime topology manifest supplied in context
    # metadata, which is the same inventory used by topology/path analysis.
    try:
        graph = TopologyGraph.from_context(context)
        device = graph.resolve_node(normalized)
        if device:
            return device, graph.roles.get(device)
    except (OSError, TypeError, ValueError):
        pass
    return None, None


def _selected_static_route(payload: Mapping[str, Any], *, prefix: str) -> Mapping[str, Any] | None:
    """Find the selected static route across real structured parser shapes."""
    candidates = [
        row
        for row in payload.get("routes", [])
        if isinstance(row, Mapping) and _route_covers_destination(row, prefix) and row.get("selected") is not False
    ]
    for row in sorted(candidates, key=_route_prefix_length, reverse=True):
        protocol = str(row.get("protocol") or row.get("type") or "").lower()
        if protocol == "static" or bool(row.get("is_discard")) or int(row.get("admin_distance") or -1) == 1:
            return row
    return None


def _static_route_peer_role(
    topology: TopologyIndex,
    *,
    device: str,
    route: Mapping[str, Any],
) -> tuple[str | None, str | None, str | None]:
    """Resolve a selected route's egress interface to its physical peer.

    Route parsers often know the resolved egress even when the agent-facing
    topology does not expose the next-hop IP.  Using the canonical topology
    mapping is more reliable than parsing interface names or guessing address
    ownership, and remains independent of scenario metadata.
    """
    interfaces: list[str] = []
    direct = route.get("interface") or route.get("ifname") or route.get("outgoing_interface")
    if direct:
        interfaces.append(str(direct))
    nexthops = route.get("nexthops")
    if isinstance(nexthops, Sequence) and not isinstance(nexthops, (str, bytes)):
        for item in nexthops:
            if not isinstance(item, Mapping):
                continue
            interface = item.get("interface") or item.get("ifname") or item.get("outgoing_interface")
            if interface:
                interfaces.append(str(interface))
    for interface in interfaces:
        endpoint = topology.resolve_interface(device, interface)
        if endpoint is None:
            continue
        peer = topology.peer(device, endpoint.canonical_interface)
        if peer is not None:
            return peer.device, topology.devices.get(peer.device), endpoint.canonical_interface
    return None, None, None


def _targeted_static_route_evidence(
    context: Any,
    topology: TopologyIndex,
    *,
    device: str,
    destination: str,
    route_payload: Mapping[str, Any],
    config: str,
    evidence_suffix: str,
) -> tuple[str, tuple[Evidence, Evidence]] | None:
    """Confirm a faulty selected static route from two public-tool views.

    A targeted route lookup is already performed for concentrated failures.
    Reusing that structured snapshot before wider semantic scans prevents the
    stage budget from starving the decisive config+RIB comparison.  The base
    label is used only to schedule this check; submission still requires an
    independently observed selected route and its matching live config line.
    """
    selected = _selected_static_route(route_payload, prefix=destination)
    if selected is None:
        return None
    selected_prefix = str(selected.get("prefix") or destination).strip()
    configured = [
        match
        for match in _STATIC_ROUTE_RE.finditer(config)
        if _route_covers_destination({"prefix": match.group("prefix")}, destination)
    ]
    if selected_prefix:
        exact = [match for match in configured if match.group("prefix") == selected_prefix]
        if exact:
            configured = exact
    if not configured:
        return None
    match = max(
        configured,
        key=lambda item: _route_prefix_length({"prefix": item.group("prefix")}),
    )
    prefix = match.group("prefix")
    nexthop = match.group("nexthop")
    is_discard = bool(selected.get("is_discard")) or nexthop.lower().strip("'\"") in {
        "null0",
        "blackhole",
        "discard",
    }
    unresolved = not selected.get("nexthops") and not is_discard
    next_hop_device, next_hop_role = _topology_role_for_address(context, nexthop)
    peer_device, peer_role, egress_interface = _static_route_peer_role(
        topology,
        device=device,
        route=selected,
    )
    if next_hop_device is None and peer_device is not None:
        next_hop_device, next_hop_role = peer_device, peer_role
    invalid_next_hop_role = next_hop_role == "client" or peer_role == "client"
    if not is_discard and not unresolved and not invalid_next_hop_role:
        return None

    config_evidence = Evidence(
        evidence_id=f"semantic-targeted-static-{evidence_suffix}-config",
        entity_type="device",
        entity_id=device,
        category="configured_static_route",
        value={"prefix": prefix, "next_hop": nexthop},
        source="get_device_config",
        timestamp=datetime.now(UTC),
        reliability=1.0,
        probe_id="semantic-runtime-closure",
        origin=EvidenceOrigin.CONFIG_READ,
        independence_key=f"tool:get_device_config:{device}",
        metadata={
            "semantic_family": "static_route",
            "prefix": prefix,
            "direct_configuration_evidence": True,
        },
    )
    route_evidence = Evidence(
        evidence_id=f"semantic-targeted-static-{evidence_suffix}-route",
        entity_type="device",
        entity_id=device,
        category="route_presence",
        value={
            "prefix": prefix,
            "protocol": "static",
            "selected": selected.get("selected"),
            "resolved_nexthops": len(selected.get("nexthops") or []),
            "is_discard": is_discard,
            "configured_next_hop": nexthop,
            "next_hop_device": next_hop_device,
            "next_hop_role": next_hop_role,
            "invalid_next_hop_role": invalid_next_hop_role,
            "egress_interface": egress_interface,
        },
        source="get_route_table",
        timestamp=datetime.now(UTC),
        reliability=1.0,
        probe_id="semantic-runtime-closure",
        origin=EvidenceOrigin.LIVE_TELEMETRY,
        independence_key=f"tool:get_route_table:{device}:{destination}",
        metadata={
            "semantic_family": "static_route",
            "prefix": prefix,
            "query_target": destination,
            "reused_targeted_route_snapshot": True,
        },
    )
    canonical = "blackhole_route" if is_discard else "static_route_misconfig"
    return canonical, (config_evidence, route_evidence)


def _build_result(
    base: DiagnosisResult,
    *,
    fault_type: str,
    device: str,
    interface: str | None,
    evidence: Sequence[Evidence],
    confidence: float,
) -> DiagnosisResult:
    findings = dict(base.findings or {})
    findings["fault_type"] = fault_type
    findings["location"] = {"device": device, "interface": interface}
    findings["evidence"] = [
        f"{item.source}: {item.category} on {item.entity_id}" for item in evidence if item.reliability > 0
    ]
    metadata = dict(base.metadata or {})
    harness = dict(metadata.get("diagnostic_harness") or {})
    harness["hard_path_status"] = "semantic_runtime_closure"
    harness["semantic_closure"] = {
        "fault_type": fault_type,
        "evidence_ids": [item.evidence_id for item in evidence if item.reliability > 0],
        "public_tools_only": True,
    }
    metadata["diagnostic_harness"] = harness
    return replace(
        base,
        verdict="fault_detected",
        findings=findings,
        confidence=confidence,
        reasoning=(
            f"Deterministic public-tool inspection identified {fault_type} on "
            f"{device}{':' + interface if interface else ''}."
        ),
        metadata=metadata,
    )


class SemanticRuntimeClosure:
    """Inspect ACL and static-route semantics after an incomplete base run."""

    def __init__(self, *, timeout_seconds: float = 35.0, max_candidate_devices: int = 3):
        self.timeout_seconds = timeout_seconds
        self.max_candidate_devices = max_candidate_devices
        normalizer = FaultTypeNormalizer()
        self.acl_fault_type = normalizer.normalize("acl_misconfiguration").value
        self.static_route_fault_type = normalizer.normalize("static_route_misconfiguration").value
        self.blackhole_fault_type = normalizer.normalize("blackhole_route").value

    async def inspect(
        self,
        context: Any,
        *,
        base_result: DiagnosisResult,
        topology: TopologyIndex,
        evidence: Sequence[Evidence],
        budget: ProbeBudget,
        requested_family: str | None,
        max_candidate_devices: int | None = None,
        max_targeted_route_queries: int = 2,
        allow_multileaf_route_queries: bool = False,
    ) -> SemanticClosureResult:
        location = DiagnosisView.from_result(base_result).location
        candidates = _candidate_devices(
            evidence,
            topology,
            initial_device=location.get("device"),
            limit=max_candidate_devices or self.max_candidate_devices,
        )
        collected: list[Evidence] = []
        errors: list[Evidence] = []
        acl_checked: set[str] = set()
        device_configs: dict[str, str] = {}

        if requested_family == "runtime_semantic":
            queries = list(
                _concentrated_route_queries(
                    evidence,
                    topology,
                    limit=max(0, int(max_targeted_route_queries)),
                    allow_multileaf=allow_multileaf_route_queries,
                )
            )
            # Runtime failures often leave several semantic families open. Do
            # one cheap breadth discriminator on each affected endpoint
            # before route fallbacks can consume the whole stage budget.  A
            # positive ACL result contains both configuration and dataplane
            # counter evidence; a negative result is only a planning outcome
            # and never becomes healthy/fault evidence.
            acl_capacity = max(0, budget.remaining_stage_tool_calls - len(queries))
            max_acl_candidates = 2 if len(queries) > 2 else 1
            acl_candidates = candidates[: min(max_acl_candidates, acl_capacity)]
            for device in acl_candidates:
                if not budget.remaining_stage_tool_calls:
                    break
                acl_checked.add(device)
                acl = await invoke_tool(
                    context,
                    "get_device_acl",
                    {"device": device, "view": "summary", "max_lines": 300},
                    budget=budget,
                    timeout_seconds=self.timeout_seconds,
                )
                if not acl.success or acl.data is None:
                    errors.append(
                        tool_error_evidence(
                            evidence_id=f"semantic-acl-{device}-error",
                            probe_id="semantic-runtime-closure",
                            entity_id=device,
                            error=acl.error or "get_device_acl failed",
                            source="get_device_acl",
                        )
                    )
                else:
                    resolved = self._acl_result(base_result, topology, device, acl.data)
                    collected.extend(resolved[1])
                    if resolved[0] is not None:
                        return SemanticClosureResult(
                            resolved[0],
                            self._outcome(budget, (*collected, *errors), "acl_misconfig"),
                            "acl",
                        )

            def close_discard_route(
                *,
                index: int,
                device: str,
                destination: str,
                consequence: Evidence,
                selected: Mapping[str, Any],
            ) -> SemanticClosureResult:
                route_prefix = str(selected.get("prefix") or destination)
                route_evidence = Evidence(
                    evidence_id=f"semantic-targeted-route-{index}",
                    entity_type="device",
                    entity_id=device,
                    category="route_presence",
                    value={
                        "destination": destination,
                        "prefix": route_prefix,
                        "protocol": selected.get("protocol"),
                        "selected": selected.get("selected"),
                        "is_discard": True,
                    },
                    source="get_route_table",
                    timestamp=datetime.now(UTC),
                    reliability=1.0,
                    probe_id="semantic-runtime-closure",
                    origin=EvidenceOrigin.LIVE_TELEMETRY,
                    independence_key=f"tool:get_route_table:{device}:{destination}",
                    metadata={
                        "semantic_family": "static_route",
                        "direct_route_evidence": True,
                        "destination": destination,
                        "prefix": route_prefix,
                    },
                )
                collected.append(route_evidence)
                direct = (route_evidence, consequence)
                result = _build_result(
                    base_result,
                    fault_type=str(self.blackhole_fault_type),
                    device=device,
                    interface=None,
                    evidence=direct,
                    confidence=0.95,
                )
                outcome = self._outcome(budget, (*collected, *errors), "blackhole_route")
                return SemanticClosureResult(result, outcome, "static_route")

            # Before broad ACL/config enumeration, use at most two targeted
            # route lookups derived solely from public anomalous flow endpoints.
            # This preserves the unchanged semantic/tool budget and prevents a
            # recursion failure from turning a selected discard route into a
            # packet-loss hypothesis merely because generic scans ran first.
            fallback_queries: list[tuple[int, str, str, Evidence]] = []
            # First give every high-recall candidate one exact lookup.  A
            # per-candidate full-table fallback used to consume two calls on
            # the first empty result and starve later candidates on Large
            # topologies.  Fallbacks now run only after the breadth pass.
            for index, (device, destination, consequence) in enumerate(queries, start=1):
                route_destinations = tuple(consequence.metadata.get("route_candidate_destinations") or ())
                full_table_scan = bool(route_destinations)
                route = await invoke_tool(
                    context,
                    "get_route_table",
                    (
                        {"device": device, "format": "structured", "max_routes": 500}
                        if full_table_scan
                        else {"device": device, "prefix": destination, "format": "structured", "max_routes": 20}
                    ),
                    budget=budget,
                    timeout_seconds=self.timeout_seconds,
                )
                if not route.success or route.data is None:
                    errors.append(
                        tool_error_evidence(
                            evidence_id=f"semantic-targeted-route-{index}-error",
                            probe_id="semantic-runtime-closure",
                            entity_id=device,
                            error=route.error or "get_route_table failed",
                            source="get_route_table",
                        )
                    )
                    continue
                selected_destination = destination
                selected = None
                for candidate_destination in route_destinations or (destination,):
                    selected = _selected_discard_route(route.data, destination=candidate_destination)
                    if selected is not None:
                        selected_destination = candidate_destination
                        break
                if selected is None and not full_table_scan and _route_payload_requires_fallback(route.data):
                    fallback_queries.append((index, device, destination, consequence))
                if selected is None:
                    # A non-discard selected static route can still be the
                    # causal fault (unresolved next hop, client next hop, or
                    # forwarding loop).  When the base identified this same
                    # failure-domain device as route-like, use that label only
                    # to schedule one live config confirmation.  The final
                    # result still requires both independent public-tool
                    # observations and never trusts the prose by itself.
                    base_view = DiagnosisView.from_result(base_result)
                    initial_device = topology.resolve_device(base_view.device)
                    route_like_base = base_view.fault_type in {
                        "blackhole_route",
                        "static_route_misconfig",
                    }
                    if (
                        route_like_base
                        and initial_device == device
                        and _selected_static_route(route.data, prefix=destination) is not None
                    ):
                        config_call = await invoke_tool(
                            context,
                            "get_device_config",
                            {"device": device, "max_lines": 500},
                            budget=budget,
                            timeout_seconds=self.timeout_seconds,
                        )
                        if config_call.success and config_call.data is not None:
                            config = str(config_call.data.get("config") or "")
                            device_configs[device] = config
                            static_fault = _targeted_static_route_evidence(
                                context,
                                topology,
                                device=device,
                                destination=destination,
                                route_payload=route.data,
                                config=config,
                                evidence_suffix=str(index),
                            )
                            if static_fault is not None:
                                canonical, direct = static_fault
                                collected.extend(direct)
                                result = _build_result(
                                    base_result,
                                    fault_type=(
                                        str(self.blackhole_fault_type)
                                        if canonical == "blackhole_route"
                                        else str(self.static_route_fault_type)
                                    ),
                                    device=device,
                                    interface=None,
                                    evidence=direct,
                                    confidence=0.95,
                                )
                                return SemanticClosureResult(
                                    result,
                                    self._outcome(budget, (*collected, *errors), canonical),
                                    "static_route",
                                )
                        elif config_call.error:
                            errors.append(
                                tool_error_evidence(
                                    evidence_id=f"semantic-targeted-static-{index}-config-error",
                                    probe_id="semantic-runtime-closure",
                                    entity_id=device,
                                    error=config_call.error,
                                    source="get_device_config",
                                )
                            )
                    continue
                return close_discard_route(
                    index=index,
                    device=device,
                    destination=selected_destination,
                    consequence=_route_consequence_for_destination(
                        evidence,
                        selected_destination,
                        fallback=consequence,
                    ),
                    selected=selected,
                )

            for index, device, destination, consequence in fallback_queries:
                if not budget.remaining_stage_tool_calls:
                    break
                fallback = await invoke_tool(
                    context,
                    "get_route_table",
                    {"device": device, "format": "structured", "max_routes": 500},
                    budget=budget,
                    timeout_seconds=self.timeout_seconds,
                )
                if not fallback.success or fallback.data is None:
                    continue
                selected = _selected_discard_route(fallback.data, destination=destination)
                if selected is not None:
                    return close_discard_route(
                        index=index,
                        device=device,
                        destination=destination,
                        consequence=consequence,
                        selected=selected,
                    )

        # Base-agent prose is not trusted as a final answer, but explicit
        # public route/config observations are sufficient to seed this branch.
        # This closes the LOW_EVIDENCE -> static-route path without consuming
        # the packet-loss probe budget.
        if requested_family == "static_route":
            # Structured results from successful base-agent tool calls are
            # planning seeds, not proof.  Re-query the exact device/prefix
            # through two independent public tools before submitting.  This
            # avoids broad, topology-size-dependent scans while keeping an
            # incorrect base diagnosis unable to pass the gate by itself.
            planning_seeds: list[tuple[str, str]] = []
            for item in evidence:
                if not can_plan_from(item) or item.origin is EvidenceOrigin.BASE_CLAIM:
                    continue
                if item.category not in {"configured_static_route", "observed_routing_entry"}:
                    continue
                semantic_family = str(item.metadata.get("semantic_family") or "")
                value = item.value if isinstance(item.value, Mapping) else {}
                protocol = str(value.get("protocol") or "").lower()
                if (
                    item.category != "configured_static_route"
                    and semantic_family != "static_route"
                    and protocol != "static"
                    and not value.get("is_discard")
                ):
                    continue
                device = topology.resolve_device(item.entity_id)
                prefix = str(value.get("prefix") or item.metadata.get("prefix") or "").strip()
                seed = (device, prefix) if device and prefix else None
                if seed and seed not in planning_seeds:
                    planning_seeds.append(seed)

            for seed_index, (device, prefix) in enumerate(planning_seeds[:2], start=1):
                config_call = await invoke_tool(
                    context,
                    "get_device_config",
                    {"device": device, "max_lines": 500},
                    budget=budget,
                    timeout_seconds=self.timeout_seconds,
                )
                if not config_call.success or config_call.data is None:
                    errors.append(
                        tool_error_evidence(
                            evidence_id=f"semantic-static-seed-{seed_index}-config-error",
                            probe_id="semantic-runtime-closure",
                            entity_id=device,
                            error=config_call.error or "get_device_config failed",
                            source="get_device_config",
                        )
                    )
                    continue
                config = str(config_call.data.get("config") or "")
                configured = next(
                    (match for match in _STATIC_ROUTE_RE.finditer(config) if match.group("prefix") == prefix),
                    None,
                )
                if configured is None:
                    continue
                nexthop = configured.group("nexthop")
                config_evidence = Evidence(
                    evidence_id=f"semantic-static-seed-{seed_index}-config",
                    entity_type="device",
                    entity_id=device,
                    category="configured_static_route",
                    value={"prefix": prefix, "next_hop": nexthop},
                    source="get_device_config",
                    timestamp=datetime.now(UTC),
                    reliability=1.0,
                    probe_id="semantic-runtime-closure",
                    origin=EvidenceOrigin.CONFIG_READ,
                    independence_key=f"tool:get_device_config:{device}",
                    metadata={
                        "semantic_family": "static_route",
                        "prefix": prefix,
                        "direct_configuration_evidence": True,
                    },
                )
                route_call = await invoke_tool(
                    context,
                    "get_route_table",
                    {"device": device, "prefix": prefix, "format": "structured", "max_routes": 20},
                    budget=budget,
                    timeout_seconds=self.timeout_seconds,
                )
                if not route_call.success or route_call.data is None:
                    errors.append(
                        tool_error_evidence(
                            evidence_id=f"semantic-static-seed-{seed_index}-route-error",
                            probe_id="semantic-runtime-closure",
                            entity_id=device,
                            error=route_call.error or "get_route_table failed",
                            source="get_route_table",
                        )
                    )
                    continue
                selected_static = _selected_static_route(route_call.data, prefix=prefix)
                if (
                    selected_static is None
                    and _route_payload_requires_fallback(route_call.data)
                    and budget.remaining_stage_tool_calls
                ):
                    fallback = await invoke_tool(
                        context,
                        "get_route_table",
                        {"device": device, "format": "structured", "max_routes": 500},
                        budget=budget,
                        timeout_seconds=self.timeout_seconds,
                    )
                    if fallback.success and fallback.data is not None:
                        selected_static = _selected_static_route(fallback.data, prefix=prefix)
                if selected_static is None:
                    continue
                is_discard = bool(selected_static.get("is_discard")) or nexthop.lower() in {
                    "null0",
                    "blackhole",
                    "discard",
                }
                unresolved = not selected_static.get("nexthops") and not is_discard
                next_hop_device, next_hop_role = _topology_role_for_address(context, nexthop)
                peer_device, peer_role, egress_interface = _static_route_peer_role(
                    topology,
                    device=device,
                    route=selected_static,
                )
                if next_hop_device is None and peer_device is not None:
                    next_hop_device, next_hop_role = peer_device, peer_role
                invalid_next_hop_role = next_hop_role == "client" or peer_role == "client"
                if not is_discard and not unresolved and not invalid_next_hop_role:
                    continue
                route_evidence = Evidence(
                    evidence_id=f"semantic-static-seed-{seed_index}-route",
                    entity_type="device",
                    entity_id=device,
                    category="route_presence",
                    value={
                        "prefix": prefix,
                        "protocol": "static",
                        "selected": selected_static.get("selected"),
                        "resolved_nexthops": len(selected_static.get("nexthops") or []),
                        "is_discard": is_discard,
                        "configured_next_hop": nexthop,
                        "next_hop_device": next_hop_device,
                        "next_hop_role": next_hop_role,
                        "invalid_next_hop_role": invalid_next_hop_role,
                        "egress_interface": egress_interface,
                    },
                    source="get_route_table",
                    timestamp=datetime.now(UTC),
                    reliability=1.0,
                    probe_id="semantic-runtime-closure",
                    origin=EvidenceOrigin.LIVE_TELEMETRY,
                    independence_key=f"tool:get_route_table:{device}:{prefix}",
                    metadata={"semantic_family": "static_route", "prefix": prefix},
                )
                direct = (config_evidence, route_evidence)
                collected.extend(direct)
                result = _build_result(
                    base_result,
                    fault_type=str(self.blackhole_fault_type if is_discard else self.static_route_fault_type),
                    device=device,
                    interface=None,
                    evidence=direct,
                    confidence=0.95,
                )
                canonical = "blackhole_route" if is_discard else "static_route_misconfig"
                return SemanticClosureResult(
                    result,
                    self._outcome(budget, (*collected, *errors), canonical),
                    "static_route",
                )

            direct = [
                item
                for item in evidence
                if can_support_fault(item)
                and item.origin is not EvidenceOrigin.BASE_CLAIM
                and item.category in {"configured_static_route", "unexpected_next_hop", "observed_routing_entry"}
            ]
            has_difference = any(item.category == "unexpected_next_hop" for item in direct)
            has_route = any(item.category in {"configured_static_route", "observed_routing_entry"} for item in direct)
            if has_difference and has_route:
                device = str(
                    location.get("device")
                    or next((item.entity_id for item in direct if item.entity_id != "unknown-device"), "")
                )
                if device:
                    is_blackhole = any(
                        isinstance(item.value, Mapping)
                        and str(item.value.get("next_hop") or item.value.get("configured_next_hop") or "")
                        .lower()
                        .rstrip("'\"")
                        in {"null0", "blackhole", "discard"}
                        for item in direct
                    )
                    fault_type = self.blackhole_fault_type if is_blackhole else self.static_route_fault_type
                    result = _build_result(
                        base_result,
                        fault_type=str(fault_type),
                        device=device,
                        interface=None,
                        evidence=direct,
                        confidence=0.90,
                    )
                    outcome = self._outcome(
                        budget,
                        direct,
                        "blackhole_route" if is_blackhole else "static_route_misconfig",
                    )
                    return SemanticClosureResult(result, outcome, "static_route")

        if requested_family == "route_policy":
            # LOW_EVIDENCE route-policy results still retain an explicit public
            # configuration hint. Confirm that hint against the target's live
            # configuration and RIB; neither the base label nor a loss symptom
            # is sufficient on its own.
            policy_hints = [
                item
                for item in evidence
                if item.reliability > 0
                and item.category in {"configuration_difference", "route_presence"}
                and item.metadata.get("semantic_family") == "route_policy"
            ]
            prefixes = _rank_route_policy_prefixes(
                context,
                policy_hints,
                preferred_owner=str(location.get("device") or "") or None,
                limit=1,
            )
            prefix = prefixes[0] if prefixes else None
            # Prefix-less prose cannot be independently checked against live
            # configuration and therefore cannot close this semantic branch.
            if policy_hints and prefix:
                query_target, owner_device = _route_policy_target_scope(
                    context,
                    prefix,
                    preferred_owner=str(location.get("device") or "") or None,
                )
                if owner_device:
                    candidates = [owner_device, *(item for item in candidates if item != owner_device)]
                    candidates = candidates[: max_candidate_devices or self.max_candidate_devices]
                for device in candidates:
                    route_evidence: Evidence | None = None
                    route = await invoke_tool(
                        context,
                        "get_route_table",
                        {"device": device, "prefix": query_target, "format": "structured", "max_routes": 20},
                        budget=budget,
                        timeout_seconds=self.timeout_seconds,
                    )
                    if route.success and route.data is not None:
                        selected_discard = _selected_discard_route(route.data, destination=query_target)
                        matching_routes = [
                            item
                            for item in route.data.get("routes", ())
                            if isinstance(item, Mapping) and _route_covers_destination(item, query_target)
                        ]
                        raw_routes = [item for item in route.data.get("routes", ()) if isinstance(item, Mapping)]
                        if owner_device is None and _route_payload_proves_owner(route.data, query_target):
                            owner_device = device
                        route_count = len(matching_routes) or (
                            len(raw_routes) if raw_routes and all(not item.get("prefix") for item in raw_routes) else 0
                        )
                        canonical_prefix = (
                            str(max(matching_routes, key=_route_prefix_length).get("prefix"))
                            if matching_routes
                            else prefix
                        )
                        route_evidence = Evidence(
                            evidence_id=f"semantic-policy-{device}-route",
                            entity_type="device",
                            entity_id=device,
                            category="route_presence",
                            value={
                                "prefix": canonical_prefix,
                                "query_target": query_target,
                                "route_count": route_count,
                                "is_discard": selected_discard is not None,
                                "protocol": selected_discard.get("protocol") if selected_discard else None,
                            },
                            source="get_route_table",
                            timestamp=datetime.now(UTC),
                            reliability=1.0,
                            probe_id="semantic-runtime-closure",
                            origin=EvidenceOrigin.LIVE_TELEMETRY,
                            independence_key=f"tool:get_route_table:{device}:{query_target}",
                            metadata={
                                "semantic_family": "route_policy",
                                "prefix": canonical_prefix,
                                "query_target": query_target,
                                "owner_device": owner_device,
                                "owner_resolution": (
                                    "live_connected_route" if owner_device == device else "public_topology"
                                ),
                            },
                        )
                        collected.append(route_evidence)
                        if selected_discard is not None:
                            # Prefer the direct forwarding cause over policy
                            # vocabulary in the base claim.  This is a generic
                            # selected-route check, not a case-specific alias.
                            result = _build_result(
                                base_result,
                                fault_type=str(self.blackhole_fault_type),
                                device=device,
                                interface=None,
                                evidence=(route_evidence,),
                                confidence=0.95,
                            )
                            outcome = self._outcome(budget, (*collected, *errors), "blackhole_route")
                            return SemanticClosureResult(result, outcome, "static_route")
                    elif route.error:
                        errors.append(
                            tool_error_evidence(
                                evidence_id=f"semantic-policy-{device}-route-error",
                                probe_id="semantic-runtime-closure",
                                entity_id=device,
                                error=route.error,
                                source="get_route_table",
                            )
                        )
                    invocation = await invoke_tool(
                        context,
                        "get_device_config",
                        {"device": device, "max_lines": 500},
                        budget=budget,
                        timeout_seconds=self.timeout_seconds,
                    )
                    if not invocation.success or invocation.data is None:
                        errors.append(
                            tool_error_evidence(
                                evidence_id=f"semantic-policy-{device}-config-error",
                                probe_id="semantic-runtime-closure",
                                entity_id=device,
                                error=invocation.error or "get_device_config failed",
                                source="get_device_config",
                            )
                        )
                        continue
                    config = str(invocation.data.get("config") or "")
                    difference = _route_policy_configuration_difference(config, query_target)
                    if difference is None:
                        continue
                    connected_prefix = _connected_prefix_for_target(config, query_target)
                    # A missing prefix is meaningful only on a device that
                    # actually owns or resolves that prefix. Every unrelated
                    # fabric node naturally lacks the local network statement;
                    # accepting that absence selected the first leaf in large
                    # topologies instead of the affected leaf. An explicit deny
                    # remains direct evidence even when the route is absent.
                    if difference == "missing_prefix_configuration" and not (
                        device == owner_device or connected_prefix is not None
                    ):
                        continue
                    evidence_prefix = (
                        connected_prefix or route_evidence.value.get("prefix")
                        if route_evidence is not None and isinstance(route_evidence.value, Mapping)
                        else connected_prefix or prefix
                    )
                    config_evidence = Evidence(
                        evidence_id=f"semantic-policy-{device}-config",
                        entity_type="device",
                        entity_id=device,
                        category="configuration_difference",
                        value={
                            "semantic_family": "route_policy",
                            "prefix": evidence_prefix,
                            "query_target": query_target,
                            "difference": difference,
                        },
                        source="get_device_config",
                        timestamp=datetime.now(UTC),
                        reliability=1.0,
                        probe_id="semantic-runtime-closure",
                        origin=EvidenceOrigin.CONFIG_READ,
                        independence_key=f"tool:get_device_config:{device}",
                        metadata={
                            "semantic_family": "route_policy",
                            "prefix": evidence_prefix,
                            "query_target": query_target,
                            "owner_device": owner_device,
                            "direct_configuration_evidence": True,
                        },
                    )
                    collected.append(config_evidence)
                    direct = tuple(policy_hints) + tuple(collected)
                    result = _build_result(
                        base_result,
                        fault_type="route_policy_misconfig",
                        device=device,
                        interface=None,
                        evidence=direct,
                        confidence=0.90,
                    )
                    outcome = self._outcome(budget, (*collected, *errors), "route_policy_misconfig")
                    return SemanticClosureResult(result, outcome, "route_policy")

        if requested_family in {"acl", "runtime_semantic", None}:
            for device in candidates:
                if device in acl_checked:
                    continue
                invocation = await invoke_tool(
                    context,
                    "get_device_acl",
                    {"device": device, "view": "summary", "max_lines": 300},
                    budget=budget,
                    timeout_seconds=self.timeout_seconds,
                )
                if not invocation.success or invocation.data is None:
                    errors.append(
                        tool_error_evidence(
                            evidence_id=f"semantic-acl-{device}-error",
                            probe_id="semantic-runtime-closure",
                            entity_id=device,
                            error=invocation.error or "get_device_acl failed",
                            source="get_device_acl",
                        )
                    )
                    continue
                resolved = self._acl_result(base_result, topology, device, invocation.data)
                collected.extend(resolved[1])
                if resolved[0] is not None:
                    outcome = self._outcome(budget, (*collected, *errors), "acl_misconfig")
                    return SemanticClosureResult(resolved[0], outcome, "acl")

        if requested_family in {"static_route", "runtime_semantic", None}:
            for device in candidates:
                if device in device_configs:
                    config = device_configs[device]
                else:
                    invocation = await invoke_tool(
                        context,
                        "get_device_config",
                        {"device": device, "max_lines": 500},
                        budget=budget,
                        timeout_seconds=self.timeout_seconds,
                    )
                    if not invocation.success or invocation.data is None:
                        errors.append(
                            tool_error_evidence(
                                evidence_id=f"semantic-config-{device}-error",
                                probe_id="semantic-runtime-closure",
                                entity_id=device,
                                error=invocation.error or "get_device_config failed",
                                source="get_device_config",
                            )
                        )
                        continue
                    config = str(invocation.data.get("config") or "")
                    device_configs[device] = config
                for index, match in enumerate(_STATIC_ROUTE_RE.finditer(config), start=1):
                    prefix = match.group("prefix")
                    nexthop = match.group("nexthop")
                    config_evidence = Evidence(
                        evidence_id=f"semantic-static-{device}-{index}-config",
                        entity_type="device",
                        entity_id=device,
                        category="configuration_difference",
                        value={"protocol": "static", "prefix": prefix, "nexthop": nexthop},
                        source="get_device_config",
                        timestamp=datetime.now(UTC),
                        reliability=1.0,
                        probe_id="semantic-runtime-closure",
                        origin=EvidenceOrigin.CONFIG_READ,
                        independence_key=f"tool:get_device_config:{device}",
                        metadata={
                            "semantic_family": "static_route",
                            "direct_configuration_evidence": True,
                            "prefix": prefix,
                        },
                    )
                    route = await invoke_tool(
                        context,
                        "get_route_table",
                        {"device": device, "prefix": prefix, "format": "structured", "max_routes": 20},
                        budget=budget,
                        timeout_seconds=self.timeout_seconds,
                    )
                    if not route.success or route.data is None:
                        errors.append(
                            tool_error_evidence(
                                evidence_id=f"semantic-static-{device}-{index}-route-error",
                                probe_id="semantic-runtime-closure",
                                entity_id=device,
                                error=route.error or "get_route_table failed",
                                source="get_route_table",
                            )
                        )
                        continue
                    selected_static = _selected_static_route(route.data, prefix=prefix)
                    if selected_static is None:
                        continue
                    is_discard = bool(selected_static.get("is_discard")) or nexthop.lower() in {
                        "null0",
                        "blackhole",
                        "discard",
                    }
                    unresolved = not selected_static.get("nexthops") and not is_discard
                    next_hop_device, next_hop_role = _topology_role_for_address(context, nexthop)
                    peer_device, peer_role, egress_interface = _static_route_peer_role(
                        topology,
                        device=device,
                        route=selected_static,
                    )
                    if next_hop_device is None and peer_device is not None:
                        next_hop_device, next_hop_role = peer_device, peer_role
                    invalid_next_hop_role = next_hop_role == "client" or peer_role == "client"
                    if not is_discard and not unresolved and not invalid_next_hop_role:
                        continue
                    route_evidence = Evidence(
                        evidence_id=f"semantic-static-{device}-{index}-route",
                        entity_type="device",
                        entity_id=device,
                        category="route_presence",
                        value={
                            "prefix": prefix,
                            "protocol": "static",
                            "selected": selected_static.get("selected"),
                            "resolved_nexthops": len(selected_static.get("nexthops") or []),
                            "is_discard": is_discard,
                            "configured_next_hop": nexthop,
                            "next_hop_device": next_hop_device,
                            "next_hop_role": next_hop_role,
                            "invalid_next_hop_role": invalid_next_hop_role,
                            "egress_interface": egress_interface,
                        },
                        source="get_route_table",
                        timestamp=datetime.now(UTC),
                        reliability=1.0,
                        probe_id="semantic-runtime-closure",
                        origin=EvidenceOrigin.LIVE_TELEMETRY,
                        independence_key=f"tool:get_route_table:{device}:{prefix}",
                        metadata={"semantic_family": "static_route", "prefix": prefix},
                    )
                    direct = (config_evidence, route_evidence)
                    collected.extend(direct)
                    result = _build_result(
                        base_result,
                        fault_type=str(self.blackhole_fault_type if is_discard else self.static_route_fault_type),
                        device=device,
                        interface=None,
                        evidence=direct,
                        confidence=0.95,
                    )
                    canonical = "blackhole_route" if is_discard else "static_route_misconfig"
                    outcome = self._outcome(budget, (*collected, *errors), canonical)
                    return SemanticClosureResult(result, outcome, "static_route")

        outcome = self._outcome(budget, (*collected, *errors), None)
        return SemanticClosureResult(None, outcome, None)

    def _acl_result(
        self,
        base: DiagnosisResult,
        topology: TopologyIndex,
        device: str,
        payload: Mapping[str, Any],
    ) -> tuple[DiagnosisResult | None, tuple[Evidence, ...]]:
        observation = parse_active_acl_drop(payload)
        if observation is None:
            return None, ()
        interface = InterfaceNameNormalizer(topology).normalize(device, observation["binding"])
        if interface.value is None or interface.validation_error:
            return None, ()
        direct = (
            Evidence(
                evidence_id=f"semantic-acl-{device}-config",
                entity_type="interface",
                entity_id=f"{device}:{interface.value}",
                category="configuration_difference",
                value={"action": "drop", "status": "active", "acl_name": observation["name"]},
                source="get_device_acl.sonic",
                timestamp=datetime.now(UTC),
                reliability=1.0,
                probe_id="semantic-runtime-closure",
                origin=EvidenceOrigin.CONFIG_READ,
                independence_key=f"tool:get_device_acl:{device}:config",
                metadata={"semantic_family": "acl", "direct_configuration_evidence": True},
            ),
            Evidence(
                evidence_id=f"semantic-acl-{device}-dataplane",
                entity_type="interface",
                entity_id=f"{device}:{interface.value}",
                category="interface_counter_delta",
                value={"drop_rule_packets": observation["packets"]},
                source="get_device_acl.iptables",
                timestamp=datetime.now(UTC),
                reliability=1.0,
                probe_id="semantic-runtime-closure",
                origin=EvidenceOrigin.LIVE_TELEMETRY,
                independence_key=f"tool:get_device_acl:{device}:counters",
                metadata={"semantic_family": "acl", "direct_dataplane_evidence": True},
            ),
        )
        result = _build_result(
            base,
            fault_type=str(self.acl_fault_type),
            device=device,
            interface=interface.value,
            evidence=direct,
            confidence=0.95,
        )
        return result, direct

    @staticmethod
    def _outcome(budget: ProbeBudget, evidence: Sequence[Evidence], fault_type: str | None) -> ProbeOutcome:
        supporting = [item for item in evidence if item.reliability > 0]
        return ProbeOutcome(
            probe_id="semantic-runtime-closure",
            status="completed" if fault_type else "inconclusive",
            evidence=tuple(evidence),
            tool_calls=budget.tool_calls,
            probe_packets=0,
            metadata={
                "fault_type": fault_type,
                "supporting_evidence": len(supporting),
                "runtime_error_used_as_fault_evidence": False,
            },
        )


__all__ = ["SemanticClosureResult", "SemanticRuntimeClosure"]
