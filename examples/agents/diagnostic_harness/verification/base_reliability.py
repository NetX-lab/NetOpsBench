"""Typed base-agent reliability and direct-evidence sufficiency checks."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence

from netopsbench.sdk.agents import DiagnosisResult

from ..evidence.semantic import has_route_policy_evidence
from ..evidence.store import EvidenceStore
from ..evidence.validator import can_support_fault
from ..models import BaseAgentAssessment, BaseAgentStatus, DiagnosisView, Evidence


def _text(result: DiagnosisResult) -> str:
    view = DiagnosisView.from_result(result)
    return " ".join((*view.evidence, result.reasoning or "")).lower()


def _runtime_failure_metadata(metadata: Mapping[str, object]) -> tuple[str, str, bool]:
    """Return structured base-agent failure fields.

    Network diagnoses routinely contain phrases such as ``ping timed out`` or
    ``all traceroute hops time out``.  Those are network observations, not
    evidence that the provider or agent runtime failed.  Runtime
    classification therefore uses only fields emitted by the containment and
    lifecycle layers.
    """
    error_type = str(metadata.get("error_type") or "").lower()
    failure_stage = str(metadata.get("agent_failure_stage") or "").lower()
    contained = bool(metadata.get("base_exception_contained"))
    return error_type, failure_stage, contained


def _structured(store: EvidenceStore | None, categories: set[str]) -> list[Evidence]:
    if store is None:
        return []
    return [item for item in store.all() if item.category in categories and can_support_fault(item)]


def _down_interface(store: EvidenceStore | None) -> bool:
    for item in _structured(store, {"interface_admin_state", "interface_oper_state"}):
        value = item.value
        if isinstance(value, Mapping):
            value = value.get("admin") if item.category == "interface_admin_state" else value.get("oper")
        if str(value).lower() in {"down", "false", "0", "x"} or value is False:
            return True
    return False


def _has_selected_blackhole_route(text: str) -> bool:
    route_match = re.search(
        r"\b(?:static route|ip route|discard route|blackhole route)\b.{0,180}\b(?:null0|blackhole|discard)\b"
        r"|\b(?:discard|blackhole) route\b",
        text,
    )
    if route_match is None:
        return False
    prefix = text[max(0, route_match.start() - 32) : route_match.start()]
    if re.search(r"\b(?:no|without)\b", prefix):
        return False
    return bool(re.search(r"\b(?:config|contains?|has|shows?|selected|installed|route table|fib)\b", text))


def _has_invalid_static_next_hop(text: str) -> bool:
    has_route_object = bool(re.search(r"\b(?:static route|ip route)\b", text))
    has_invalid_endpoint = bool(
        re.search(
            r"\b(?:next[- ]?hop|nexthop)\b.{0,120}"
            r"\b(?:non[- ]routing|non[- ]router|client\d*|end host|endpoint)\b",
            text,
        )
    )
    is_observed = bool(re.search(r"\b(?:config|selected|installed|route table|fib|via)\b", text))
    return has_route_object and has_invalid_endpoint and is_observed


def has_link_down_evidence(text: str) -> bool:
    """Recognize an explicit public interface-down observation.

    Real toolkit summaries commonly render states as ``oper:down``,
    ``admin=down``, or ``admin status to down`` and identify the port by its
    canonical name rather than the literal word ``interface``.  Require both
    a concrete interface/port object and an observed down state so speculative
    prose such as "the link may be down" cannot enter the fast path.
    """
    normalized = " ".join(text.lower().split())
    interface_object = r"(?:\b(?:interface|link|port)\b|\b(?:ethernet|eth)\d+\b)"
    down_state = (
        r"(?:admin(?:istrative(?:ly)?)?(?:\s+status)?|oper(?:ational(?:ly)?)?)"
        r"\s*(?::|=|\bis\b|\bto\b)?\s*down\b"
    )
    return bool(re.search(rf"{interface_object}.{{0,100}}{down_state}", normalized))


def has_direct_evidence(result: DiagnosisResult, store: EvidenceStore | None = None) -> bool:
    """Require a concrete observation for fast path, not merely a diagnosis label."""
    view = DiagnosisView.from_result(result)
    fault_type = view.fault_type
    text = " ".join((*view.evidence, result.reasoning or "")).lower()
    if result.verdict == "network_healthy":
        return bool(
            view.evidence
            and re.search(r"\b(?:healthy|normal|no (?:persistent )?(?:fault|anomal|loss|latency))\b", text)
            and re.search(r"\b(?:connect|ping|bgp|route|interface|path|telemetry|observation)\w*\b", text)
        )
    if result.verdict != "fault_detected" or not fault_type:
        return False
    if fault_type in {"packet_loss", "packet_corruption", "mtu_mismatch", "high_latency"}:
        # These always take the active hard path even if the prose is strong.
        return bool(view.evidence)
    if fault_type == "route_policy_misconfig":
        structured = _structured(store, {"configuration_difference", "route_presence", "bgp_neighbor_state"})
        structured_policy = any(item.metadata.get("semantic_family") == "route_policy" for item in structured)
        return structured_policy or has_route_policy_evidence(text)
    if fault_type == "static_route_misconfig":
        return _has_invalid_static_next_hop(text) or bool(
            re.search(
                r"\bstatic (?:route|next hop)\b.{0,120}"
                r"\b(?:missing|unresolv|invalid|incorrect|differs?|blackhole|no next)\b",
                text,
            )
        )
    if fault_type == "blackhole_route":
        # A narrative such as "consistent with a blackhole" is a hypothesis,
        # not a selected route observation. Require a concrete route object
        # and a public config/RIB/forwarding qualifier.
        return _has_selected_blackhole_route(text)
    if fault_type == "bgp_neighbor_misconfig":
        structured_bgp = _structured(store, {"bgp_neighbor_state"})
        direct_bgp = any(
            item.metadata.get("direct_bgp_evidence")
            or (
                isinstance(item.value, Mapping)
                and item.value.get("healthy") is False
                and any(
                    str(row.get("state") or row.get("session_state") or "").upper() != "ESTABLISHED"
                    for row in item.value.get("neighbors", ())
                    if isinstance(row, Mapping)
                )
            )
            for item in structured_bgp
        )
        policy_conflict_evidence = has_route_policy_evidence(text)
        return (
            direct_bgp
            or bool(
                re.search(r"\b(?:bgp\s+)?(?:neighbor|peer)\b", text)
                and re.search(
                    r"\b(?:idle|active|down|mismatch|incorrect|missing|not established|bad peer as|remote-as)\b",
                    text,
                )
            )
            or policy_conflict_evidence
        )
    if fault_type == "acl_misconfig":
        return bool(re.search(r"\b(?:acl|access[- ]list)\b.{0,120}\b(?:deny|drop|block|incorrect|misconfig)", text))
    if fault_type == "link_down":
        return _down_interface(store) or has_link_down_evidence(text)
    if fault_type == "link_flapping":
        return bool(
            re.search(r"\b(?:flap|transition|state changes?|up/down|down/up|session reset|states? observed)\b", text)
        ) or bool(_structured(store, {"syslog_event"}))
    if fault_type == "device_down":
        return bool(
            re.search(
                r"\b(?:device|leaf\d*|spine\d*|edge\d*|agg\d*|core\d*|switch\d*|node)\b"
                r".{0,60}\b(?:down|unreachable|offline)\b",
                text,
            )
        )
    return bool(view.evidence)


def has_semantic_conflict(result: DiagnosisResult, store: EvidenceStore | None = None) -> bool:
    fault_type = DiagnosisView.from_result(result).fault_type
    text = _text(result).lower()
    if fault_type == "link_down" and re.search(
        r"\b(?:device|leaf\d*|spine\d*|edge\d*|agg\d*|core\d*|switch\d*|node)\b.{0,80}"
        r"\b(?:crash(?:ed)?|offline|powered\s+off|not\s+running|unavailable|"
        r"device\s+down|entire\s+device\s+unreachable)\b",
        text,
    ):
        # The prose is only a routing veto. Device-down still requires live,
        # multi-link operational evidence before it can be submitted.
        return True
    if result.verdict == "network_healthy":
        if store is None:
            return False
        for item in store.all():
            if not can_support_fault(item):
                continue
            if item.category == "packet_loss_rate":
                try:
                    if float(item.value) >= 0.10:
                        return True
                except (TypeError, ValueError):
                    continue
            elif item.category == "payload_integrity_failure" and bool(item.value):
                return True
            elif item.category == "packet_size_threshold":
                value = item.value if isinstance(item.value, Mapping) else {}
                if value.get("size_dependent_failure"):
                    return True
            elif item.category in {"latency_median", "latency_p95"}:
                if item.metadata.get("category_anomaly") is not None:
                    if item.metadata.get("category_anomaly"):
                        return True
                elif item.metadata.get("absolute_anomaly") or item.metadata.get("relative_anomaly"):
                    return True
                try:
                    if float(item.value) >= 30.0:
                        return True
                except (TypeError, ValueError):
                    continue
            elif item.category in {"interface_admin_state", "interface_oper_state"}:
                value = item.value
                if isinstance(value, Mapping):
                    value = value.get("admin") if item.category == "interface_admin_state" else value.get("oper")
                if str(value).lower() in {"down", "false", "0", "x"} or value is False:
                    return True
            elif item.category == "syslog_event":
                value = item.value if isinstance(item.value, Mapping) else {}
                if value.get("temporal_transition") or item.metadata.get("temporal_transition"):
                    return True
            elif item.category == "bgp_neighbor_state" and item.metadata.get("direct_bgp_evidence"):
                return True
        return False
    if fault_type == "route_policy_misconfig":
        if _down_interface(store) or _has_selected_blackhole_route(_text(result)):
            return True
    return False


def _repeated_action(result: DiagnosisResult) -> bool:
    metadata = result.metadata if isinstance(result.metadata, Mapping) else {}
    calls = metadata.get("tool_calls")
    if not isinstance(calls, Sequence) or isinstance(calls, (str, bytes)):
        return False
    signatures: Counter[str] = Counter()
    for item in calls:
        if not isinstance(item, Mapping):
            continue
        signature = json.dumps(
            {"tool": item.get("tool"), "args": item.get("args") or {}},
            sort_keys=True,
            default=str,
        )
        signatures[signature] += 1
    return any(count > 1 for count in signatures.values())


class BaseAgentReliability:
    """Classify the base result without turning runtime failures into fault evidence."""

    def assess(
        self,
        result: DiagnosisResult,
        *,
        evidence_store: EvidenceStore | None = None,
        normalization_errors: Sequence[str] = (),
    ) -> BaseAgentAssessment:
        metadata = result.metadata if isinstance(result.metadata, Mapping) else {}
        error_type, failure_stage, contained_failure = _runtime_failure_metadata(metadata)
        text = _text(result)
        reasons: list[str] = []

        lifecycle = metadata.get("mcp_lifecycle")
        recovery_exhausted = bool(isinstance(lifecycle, Mapping) and lifecycle.get("recovery_exhausted"))
        infrastructure_failure = error_type in {"mcperror", "mcpinfrastructureerror"} or failure_stage in {
            "tool_infrastructure",
            "infrastructure",
        }
        if recovery_exhausted or infrastructure_failure:
            return BaseAgentAssessment(
                BaseAgentStatus.TOOL_INFRASTRUCTURE_FAILED,
                ("mcp_transport_failure",),
            )
        if "recursion" in error_type or failure_stage == "recursion" or re.search(
            r"\b(?:graph )?recursion limit\b", text
        ):
            return BaseAgentAssessment(BaseAgentStatus.RECURSION_FAILED, ("graph_recursion_limit",))
        if (
            normalization_errors
            or "schema" in error_type
            or re.search(r"\b(?:invalid schema|json block missing|schema validation)\b", text)
        ):
            reasons.append("invalid_result_schema")
            return BaseAgentAssessment(BaseAgentStatus.SCHEMA_FAILED, tuple(reasons))
        runtime_failure = (
            failure_stage in {"provider", "diagnose"}
            or error_type in {"providererror", "agentruntimeerror", "timeouterror", "agenttimeouterror"}
            or (contained_failure and not failure_stage)
        )
        if runtime_failure:
            return BaseAgentAssessment(BaseAgentStatus.TOOL_LOOP_FAILED, ("provider_or_runtime_failure",))
        if result.verdict == "inconclusive" and _repeated_action(result):
            return BaseAgentAssessment(BaseAgentStatus.TOOL_LOOP_FAILED, ("repeated_action_signature",))

        direct = has_direct_evidence(result, evidence_store)
        conflict = has_semantic_conflict(result, evidence_store)
        if result.verdict == "inconclusive" and result.confidence <= 0:
            reasons.append("zero_confidence_inconclusive")
        elif result.verdict == "inconclusive":
            reasons.append("inconclusive_without_required_evidence")
        if result.verdict in {"fault_detected", "network_healthy"} and not direct:
            reasons.append("missing_required_direct_evidence")
        if conflict:
            reasons.append("public_observation_conflict")
        if reasons:
            return BaseAgentAssessment(
                BaseAgentStatus.LOW_EVIDENCE,
                tuple(reasons),
                direct_evidence=direct,
                semantic_conflict=conflict,
            )
        return BaseAgentAssessment(
            BaseAgentStatus.VALID,
            direct_evidence=direct,
            semantic_conflict=conflict,
        )


__all__ = [
    "BaseAgentReliability",
    "has_direct_evidence",
    "has_link_down_evidence",
    "has_route_policy_evidence",
    "has_semantic_conflict",
]
