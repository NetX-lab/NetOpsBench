"""Provider-neutral conversion of base-agent tool observations into Evidence.

The harness never trusts the base agent's prose as an observation.  When an
agent records real tool calls through ``DiagnosticContext.trace``, however,
their structured results are the same public network observations that the
harness would otherwise query again.  This adapter recognizes a deliberately
small allowlist and ignores model messages, labels, and unstructured errors.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from ..models import Evidence, EvidenceDirection, EvidenceOrigin
from ..normalization.interface import TopologyIndex
from ..probes.base import parse_ping_payload
from ..topology.semantics import with_attachment_aliases
from .temporal import classify_log_signal, is_temporal_signal
from .bgp import bgp_configuration_fault_reason
from .time import parse_timestamp

_STATIC_ROUTE_RE = re.compile(
    r"^\s*ip route\s+(?P<prefix>\S+)\s+(?P<nexthop>\S+)(?:\s+.*)?$",
    re.IGNORECASE | re.MULTILINE,
)
_PREFIX_LIST_RE = re.compile(
    r"^\s*ip prefix-list\s+(?P<name>\S+)(?:\s+seq\s+\d+)?\s+"
    r"(?P<action>permit|deny)\s+(?P<prefix>\S+)",
    re.IGNORECASE | re.MULTILINE,
)
_ROUTE_MAP_RE = re.compile(r"^\s*route-map\s+(?P<name>\S+)\s+(?P<action>permit|deny)\b", re.IGNORECASE)
_MATCH_PREFIX_LIST_RE = re.compile(
    r"^\s*match\s+ip\s+address\s+prefix-list\s+(?P<names>.+?)\s*$",
    re.IGNORECASE,
)
_LOG_INTERFACE_RE = re.compile(r"\b(Ethernet\d+)\b", re.IGNORECASE)
_ACL_TABLE_RE = re.compile(
    r"^(?P<name>\S+)\s+L3\s+(?P<binding>\S+)\s+.*\bActive\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_ACL_DROP_RE = re.compile(r"^\S+\s+\S+\s+\d+\s+(?:DROP|DENY)\b.*\bActive\s*$", re.IGNORECASE | re.MULTILINE)
_IPTABLES_DROP_RE = re.compile(
    r"^\s*\d+\s+(?P<packets>\d+[KMG]?)\s+\S+\s+DROP\b.*$",
    re.IGNORECASE | re.MULTILINE,
)
_ALLOWED_TOOLS = frozenset(
    {
        "get_device_acl",
        "get_device_config",
        "get_device_interfaces",
        "get_interface_metrics",
        "get_device_logs",
        "get_route_table",
        "get_bgp_neighbors",
        "get_bgp_rib",
        "get_pingmesh_hotspots",
        "get_pingmesh_summary",
        "ping_link_test",
        "latency_link_test",
        "payload_integrity_test",
        "payload_integrity_link_test",
        "query_bgp_events",
    }
)


def trace_step_count(context: Any) -> int:
    """Return a best-effort trace cursor without requiring a specific agent."""
    trace = getattr(context, "trace", None)
    to_steps = getattr(trace, "to_steps", None)
    if not callable(to_steps):
        return 0
    try:
        steps = to_steps()
    except Exception:  # noqa: BLE001 - an optional trace must never break diagnosis
        return 0
    return len(steps) if isinstance(steps, list) else 0


def evidence_from_base_tool_observations(
    context: Any,
    topology: TopologyIndex,
    *,
    start_index: int = 0,
    latency_threshold_ms: float = 30.0,
    latency_relative_multiplier: float = 3.0,
) -> list[Evidence]:
    """Convert allowlisted successful base-tool results after ``start_index``.

    Tool errors and prose-only steps are intentionally ignored.  Every emitted
    item retains the originating trace/tool identity in its independence key.
    """
    trace = getattr(context, "trace", None)
    to_steps = getattr(trace, "to_steps", None)
    if not callable(to_steps):
        return []
    try:
        steps = to_steps()
    except Exception:  # noqa: BLE001 - optional provider tracing is best effort
        return []
    if not isinstance(steps, list):
        return []

    evidence: list[Evidence] = []
    for index, step in enumerate(steps[max(0, int(start_index)) :], start=max(0, int(start_index))):
        if not isinstance(step, Mapping) or step.get("error"):
            continue
        tool = _tool_name(step)
        if tool not in _ALLOWED_TOOLS:
            continue
        args = _tool_args(step)
        payload = _structured_payload(step.get("observation"), tool)
        if payload is None:
            continue
        call_id = str(step.get("tool_call_id") or step.get("run_id") or f"step-{index}")
        evidence.extend(
            _convert(
                tool,
                args,
                payload,
                topology=topology,
                call_id=call_id,
                latency_threshold_ms=latency_threshold_ms,
                latency_relative_multiplier=latency_relative_multiplier,
            )
        )
    return _deduplicate(evidence)


def _tool_name(step: Mapping[str, Any]) -> str:
    name = step.get("name") or step.get("tool")
    if name:
        return str(name)
    calls = step.get("tool_calls")
    if isinstance(calls, Sequence) and calls and isinstance(calls[0], Mapping):
        return str(calls[0].get("function_name") or calls[0].get("name") or "")
    return ""


def _tool_args(step: Mapping[str, Any]) -> Mapping[str, Any]:
    args = step.get("args") or step.get("input")
    if isinstance(args, Mapping):
        return args
    calls = step.get("tool_calls")
    if isinstance(calls, Sequence) and calls and isinstance(calls[0], Mapping):
        candidate = calls[0].get("arguments") or calls[0].get("args")
        if isinstance(candidate, Mapping):
            return candidate
    return {}


def _structured_payload(value: Any, tool: str, *, depth: int = 0) -> Mapping[str, Any] | None:
    if depth > 8:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text.startswith(("{", "[")):
            return None
        try:
            return _structured_payload(json.loads(text), tool, depth=depth + 1)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
    if isinstance(value, Mapping):
        if _looks_like_tool_payload(value, tool):
            return value
        for key in ("structured_content", "result", "data", "artifact", "content", "results"):
            if key not in value:
                continue
            found = _structured_payload(value[key], tool, depth=depth + 1)
            if found is not None:
                return found
        return None
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            found = _structured_payload(item, tool, depth=depth + 1)
            if found is not None:
                return found
    return None


def _looks_like_tool_payload(value: Mapping[str, Any], tool: str) -> bool:
    required = {
        "get_device_acl": {"sonic_acl_config", "iptables_forward_rules"},
        "get_device_config": {"config"},
        "get_device_interfaces": {"interfaces"},
        "get_interface_metrics": {"summary"},
        "get_route_table": {"routes", "route_table", "route_count"},
        "get_bgp_neighbors": {"neighbors"},
        "get_bgp_rib": {"bgp_rib"},
        "get_pingmesh_hotspots": {"hotspots"},
        "get_pingmesh_summary": {"path_type_summary"},
        "ping_link_test": {"output", "return_code"},
        "latency_link_test": {"directions"},
        "payload_integrity_test": {"supported", "packets_sent", "packets_observed"},
        "payload_integrity_link_test": {"directions"},
        "query_bgp_events": {"events"},
        "get_device_logs": {"logs"},
    }[tool]
    return bool(required.intersection(value))


def _convert(
    tool: str,
    args: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    topology: TopologyIndex,
    call_id: str,
    latency_threshold_ms: float,
    latency_relative_multiplier: float,
) -> list[Evidence]:
    if tool == "get_device_interfaces":
        return _interface_evidence(args, payload, topology=topology, call_id=call_id)
    if tool == "get_interface_metrics":
        return _interface_metric_evidence(args, payload, topology=topology, call_id=call_id)
    if tool == "get_device_acl":
        return _acl_evidence(args, payload, topology=topology, call_id=call_id)
    if tool == "get_device_logs":
        return _log_evidence(args, payload, topology=topology, call_id=call_id)
    if tool == "get_route_table":
        return _route_evidence(args, payload, call_id=call_id)
    if tool == "get_device_config":
        return _config_evidence(args, payload, call_id=call_id)
    if tool == "get_bgp_neighbors":
        return _bgp_evidence(args, payload, call_id=call_id)
    if tool == "get_bgp_rib":
        return _bgp_rib_evidence(args, payload, call_id=call_id)
    if tool in {"get_pingmesh_summary", "get_pingmesh_hotspots"}:
        return _pingmesh_evidence(tool, payload, call_id=call_id)
    if tool == "query_bgp_events":
        return _event_evidence(payload, call_id=call_id)
    if tool == "latency_link_test":
        return _latency_link_evidence(
            args,
            payload,
            topology=topology,
            call_id=call_id,
            absolute_threshold_ms=latency_threshold_ms,
            relative_multiplier=latency_relative_multiplier,
        )
    if tool == "ping_link_test":
        return _ping_link_evidence(args, payload, topology=topology, call_id=call_id)
    if tool in {"payload_integrity_test", "payload_integrity_link_test"}:
        return _payload_integrity_evidence(
            tool,
            args,
            payload,
            topology=topology,
            call_id=call_id,
        )
    return []


def _validated_link(
    topology: TopologyIndex,
    *,
    device_a: str,
    interface_a: str,
    device_b: str,
    interface_b: str,
) -> tuple[str, str, str, str, Any] | None:
    resolved_a = topology.resolve_device(device_a)
    resolved_b = topology.resolve_device(device_b)
    if not resolved_a or not resolved_b:
        return None
    endpoint_a = topology.resolve_interface(resolved_a, interface_a)
    endpoint_b = topology.resolve_interface(resolved_b, interface_b)
    if endpoint_a is None or endpoint_b is None:
        return None
    link = topology.physical_link(resolved_a, endpoint_a.canonical_interface)
    peer = link.peer_of(resolved_a, endpoint_a.canonical_interface) if link is not None else None
    if (
        link is None
        or peer is None
        or peer.device != resolved_b
        or peer.canonical_interface != endpoint_b.canonical_interface
    ):
        return None
    return (
        resolved_a,
        endpoint_a.canonical_interface,
        resolved_b,
        endpoint_b.canonical_interface,
        link,
    )


def _ping_link_evidence(
    args: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    topology: TopologyIndex,
    call_id: str,
) -> list[Evidence]:
    endpoints = _validated_link(
        topology,
        device_a=str(payload.get("source") or args.get("src") or ""),
        interface_a=str(payload.get("source_interface") or args.get("source_interface") or ""),
        device_b=str(payload.get("target_device") or args.get("target_device") or ""),
        interface_b=str(payload.get("target_interface") or args.get("target_interface") or ""),
    )
    if endpoints is None:
        return []
    source, source_interface, target, target_interface, link = endpoints
    try:
        observation = parse_ping_payload(payload)
    except (TypeError, ValueError):
        return []
    if observation.sent <= 0:
        return []
    return [
        Evidence(
            evidence_id=f"base-tool-{call_id}-link-loss",
            entity_type="path",
            entity_id=f"{source}--{target}",
            category="packet_loss_rate",
            value=observation.loss_rate,
            source="base_tool:ping_link_test",
            timestamp=datetime.now(UTC),
            reliability=min(1.0, observation.sent / max(1, observation.requested_count)),
            raw_reference=f"trace-tool:{call_id}",
            observed_path=(link.link_id,),
            possible_paths=((link.link_id,),),
            covered_links=(link.link_id,),
            path_observation_confidence=1.0,
            origin=EvidenceOrigin.LIVE_TELEMETRY,
            independence_key=f"base-tool:ping_link_test:{call_id}",
            metadata={
                "source": source,
                "destination": target,
                "source_interface": source_interface,
                "target_interface": target_interface,
                "sent": observation.sent,
                "received": observation.received,
                "loss_rate": observation.loss_rate,
                "rounds": 1,
                "selection": "base_link_observation",
                "link_probe": True,
                "payload_size": observation.payload_size,
                "dont_fragment": observation.dont_fragment,
                "base_tool_observation": True,
            },
        )
    ]


def _payload_integrity_evidence(
    tool: str,
    args: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    topology: TopologyIndex,
    call_id: str,
) -> list[Evidence]:
    if payload.get("supported") is False:
        return []
    link = None
    covered_links: tuple[str, ...] = ()
    directions = payload.get("directions")
    if tool == "payload_integrity_link_test":
        endpoints = _validated_link(
            topology,
            device_a=str(payload.get("device_a") or args.get("device_a") or ""),
            interface_a=str(payload.get("interface_a") or args.get("interface_a") or ""),
            device_b=str(payload.get("device_b") or args.get("device_b") or ""),
            interface_b=str(payload.get("interface_b") or args.get("interface_b") or ""),
        )
        if endpoints is None or not isinstance(directions, Sequence):
            return []
        device_a, interface_a, device_b, interface_b, link = endpoints
        covered_links = (link.link_id,)
        observations = [item for item in directions if isinstance(item, Mapping)]
        entity_id = link.link_id
    else:
        observations = [payload]
        device_a = str(payload.get("source") or args.get("src") or "")
        device_b = str(payload.get("destination") or args.get("dst_ip") or "")
        interface_a = interface_b = ""
        entity_id = f"{device_a}--{device_b}"

    result: list[Evidence] = []
    for index, item in enumerate(observations, start=1):
        sent = int(item.get("packets_sent") or 0)
        observed = int(item.get("packets_observed") or 0)
        missing = int(item.get("missing_packets") or max(0, sent - observed))
        checksum_failures = int(item.get("checksum_failures") or 0)
        checksum_valid = item.get("checksum_valid")
        if sent <= 0 or checksum_valid is None:
            continue
        source = str(item.get("source") or device_a)
        target = str(item.get("target_device") or item.get("destination") or device_b)
        independence_key = f"base-tool:{tool}:{call_id}:direction:{index}"
        metadata = {
            "source": source,
            "destination": target,
            "sent": sent,
            "packets_observed": observed,
            "missing_packets": missing,
            "checksum_failures": checksum_failures,
            "checksum_valid": bool(checksum_valid),
            "integrity_complete": bool(item.get("integrity_complete")),
            "method": payload.get("method"),
            "base_tool_observation": True,
        }
        if link is not None:
            source_device = topology.resolve_device(source)
            source_endpoint = (
                topology.resolve_interface(source_device, str(item.get("source_interface") or ""))
                if source_device
                else None
            )
            if source_device and source_endpoint is not None and checksum_failures:
                metadata.update(
                    fault_endpoint_device=source_device,
                    fault_endpoint_interface=source_endpoint.canonical_interface,
                    interface_direct_anomaly=True,
                )
            metadata.update(
                device_a=device_a,
                interface_a=interface_a,
                device_b=device_b,
                interface_b=interface_b,
            )
        common = {
            "entity_type": "path",
            "entity_id": entity_id,
            "source": f"base_tool:{tool}",
            "timestamp": datetime.now(UTC),
            "reliability": min(1.0, observed / sent) if checksum_failures else 1.0,
            "raw_reference": f"trace-tool:{call_id}",
            "observed_path": covered_links or None,
            "possible_paths": (covered_links,) if covered_links else (),
            "covered_links": covered_links,
            "path_observation_confidence": 1.0 if covered_links else 0.0,
            "origin": EvidenceOrigin.LIVE_TELEMETRY,
            "independence_key": independence_key,
            "metadata": metadata,
        }
        result.append(
            Evidence(
                evidence_id=f"base-tool-{call_id}-integrity-{index}",
                category="payload_integrity_failure",
                value=checksum_failures > 0 or checksum_valid is False,
                **common,
            )
        )
        if missing:
            result.append(
                Evidence(
                    evidence_id=f"base-tool-{call_id}-missing-{index}",
                    category="packet_loss_rate",
                    value=missing / sent,
                    supports_submission=False,
                    **common,
                )
            )
    return result


def _latency_link_evidence(
    args: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    topology: TopologyIndex,
    call_id: str,
    absolute_threshold_ms: float,
    relative_multiplier: float,
) -> list[Evidence]:
    """Convert a validated one-hop latency result without trusting prose.

    The public tool validates both physical endpoints before measuring.  That
    makes its structured result exact-link evidence, even when the base
    diagnosis later returns the wrong label or an invalid schema.  Median and
    p95 from one direction share an independence key because they are derived
    from the same packet batch.
    """

    endpoints = _validated_link(
        topology,
        device_a=str(payload.get("device_a") or args.get("device_a") or ""),
        interface_a=str(payload.get("interface_a") or args.get("interface_a") or ""),
        device_b=str(payload.get("device_b") or args.get("device_b") or ""),
        interface_b=str(payload.get("interface_b") or args.get("interface_b") or ""),
    )
    if endpoints is None:
        return []
    device_a, interface_a, device_b, interface_b, link = endpoints

    directions = [
        item
        for item in payload.get("directions", ())
        if isinstance(item, Mapping) and int(item.get("sample_count") or 0) > 0
    ]
    if len(directions) != 2:
        return []
    medians = [float(item.get("median_ms") or 0.0) for item in directions]
    high_index = max(range(2), key=medians.__getitem__)
    low_reference = max(medians[1 - high_index], 0.001)
    endpoint_discriminated = bool(
        medians[high_index] >= float(absolute_threshold_ms)
        and medians[high_index] / low_reference >= float(relative_multiplier)
    )
    fault_source = str(directions[high_index].get("source") or "") if endpoint_discriminated else ""
    fault_interface = str(directions[high_index].get("source_interface") or "") if endpoint_discriminated else ""

    result: list[Evidence] = []
    for index, item in enumerate(directions, start=1):
        source = topology.resolve_device(str(item.get("source") or ""))
        target = topology.resolve_device(str(item.get("target_device") or ""))
        if source not in {device_a, device_b} or target not in {device_a, device_b} or source == target:
            continue
        source_endpoint = topology.resolve_interface(source, str(item.get("source_interface") or ""))
        target_endpoint = topology.resolve_interface(target, str(item.get("target_interface") or ""))
        if source_endpoint is None or target_endpoint is None:
            continue
        median_ms = float(item.get("median_ms") or 0.0)
        p95_ms = float(item.get("p95_ms") or median_ms)
        sample_count = int(item.get("sample_count") or 0)
        is_fault_direction = endpoint_discriminated and source == fault_source
        metadata = {
            "source": source,
            "destination": target,
            "source_interface": source_endpoint.canonical_interface,
            "target_interface": target_endpoint.canonical_interface,
            "sample_count": sample_count,
            "selection": "base_directional_link_latency",
            "link_probe": True,
            "one_way": True,
            "absolute_anomaly": median_ms >= float(absolute_threshold_ms),
            "relative_anomaly": is_fault_direction,
            "category_anomaly": is_fault_direction,
            "interface_direct_anomaly": is_fault_direction,
            "base_tool_observation": True,
        }
        if is_fault_direction:
            fault_endpoint = topology.resolve_interface(source, fault_interface)
            metadata.update(
                fault_endpoint_device=source,
                fault_endpoint_interface=(
                    fault_endpoint.canonical_interface
                    if fault_endpoint is not None
                    else source_endpoint.canonical_interface
                ),
            )
        direction = EvidenceDirection.A_TO_B if source == device_a else EvidenceDirection.B_TO_A
        common = {
            "entity_type": "path",
            "entity_id": f"{source}--{target}",
            "source": "base_tool:latency_link_test",
            "timestamp": datetime.now(UTC),
            "reliability": min(1.0, sample_count / max(1, int(payload.get("count_per_direction") or sample_count))),
            "raw_reference": f"trace-tool:{call_id}",
            "origin": EvidenceOrigin.LIVE_TELEMETRY,
            "independence_key": f"base-tool:latency_link_test:{call_id}:direction:{index}",
            "direction": direction,
            "metadata": metadata,
            "observed_path": (link.link_id,),
            "possible_paths": ((link.link_id,),),
            "covered_links": (link.link_id,),
            "path_observation_confidence": 1.0,
        }
        result.extend(
            (
                Evidence(
                    evidence_id=f"base-tool-{call_id}-latency-median-{index}",
                    category="latency_median",
                    value=median_ms,
                    **common,
                ),
                Evidence(
                    evidence_id=f"base-tool-{call_id}-latency-p95-{index}",
                    category="latency_p95",
                    value=p95_ms,
                    **common,
                ),
            )
        )
    return result


def _pingmesh_loss_rate(value: Any) -> float | None:
    """Normalize the two public Pingmesh result shapes to a 0..1 rate.

    Summary rows expose the stored rate while the historical hotspot query
    may expose percentage points.  Values above one therefore require the
    documented percentage conversion; values already in rate form are kept.
    """
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed < 0:
        return None
    return min(1.0, parsed / 100.0 if parsed > 1.0 else parsed)


def _pingmesh_timestamp(payload: Mapping[str, Any]) -> datetime | None:
    scope = payload.get("time_scope")
    raw = scope.get("end_time") if isinstance(scope, Mapping) else None
    return parse_timestamp(raw)


def _pingmesh_evidence(tool: str, payload: Mapping[str, Any], *, call_id: str) -> list[Evidence]:
    """Preserve successful episode-scoped Pingmesh results as typed evidence.

    These observations may route and rank a hard case, but their aggregate
    shape cannot by itself satisfy the repeated-loss or interface contracts.
    Final submission still requires independent active/link evidence.
    """
    timestamp = _pingmesh_timestamp(payload)
    independence_key = f"tool:{tool}:{call_id}"
    result: list[Evidence] = []
    if tool == "get_pingmesh_summary":
        summary = payload.get("path_type_summary")
        if not isinstance(summary, Mapping):
            return []
        for path_type, row in sorted(summary.items(), key=lambda item: str(item[0])):
            if not isinstance(row, Mapping):
                continue
            loss_rate = _pingmesh_loss_rate(row.get("packet_loss"))
            if loss_rate is not None:
                result.append(
                    _evidence(
                        call_id,
                        suffix=f"summary-{path_type}-loss",
                        entity_type="path_group",
                        entity_id=str(path_type),
                        category="packet_loss_rate",
                        value=loss_rate,
                        tool=tool,
                        origin=EvidenceOrigin.LIVE_TELEMETRY,
                        metadata={
                            "path_type": str(path_type),
                            "aggregate_pingmesh": True,
                            "weak_performance_symptom": 0.02 <= loss_rate < 0.10,
                            "base_tool_observation": True,
                        },
                        timestamp=timestamp,
                        independence_key=independence_key,
                    )
                )
            try:
                latency = float(row.get("rtt_p99"))
            except (TypeError, ValueError):
                continue
            result.append(
                _evidence(
                    call_id,
                    suffix=f"summary-{path_type}-latency",
                    entity_type="path_group",
                    entity_id=str(path_type),
                    category="latency_p95",
                    value=latency,
                    tool=tool,
                    origin=EvidenceOrigin.LIVE_TELEMETRY,
                    metadata={
                        "path_type": str(path_type),
                        "aggregate_pingmesh": True,
                        "base_tool_observation": True,
                    },
                    timestamp=timestamp,
                    independence_key=independence_key,
                )
            )
        return result

    hotspots = payload.get("hotspots")
    if not isinstance(hotspots, list):
        return []
    for index, row in enumerate(hotspots):
        if not isinstance(row, Mapping):
            continue
        src_leaf = str(row.get("src_leaf") or "").strip()
        dst_leaf = str(row.get("dst_leaf") or "").strip()
        loss_rate = _pingmesh_loss_rate(row.get("packet_loss"))
        if not src_leaf or not dst_leaf or loss_rate is None:
            continue
        result.append(
            _evidence(
                call_id,
                suffix=f"hotspot-{index}-loss",
                entity_type="path",
                entity_id=f"{src_leaf}--{dst_leaf}",
                category="packet_loss_rate",
                value=loss_rate,
                tool=tool,
                origin=EvidenceOrigin.LIVE_TELEMETRY,
                metadata=with_attachment_aliases(
                    {
                        "source": src_leaf,
                        "destination": dst_leaf,
                        "src_leaf": src_leaf,
                        "dst_leaf": dst_leaf,
                        "leaf_aggregate": True,
                        "attachment_aggregate": True,
                        "weak_performance_symptom": 0.01 <= loss_rate < 0.10,
                        "base_tool_observation": True,
                    }
                ),
                timestamp=timestamp,
                independence_key=independence_key,
            )
        )
    return result


def _interface_evidence(
    args: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    topology: TopologyIndex,
    call_id: str,
) -> list[Evidence]:
    device = topology.resolve_device(str(payload.get("device") or args.get("device") or ""))
    rows = payload.get("interfaces")
    if device is None or not isinstance(rows, list):
        return []
    result: list[Evidence] = []
    resolved_rows: list[tuple[Mapping[str, Any], Any, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping) or not row.get("name"):
            continue
        endpoint = topology.resolve_interface(device, str(row["name"]))
        if endpoint is None:
            continue
        link = topology.physical_link(device, endpoint.canonical_interface)
        resolved_rows.append((row, endpoint, link))
        metadata = {"link_id": link.link_id if link else None, "base_tool_observation": True}
        for category, value in (
            ("interface_admin_state", row.get("admin")),
            ("interface_oper_state", row.get("oper")),
        ):
            result.append(
                _evidence(
                    call_id,
                    suffix=f"{device}-{endpoint.canonical_interface}-{category}",
                    entity_type="interface",
                    entity_id=f"{device}:{endpoint.canonical_interface}",
                    category=category,
                    value=value,
                    tool="get_device_interfaces",
                    origin=EvidenceOrigin.LIVE_TELEMETRY,
                    metadata=metadata,
                )
            )
    # A local MTU outlier is a safe planning signal, but not submission proof.
    # The normal MTU branch still requires a size sweep and peer comparison.
    mtu_rows: list[tuple[int, Any, Any]] = []
    for row, endpoint, link in resolved_rows:
        try:
            mtu = int(row.get("mtu") or row.get("ip_mtu"))
        except (TypeError, ValueError):
            continue
        if mtu > 0:
            mtu_rows.append((mtu, endpoint, link))
    if len(mtu_rows) >= 3:
        counts: dict[int, int] = {}
        for mtu, *_rest in mtu_rows:
            counts[mtu] = counts.get(mtu, 0) + 1
        baseline = max(counts, key=lambda value: (counts[value], value))
        if counts[baseline] >= 2:
            for mtu, endpoint, link in mtu_rows:
                if baseline - mtu < 256:
                    continue
                result.append(
                    _evidence(
                        call_id,
                        suffix=f"{device}-{endpoint.canonical_interface}-mtu-outlier",
                        entity_type="interface",
                        entity_id=f"{device}:{endpoint.canonical_interface}",
                        category="configuration_difference",
                        value={"local_mtu": mtu, "device_mode_mtu": baseline, "different": True},
                        tool="get_device_interfaces",
                        origin=EvidenceOrigin.CONFIG_READ,
                        metadata={
                            "link_id": link.link_id if link else None,
                            "semantic_family": "mtu",
                            "planning_only": True,
                            "base_tool_observation": True,
                        },
                        supports_submission=False,
                    )
                )
    return result


def _interface_metric_evidence(
    args: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    topology: TopologyIndex,
    call_id: str,
) -> list[Evidence]:
    """Preserve positive counter deltas as location evidence, never a fault label."""
    device = topology.resolve_device(str(payload.get("device") or args.get("device") or ""))
    interface = str(payload.get("interface") or args.get("interface") or "")
    endpoint = topology.resolve_interface(device, interface) if device and interface else None
    summary = payload.get("summary")
    if device is None or endpoint is None or not isinstance(summary, Mapping):
        return []
    link = topology.physical_link(device, endpoint.canonical_interface)
    result: list[Evidence] = []
    for metric in ("in_discarded_packets", "out_discarded_packets", "in_errors", "out_errors"):
        row = summary.get(metric)
        if not isinstance(row, Mapping):
            continue
        try:
            delta = float(row.get("window_delta") or 0.0)
        except (TypeError, ValueError):
            continue
        if delta <= 0:
            continue
        result.append(
            _evidence(
                call_id,
                suffix=f"{device}-{endpoint.canonical_interface}-{metric}",
                entity_type="interface",
                entity_id=f"{device}:{endpoint.canonical_interface}",
                category="interface_counter_delta",
                value={"metric": metric, "delta": delta},
                tool="get_interface_metrics",
                origin=EvidenceOrigin.LIVE_TELEMETRY,
                metadata={
                    "device": device,
                    "interface": endpoint.canonical_interface,
                    "link_id": link.link_id if link else None,
                    "planning_only": True,
                    "base_tool_observation": True,
                },
                supports_submission=False,
            )
        )
    return result


def _acl_evidence(
    args: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    topology: TopologyIndex,
    call_id: str,
) -> list[Evidence]:
    """Convert an active ACL plus a live DROP counter into two-source Evidence."""
    device = topology.resolve_device(str(payload.get("device") or args.get("device") or ""))
    observation = parse_active_acl_drop(payload)
    if device is None or observation is None:
        return []
    endpoint = topology.resolve_interface(device, observation["binding"])
    if endpoint is None:
        return []
    link = topology.physical_link(device, endpoint.canonical_interface)
    metadata = {
        "semantic_family": "acl",
        "device": device,
        "interface": endpoint.canonical_interface,
        "link_id": link.link_id if link else None,
        "base_tool_observation": True,
    }
    return [
        _evidence(
            call_id,
            suffix=f"{device}-{endpoint.canonical_interface}-acl-config",
            entity_type="interface",
            entity_id=f"{device}:{endpoint.canonical_interface}",
            category="configuration_difference",
            value={"action": "drop", "status": "active", "acl_name": observation["name"]},
            tool="get_device_acl",
            origin=EvidenceOrigin.CONFIG_READ,
            metadata={**metadata, "direct_configuration_evidence": True},
            independence_key=f"tool:get_device_acl:{device}:config",
        ),
        _evidence(
            call_id,
            suffix=f"{device}-{endpoint.canonical_interface}-acl-counter",
            entity_type="interface",
            entity_id=f"{device}:{endpoint.canonical_interface}",
            category="interface_counter_delta",
            value={"drop_rule_packets": observation["packets"]},
            tool="get_device_acl",
            origin=EvidenceOrigin.LIVE_TELEMETRY,
            metadata={**metadata, "direct_dataplane_evidence": True},
            independence_key=f"tool:get_device_acl:{device}:counters",
        ),
    ]


def parse_active_acl_drop(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return a normalized ACL observation only when config and traffic agree."""
    sonic = str(payload.get("sonic_acl_config") or "")
    iptables = str(payload.get("iptables_forward_rules") or "")
    table = _ACL_TABLE_RE.search(sonic)
    drop = _ACL_DROP_RE.search(sonic)
    dataplane = next(
        (match for match in _IPTABLES_DROP_RE.finditer(iptables) if _scaled_counter(match.group("packets")) > 0),
        None,
    )
    if table is None or drop is None or dataplane is None:
        return None
    return {
        "name": table.group("name"),
        "binding": table.group("binding"),
        "packets": _scaled_counter(dataplane.group("packets")),
    }


