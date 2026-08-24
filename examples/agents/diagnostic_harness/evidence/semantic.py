"""Convert operator-visible diagnosis prose into bounded semantic observations."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from netopsbench.sdk.agents import DiagnosisResult

from ..models import DiagnosisView, Evidence, EvidenceOrigin, FaultFamilyHint
from ..normalization.interface import TopologyIndex

_STATIC_RE = re.compile(r"\bip route\s+(?P<prefix>\S+)\s+(?P<nexthop>\S+)", re.I)
_VIA_RE = re.compile(r"\b(?:via|next[- ]?hop)\s+(?P<nexthop>\d{1,3}(?:\.\d{1,3}){3})", re.I)
_PREFIX_RE = re.compile(r"\b(?P<prefix>\d{1,3}(?:\.\d{1,3}){3}/\d{1,2})\b")
_ROUTE_TARGET = r"(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?"


def normalize_next_hop(value: object) -> str:
    """Normalize CLI punctuation around a route next-hop token.

    Text copied from a model/tool often contains a trailing quote or comma
    (for example ``Null0'``).  That punctuation is not part of the route
    object and must not change its semantic family.
    """
    return str(value or "").strip().lower().strip("'\"`.,;:()[]{}")


def has_route_policy_evidence(text: str) -> bool:
    """Return true only for an explicit public route-policy/config difference."""
    normalized = " ".join(str(text).lower().split())
    policy_difference = bool(
        re.search(
            r"\b(?:route[- ]map|prefix[- ](?:list|filter))\b.{0,120}"
            r"\b(?:missing|absent|incorrect|misconfigur\w*|den(?:y|ied|ies)|reject(?:ed|s)?)\b"
            r"|\b(?:missing|absent|incorrect|misconfigur\w*)\b.{0,80}"
            r"\b(?:route[- ]map|prefix[- ](?:list|filter))\b"
            r"|\broute policy\b.{0,100}\b(?:den(?:y|ied|ies)|reject(?:ed|s)?)\b.{0,80}"
            r"\b(?:\d{1,3}\.){3}\d{1,3}/\d{1,2}\b",
            normalized,
        )
    )
    # A bare negative such as ``no route-map filters`` is commonly a healthy
    # control-plane observation.  It is not evidence that an expected policy
    # object is missing.  Network-statement evidence must name the statement
    # directly (and preferably the affected prefix), so prose such as
    # ``No anomalies ... the network is healthy`` cannot cross the semantic
    # boundary.
    network_difference = bool(
        re.search(
            r"\b(?:missing|absent|no)\s+(?:matching\s+)?network\s+statement\b"
            r"|\b(?:missing|absent|no)\s+(?:the\s+)?['\"]?network\s+"
            rf"{_ROUTE_TARGET}\b"
            r"|\bnetwork\s+statement\b.{0,80}\b(?:missing|absent|not (?:present|configured|advertised))\b",
            normalized,
        )
        or re.search(
            rf"\bnetwork\s+{_ROUTE_TARGET}\b.{{0,120}}"
            r"\b(?:missing|absent|not (?:present|configured|advertised|in (?:the )?table))\b",
            normalized,
        )
        or re.search(
            r"\bnetwork statement\b.{0,80}\b(?:missing|absent|not (?:present|configured|advertised))\b",
            normalized,
        )
        # Real BGP configuration summaries commonly render a comparison as
        # ``network statements: <present prefixes>; MISSING <prefix>``.  The
        # word ``network`` and the missing prefix are not necessarily adjacent,
        # so accept that bounded, configuration-scoped form as well.
        or re.search(
            r"\b(?:bgp (?:config|configuration)|network statements?)\b.{0,240}"
            rf"\bmissing\s+(?:prefix\s+)?{_ROUTE_TARGET}\b",
            normalized,
        )
        # Operator summaries also describe an asymmetric configuration as
        # ``BGP config advertises <siblings> but omits network <prefix>``.
        # Bind omission verbs to an explicit network object and a bounded BGP
        # configuration context; a generic missing route/RIB consequence must
        # not become configuration evidence.
        or re.search(
            r"\b(?:bgp (?:config|configuration)|address[- ]family|network statements?)\b.{0,240}"
            r"\b(?:omit(?:s|ted)?|does not include|fails? to (?:advertise|originate|configure))\b.{0,80}"
            rf"\bnetwork\s+{_ROUTE_TARGET}\b",
            normalized,
        )
    )
    config_context = bool(
        re.search(r"\b(?:bgp|address[- ]family|running config|configuration|rib|advertis\w*)\b", normalized)
    )
    link_state_consequence = bool(
        re.search(
            r"\b(?:connected (?:interface|route)|interface)\b.{0,100}\b(?:down|missing|absent|no best path)\b"
            r"|\b(?:no|missing|without)\s+(?:the\s+)?(?:matching\s+)?connected\s+(?:interface|route)\b",
            normalized,
        )
    )
    return policy_difference or (network_difference and config_context and not link_state_consequence)


def _device_from_text(text: str, topology: TopologyIndex | None = None) -> str | None:
    if topology is not None:
        matches = [
            device
            for device in topology.routing_devices()
            if re.search(rf"(?<![\w.-]){re.escape(device)}(?![\w.-])", text, re.I)
        ]
        if matches:
            return min(matches, key=lambda device: text.lower().find(device.lower()))
    role_name = r"(?:leaf|spine|edge|agg|core)\w+"
    match = re.search(rf"\b(?:on|device)\s+(?P<device>{role_name})\b", text, re.I)
    if match:
        return match.group("device")
    # Real summaries also use the device as the grammatical subject:
    # ``leaf11 is missing ...`` or ``leaf2 config contains ...``.
    match = re.search(
        rf"\b(?P<device>{role_name})\s+"
        r"(?:is|has|config|configuration|shows?|device logs?|session|interface|Ethernet\d+)\b",
        text,
        re.I,
    )
    return match.group("device") if match else None


def _interface_from_text(text: str) -> str | None:
    match = re.search(r"\b(?P<interface>Ethernet\d+|eth\d+)\b", text, re.I)
    return match.group("interface") if match else None


def _direct_temporal_observation(text: str) -> tuple[str, str | None] | None:
    """Return a typed temporal observation only for concrete public events."""
    normalized = " ".join(text.lower().split())
    if re.search(
        r"\b(?:bgp (?:session )?flap|session_flap|established\s*(?:->|→)\s*active|"
        r"active\s*(?:->|→)\s*established|states_observed)\b",
        normalized,
    ):
        return "bgp_neighbor_state", _interface_from_text(text)
    if re.search(
        r"\b(?:went down|went up|link down|link up|oper error|mac_(?:local|remote)_fault|"
        r"fec_alignment_loss|no_rx_reachability|state transition)\b",
        normalized,
    ):
        return "syslog_event", _interface_from_text(text)
    return None


def _route_policy_prefix(text: str) -> str | None:
    patterns = (
        # Operators and network CLIs commonly describe a configuration diff as
        # ``the network statement for <prefix> is missing``.  Keep the state
        # word and prefix in the same clause so a nearby, correctly configured
        # network statement cannot be selected by accident.
        r"\bnetwork\s+statement\s+(?:for|covering)\s+['\"]?"
        rf"(?P<prefix>{_ROUTE_TARGET})\b[^;,\.\n]{{0,48}}"
        r"\b(?:is\s+)?(?:missing|absent|not (?:present|configured|advertised))\b",
        r"\b(?:missing|absent|no)\s+(?:the\s+)?(?:bgp\s+)?network\s+statement\s+"
        rf"(?:for|covering)\s+['\"]?(?P<prefix>{_ROUTE_TARGET})\b",
        r"\b(?:missing|absent|no)\s+(?:the\s+)?['\"]?(?:network\s+)?"
        rf"(?P<prefix>{_ROUTE_TARGET})\b",
        # Keep the omission verb, network object, and affected prefix in the
        # same bounded clause.  This deliberately does not accept a bare
        # ``route omitted from the RIB`` downstream symptom.
        r"\b(?:omit(?:s|ted)?|does not include|fails? to (?:advertise|originate|configure))\b"
        r".{0,80}\bnetwork\s+"
        rf"(?P<prefix>{_ROUTE_TARGET})\b",
        # Keep the state word bound to the same network object. A broad
        # wildcard used to cross a semicolon and attach ``missing`` from the
        # next prefix to the first, correctly configured statement.
        rf"\bnetwork\s+(?P<prefix>{_ROUTE_TARGET})\b[^;,.\n]{{0,48}}"
        r"\b(?:is\s+)?(?:missing|absent|not (?:present|configured|advertised))\b",
        rf"\b(?:deny|reject)\b.{{0,80}}\b(?P<prefix>{_ROUTE_TARGET})\b",
        rf"\b(?P<prefix>{_ROUTE_TARGET})\b.{{0,80}}\b(?:deny|reject)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return match.group("prefix")
    return None


def _direct_interface_down_observation(text: str) -> tuple[str, tuple[str, ...]] | None:
    """Parse only explicit canonical interface state observations.

    Diagnosis prose is allowed to seed verification, but a speculative phrase
    such as ``the link may be down`` must never become typed state Evidence.
    Require a concrete Ethernet/eth interface and an explicit admin/oper state.
    """
    interface = _interface_from_text(text)
    if interface is None:
        return None
    normalized = " ".join(text.lower().split())
    states: list[str] = []
    patterns = {
        "admin": r"\badmin(?:istrative(?:ly)?)?(?:[- ]status)?(?:\s*(?::|=|is)\s*|[-\s]+)down\b",
        "oper": r"\boper(?:ational(?:ly)?)?(?:[- ]status)?(?:\s*(?::|=|is)\s*|[-\s]+)down\b",
    }
    for state, pattern in patterns.items():
        for match in re.finditer(pattern, normalized):
            prefix = normalized[max(0, match.start() - 16) : match.start()]
            if re.search(r"\b(?:not|never|no)\s*$", prefix):
                continue
            states.append(state)
            break
    return (interface, tuple(states)) if states else None


def _semantic_excerpt(text: str) -> tuple[str | None, str | None]:
    """Recognize explicit public semantic clues without trusting labels."""
    lowered = " ".join(text.lower().split())
    if has_route_policy_evidence(lowered):
        return "route_policy", _route_policy_prefix(text)
    acl_match = re.search(r"\b(?:acls?|access[- ]lists?)\b.{0,100}\b(?:drop|deny|block|misconfigur)", lowered)
    acl_negated = bool(
        acl_match
        and (
            re.search(r"\b(?:no|without)\s+(?:active\s+)?(?:acls?|access[- ]lists?)\b", lowered)
            or re.search(
                r"\b(?:acls?|access[- ]lists?)\b.{0,60}\b(?:not|never)\b.{0,20}\b(?:drop|deny|block)",
                lowered,
            )
            or re.search(
                r"\b(?:acls?|access[- ]lists?)\b.{0,80}\bno\b.{0,24}\b(?:drop|deny|block)",
                lowered,
            )
        )
    )
    if acl_match and not acl_negated:
        match = _PREFIX_RE.search(text)
        return "acl", match.group("prefix") if match else None
    if re.search(r"\b(?:flap|flapping|up/down|down/up|state transition|session reset)\b", lowered):
        return "link_flapping", None
    return None, None


def extract_semantic_family_hints(
    result: DiagnosisResult,
    topology: TopologyIndex | None = None,
) -> tuple[FaultFamilyHint, ...]:
    """Keep specific, explicit route semantics without trusting the label/confidence."""
    hints: list[FaultFamilyHint] = []
    view = DiagnosisView.from_result(result)
    texts = (*view.evidence, str(result.reasoning)) if result.reasoning else view.evidence
    for index, excerpt in enumerate(texts, start=1):
        lowered = excerpt.lower()
        semantic_family, semantic_prefix = _semantic_excerpt(excerpt)
        if semantic_family is not None:
            device = _device_from_text(excerpt, topology)
            raw_hash = hashlib.sha256(excerpt.encode()).hexdigest()
            hints.append(
                FaultFamilyHint(
                    family=semantic_family,
                    source="base_diagnosis_text",
                    supporting_observation_ids=(f"base-text-{index}",),
                    device=device,
                    prefix=semantic_prefix,
                    route_table="default" if semantic_family in {"route_policy", "static_route"} else None,
                    raw_excerpt_hash=raw_hash,
                    reliability=0.80,
                )
            )
        match = _STATIC_RE.search(excerpt)
        route_words = "static route" in lowered or "ip route" in lowered
        invalid_words = bool(
            re.search(
                r"\b(?:wrong|incorrect|invalid|misconfigur|non[- ]gateway|non[- ]routing|"
                r"unresolv|redirect|points? to|pointing|sends? traffic|next[- ]?hop|"
                r"blackhole|null0|discard)\b",
                lowered,
            )
        )
        if route_words and invalid_words and match:
            device = _device_from_text(excerpt, topology)
            raw_hash = hashlib.sha256(excerpt.encode()).hexdigest()
            hints.append(
                FaultFamilyHint(
                    family="static_route",
                    source="base_diagnosis_text",
                    supporting_observation_ids=(f"base-text-{index}",),
                    device=device,
                    prefix=match.group("prefix"),
                    next_hop=normalize_next_hop(match.group("nexthop")),
                    route_table="default",
                    raw_excerpt_hash=raw_hash,
                    reliability=0.85,
                )
            )
    return tuple(hints)


def evidence_from_diagnosis(
    result: DiagnosisResult,
    hints: Sequence[FaultFamilyHint] = (),
    topology: TopologyIndex | None = None,
) -> list[Evidence]:
    """Preserve typed base claims without treating prose as an observation.

    The records returned here are routing hints only.  They remain visible in
    traces and may parameterize a live verification query, but
    ``supports_submission=False`` keeps them out of scoring, ranking, and
    diagnosability contracts.
    """
    evidence: list[Evidence] = []
    view = DiagnosisView.from_result(result)
    texts = (*view.evidence, str(result.reasoning)) if result.reasoning else view.evidence
    all_text = " ".join(texts).lower()
    device = str(view.device) if view.device else None
    for index, excerpt in enumerate(texts, start=1):
        semantic_family, semantic_prefix = _semantic_excerpt(excerpt)
        if semantic_family in {"route_policy", "acl"}:
            evidence.append(
                Evidence(
                    evidence_id=f"base-{semantic_family}-{index}-config",
                    entity_type="device",
                    entity_id=device or "unknown-device",
                    category="configuration_difference",
                    value={"semantic_family": semantic_family, "prefix": semantic_prefix},
                    source="base_diagnosis_text",
                    timestamp=datetime.now(UTC),
                    reliability=0.75,
                    raw_reference=f"base-text-{index}",
                    origin=EvidenceOrigin.BASE_CLAIM,
                    supports_submission=False,
                    metadata={
                        "semantic_family": semantic_family,
                        "prefix": semantic_prefix,
                        "direct_configuration_evidence": True,
                        "normalized": True,
                    },
                )
            )
        interface_down = _direct_interface_down_observation(excerpt)
        if interface_down is not None:
            interface, states = interface_down
            interface_device = _device_from_text(excerpt, topology) or device
            if interface_device:
                for state in states:
                    category = "interface_admin_state" if state == "admin" else "interface_oper_state"
                    evidence.append(
                        Evidence(
                            evidence_id=f"base-link-down-{index}-{state}",
                            entity_type="interface",
                            entity_id=f"{interface_device}:{interface}",
                            category=category,
                            value="down",
                            source="base_diagnosis_text",
                            timestamp=datetime.now(UTC),
                            reliability=0.90,
                            raw_reference=f"base-text-{index}",
                            origin=EvidenceOrigin.BASE_CLAIM,
                            supports_submission=False,
                            metadata={
                                "semantic_family": "link_state",
                                "device": interface_device,
                                "interface": interface,
                                "direct_interface_state": True,
                                "normalized": True,
                            },
                        )
                    )
        temporal = _direct_temporal_observation(excerpt)
        if temporal is not None:
            category, interface = temporal
            temporal_device = _device_from_text(excerpt, topology) or device
            entity_type = "interface" if temporal_device and interface else "device"
            entity_id = (
                f"{temporal_device}:{interface}"
                if temporal_device and interface
                else temporal_device or "unknown-device"
            )
            evidence.append(
                Evidence(
                    evidence_id=f"base-link-flapping-{index}-{category}",
                    entity_type=entity_type,
                    entity_id=entity_id,
                    category=category,
                    value={"temporal_transition": True, "observation": excerpt[:240]},
                    source="base_diagnosis_text",
                    timestamp=datetime.now(UTC),
                    reliability=0.85,
                    raw_reference=f"base-text-{index}",
                    origin=EvidenceOrigin.BASE_CLAIM,
                    supports_submission=False,
                    metadata={
                        "semantic_family": "link_flapping",
                        "temporal_transition": True,
                        "device": temporal_device,
                        "interface": interface,
                        "direct_temporal_evidence": True,
                    },
                )
            )
        match = _STATIC_RE.search(excerpt)
        if not match:
            continue
        prefix, nexthop = match.group("prefix"), normalize_next_hop(match.group("nexthop"))
        reference = f"base-text-{index}"
        metadata = {
            "semantic_family": "static_route",
            "prefix": prefix,
            "configured_next_hop": nexthop,
            "direct_configuration_evidence": True,
            "normalized": True,
        }
        evidence.append(
            Evidence(
                evidence_id=f"base-static-route-{index}-config",
                entity_type="device",
                entity_id=device or "unknown-device",
                category="configured_static_route",
                value={"prefix": prefix, "next_hop": nexthop},
                source="base_diagnosis_text",
                timestamp=datetime.now(UTC),
                reliability=0.85,
                raw_reference=reference,
                origin=EvidenceOrigin.BASE_CLAIM,
                supports_submission=False,
                metadata=metadata,
            )
        )
        lower = excerpt.lower()
        if re.search(
            r"\b(?:wrong|incorrect|invalid|misconfigur|non[- ]gateway|non[- ]routing|redirect|"
            r"points? to|pointing|sends? traffic|blackhole|null0|discard)\b",
            lower,
        ):
            evidence.append(
                Evidence(
                    evidence_id=f"base-static-route-{index}-difference",
                    entity_type="device",
                    entity_id=device or "unknown-device",
                    category="unexpected_next_hop",
                    value={"prefix": prefix, "configured_next_hop": nexthop, "reason": "explicit_public_observation"},
                    source="base_diagnosis_text",
                    timestamp=datetime.now(UTC),
                    reliability=0.85,
                    raw_reference=reference,
                    origin=EvidenceOrigin.BASE_CLAIM,
                    supports_submission=False,
                    metadata=metadata,
                )
            )
        if re.search(r"\b(?:route table|selected|installed|via)\b", lower):
            via = _VIA_RE.search(excerpt)
            evidence.append(
                Evidence(
                    evidence_id=f"base-static-route-{index}-observed",
                    entity_type="device",
                    entity_id=device or "unknown-device",
                    category="observed_routing_entry",
                    value={"prefix": prefix, "observed_next_hop": via.group("nexthop") if via else nexthop},
                    source="base_diagnosis_text",
                    timestamp=datetime.now(UTC),
                    reliability=0.8,
                    raw_reference=reference,
                    origin=EvidenceOrigin.BASE_CLAIM,
                    supports_submission=False,
                    metadata=metadata,
                )
            )
        if re.search(r"\b(?:ping|loss|unreachable|redirect|blackhole|fails?)\b", lower):
            evidence.append(
                Evidence(
                    evidence_id=f"base-static-route-{index}-consequence",
                    entity_type="path",
                    entity_id=f"{device or 'unknown'}:{prefix}",
                    category="route_reachability_consequence",
                    value={"prefix": prefix, "observed": "reachability_degraded"},
                    source="base_diagnosis_text",
                    timestamp=datetime.now(UTC),
                    reliability=0.75,
                    raw_reference=reference,
                    origin=EvidenceOrigin.BASE_CLAIM,
                    supports_submission=False,
                    metadata=metadata,
                )
            )
    # Route-table and reachability statements are often separate bullets from
    # the ``ip route`` line; bind them to the same parsed prefix.
    route_evidence = [item for item in evidence if item.category == "configured_static_route"]
    for item in route_evidence:
        value = item.value if isinstance(item.value, Mapping) else {}
        prefix = value.get("prefix")
        if re.search(r"\b(?:route table|selected|installed|via)\b", all_text):
            evidence.append(
                Evidence(
                    evidence_id=f"{item.evidence_id}-observed",
                    entity_type="device",
                    entity_id=item.entity_id,
                    category="observed_routing_entry",
                    value={"prefix": prefix, "observed_next_hop": value.get("next_hop")},
                    source="base_diagnosis_text",
                    timestamp=datetime.now(UTC),
                    reliability=0.8,
                    raw_reference=item.raw_reference,
                    origin=EvidenceOrigin.BASE_CLAIM,
                    supports_submission=False,
                    metadata=dict(item.metadata),
                )
            )
        if re.search(r"\b(?:ping|loss|unreachable|redirect|blackhole|fails?)\b", all_text):
            evidence.append(
                Evidence(
                    evidence_id=f"{item.evidence_id}-consequence",
                    entity_type="path",
                    entity_id=f"{item.entity_id}:{prefix}",
                    category="route_reachability_consequence",
                    value={"prefix": prefix, "observed": "reachability_degraded"},
                    source="base_diagnosis_text",
                    timestamp=datetime.now(UTC),
                    reliability=0.75,
                    raw_reference=item.raw_reference,
                    origin=EvidenceOrigin.BASE_CLAIM,
                    supports_submission=False,
                    metadata=dict(item.metadata),
                )
            )
    return evidence


__all__ = [
    "evidence_from_diagnosis",
    "extract_semantic_family_hints",
    "has_route_policy_evidence",
    "normalize_next_hop",
]