def _scaled_counter(value: object) -> int:
    text = str(value or "0").strip().upper()
    multiplier = {"K": 1_000, "M": 1_000_000, "G": 1_000_000_000}.get(text[-1:], 1)
    number = text[:-1] if multiplier > 1 else text
    try:
        return int(float(number) * multiplier)
    except ValueError:
        return 0


def _log_evidence(
    args: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    topology: TopologyIndex,
    call_id: str,
) -> list[Evidence]:
    """Convert explicit, timestamped interface transitions from live logs."""
    device = topology.resolve_device(str(payload.get("device") or args.get("device") or ""))
    rows = payload.get("logs")
    if device is None or not isinstance(rows, list):
        return []
    result: list[Evidence] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            continue
        message = str(row.get("message") or "")
        interface_match = _LOG_INTERFACE_RE.search(message)
        signal = classify_log_signal(message)
        if interface_match is None or not is_temporal_signal(signal):
            continue
        endpoint = topology.resolve_interface(device, interface_match.group(1))
        if endpoint is None:
            continue
        link = topology.physical_link(device, endpoint.canonical_interface)
        event_time = str(row.get("time") or row.get("timestamp") or row.get("datetime") or index)
        result.append(
            _evidence(
                call_id,
                suffix=f"{device}-{endpoint.canonical_interface}-{index}",
                entity_type="interface",
                entity_id=f"{device}:{endpoint.canonical_interface}",
                category="syslog_event",
                value={"temporal_transition": True, "temporal_signal": signal, "message": message[:240]},
                tool="get_device_logs",
                origin=EvidenceOrigin.LIVE_TELEMETRY,
                metadata={
                    "device": device,
                    "interface": endpoint.canonical_interface,
                    "link_id": link.link_id if link else None,
                    "temporal_transition": True,
                    "temporal_signal": signal,
                    "base_tool_observation": True,
                },
                independence_key=(f"event:get_device_logs:{device}:{endpoint.canonical_interface}:{event_time}"),
            )
        )
    return result


def _route_evidence(args: Mapping[str, Any], payload: Mapping[str, Any], *, call_id: str) -> list[Evidence]:
    device = str(payload.get("device") or args.get("device") or "")
    if not device:
        return []
    routes = payload.get("routes")
    if not isinstance(routes, list):
        return []
    prefix = str(payload.get("prefix") or args.get("prefix") or "") or None
    result: list[Evidence] = []
    if not routes:
        result.append(
            _evidence(
                call_id,
                suffix=f"{device}-absent-{prefix or 'all'}",
                entity_type="device",
                entity_id=device,
                category="route_presence",
                value={"prefix": prefix, "route_count": 0},
                tool="get_route_table",
                origin=EvidenceOrigin.LIVE_TELEMETRY,
                metadata={"prefix": prefix, "base_tool_observation": True},
                supports_submission=False,
            )
        )
        return result
    for index, route in enumerate(routes):
        if not isinstance(route, Mapping) or route.get("selected") is False:
            continue
        protocol = str(route.get("protocol") or route.get("type") or "").lower()
        is_discard = bool(route.get("is_discard")) or _route_has_discard_hop(route)
        value = {
            "prefix": route.get("prefix") or prefix,
            "protocol": protocol or None,
            "selected": route.get("selected"),
            "is_discard": is_discard,
            "nexthops": route.get("nexthops") or [],
        }
        result.append(
            _evidence(
                call_id,
                suffix=f"{device}-{index}",
                entity_type="device",
                entity_id=device,
                category="observed_routing_entry",
                value=value,
                tool="get_route_table",
                origin=EvidenceOrigin.LIVE_TELEMETRY,
                metadata={
                    "prefix": value["prefix"],
                    "semantic_family": "static_route" if protocol == "static" or is_discard else None,
                    "direct_route_evidence": True,
                    "base_tool_observation": True,
                },
                supports_submission=is_discard,
            )
        )
    return result


def _config_evidence(args: Mapping[str, Any], payload: Mapping[str, Any], *, call_id: str) -> list[Evidence]:
    device = str(payload.get("device") or args.get("device") or "")
    config = str(payload.get("config") or "")
    if not device or not config:
        return []
    result: list[Evidence] = []
    for index, match in enumerate(_STATIC_ROUTE_RE.finditer(config)):
        result.append(
            _evidence(
                call_id,
                suffix=f"{device}-static-{index}",
                entity_type="device",
                entity_id=device,
                category="configured_static_route",
                value={"prefix": match.group("prefix"), "next_hop": match.group("nexthop")},
                tool="get_device_config",
                origin=EvidenceOrigin.CONFIG_READ,
                metadata={
                    "semantic_family": "static_route",
                    "direct_configuration_evidence": True,
                    "base_tool_observation": True,
                },
                supports_submission=False,
            )
        )
    for index, prefix in enumerate(sorted(explicit_route_policy_denies(config))):
        result.append(
            _evidence(
                call_id,
                suffix=f"{device}-policy-{index}",
                entity_type="device",
                entity_id=device,
                category="configuration_difference",
                value={"semantic_family": "route_policy", "prefix": prefix, "difference": "explicit_prefix_deny"},
                tool="get_device_config",
                origin=EvidenceOrigin.CONFIG_READ,
                metadata={
                    "semantic_family": "route_policy",
                    "prefix": prefix,
                    "direct_configuration_evidence": True,
                    "base_tool_observation": True,
                },
                supports_submission=False,
            )
        )
    return result


def explicit_route_policy_denies(config: str) -> set[str]:
    """Return prefixes explicitly denied by prefix-list/route-map semantics."""
    prefix_lists: dict[str, list[tuple[str, str]]] = {}
    for match in _PREFIX_LIST_RE.finditer(config):
        prefix_lists.setdefault(match.group("name"), []).append((match.group("action").lower(), match.group("prefix")))
    denied = {prefix for rows in prefix_lists.values() for action, prefix in rows if action == "deny"}
    active_deny_map = False
    for line in config.splitlines():
        route_map = _ROUTE_MAP_RE.match(line)
        if route_map:
            active_deny_map = route_map.group("action").lower() == "deny"
            continue
        match = _MATCH_PREFIX_LIST_RE.match(line)
        if not active_deny_map or match is None:
            continue
        for name in match.group("names").split():
            denied.update(prefix for action, prefix in prefix_lists.get(name, ()) if action == "permit")
    return denied


def _bgp_evidence(args: Mapping[str, Any], payload: Mapping[str, Any], *, call_id: str) -> list[Evidence]:
    device = str(payload.get("device") or args.get("device") or "")
    rows = payload.get("neighbors")
    if not device or not isinstance(rows, list):
        return []
    abnormal = [
        dict(row)
        for row in rows
        if isinstance(row, Mapping) and str(row.get("state") or row.get("session_state") or "").upper() != "ESTABLISHED"
    ]
    if not abnormal:
        return []
    configuration_reason = bgp_configuration_fault_reason(abnormal)
    return [
        _evidence(
            call_id,
            suffix=device,
            entity_type="device",
            entity_id=device,
            category="bgp_neighbor_state",
            value={"neighbors": abnormal, "healthy": False},
            tool="get_bgp_neighbors",
            origin=EvidenceOrigin.LIVE_TELEMETRY,
            metadata={
                "semantic_family": "bgp",
                "direct_bgp_evidence": True,
                "direct_bgp_configuration_evidence": configuration_reason is not None,
                "bgp_configuration_fault_reason": configuration_reason,
                "base_tool_observation": True,
            },
        )
    ]


def _bgp_rib_evidence(args: Mapping[str, Any], payload: Mapping[str, Any], *, call_id: str) -> list[Evidence]:
    """Preserve a BGP-RIB miss as a consequence, not a policy-family claim."""
    device = str(payload.get("device") or args.get("device") or "")
    prefix = str(payload.get("prefix") or args.get("prefix") or "")
    text = str(payload.get("bgp_rib") or "")
    if not device or not prefix or not text:
        return []
    missing = bool(re.search(r"network not in table|no matching route|not found", text, re.IGNORECASE))
    return [
        _evidence(
            call_id,
            suffix=f"{device}-{prefix}-bgp-rib",
            entity_type="device",
            entity_id=device,
            category="route_presence",
            value={"prefix": prefix, "present": not missing, "protocol": "bgp"},
            tool="get_bgp_rib",
            origin=EvidenceOrigin.LIVE_TELEMETRY,
            metadata={
                "prefix": prefix,
                "missing_bgp_route": missing,
                "route_semantic_candidate": missing,
                "base_tool_observation": True,
            },
            supports_submission=False,
        )
    ]


def _event_evidence(payload: Mapping[str, Any], *, call_id: str) -> list[Evidence]:
    rows = payload.get("events")
    if not isinstance(rows, list):
        return []
    result: list[Evidence] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            continue
        device = str(row.get("device") or row.get("hostname") or "")
        if not device:
            continue
        result.append(
            _evidence(
                call_id,
                suffix=f"{device}-{index}",
                entity_type="device",
                entity_id=device,
                category="bgp_neighbor_state",
                value={**dict(row), "temporal_transition": True},
                tool="query_bgp_events",
                origin=EvidenceOrigin.LIVE_TELEMETRY,
                metadata={"temporal_transition": True, "base_tool_observation": True},
            )
        )
    return result


def _route_has_discard_hop(route: Mapping[str, Any]) -> bool:
    tokens = {str(route.get(key) or "").lower() for key in ("next_hop", "nexthop", "interface")}
    for item in route.get("nexthops") or []:
        if isinstance(item, Mapping):
            tokens.update(str(value or "").lower() for value in item.values())
        else:
            tokens.add(str(item or "").lower())
    return bool(tokens.intersection({"null0", "blackhole", "discard"}))


def _evidence(
    call_id: str,
    *,
    suffix: str,
    entity_type: str,
    entity_id: str,
    category: str,
    value: Any,
    tool: str,
    origin: EvidenceOrigin,
    metadata: Mapping[str, Any],
    supports_submission: bool = True,
    timestamp: datetime | None = None,
    independence_key: str | None = None,
) -> Evidence:
    return Evidence(
        evidence_id=f"base-tool-{call_id}-{suffix}",
        entity_type=entity_type,
        entity_id=entity_id,
        category=category,
        value=value,
        source=f"base_tool:{tool}",
        timestamp=timestamp or datetime.now(UTC),
        reliability=1.0,
        raw_reference=f"trace-tool:{call_id}",
        origin=origin,
        independence_key=independence_key or f"base-tool:{tool}:{call_id}",
        supports_submission=supports_submission,
        metadata=dict(metadata),
    )


def _deduplicate(items: Sequence[Evidence]) -> list[Evidence]:
    result: list[Evidence] = []
    seen: set[tuple[str, str, str]] = set()
    for item in items:
        key = (item.entity_id, item.category, repr(item.value))
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


__all__ = [
    "evidence_from_base_tool_observations",
    "explicit_route_policy_denies",
    "trace_step_count",
]
