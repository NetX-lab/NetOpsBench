"""Real toolkit invocation, parsing, pair selection, and shared budgets."""

from __future__ import annotations

import asyncio
import inspect
import math
import re
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import fmean
from typing import Any

from ..config import AdaptiveBudgetConfig, BudgetConfig
from ..context_topology import load_context_manifest
from ..models import Evidence, EvidenceOrigin, PingObservation, ProbePair
from ..topology.semantics import attachment_from_metadata

_PACKET_SUMMARY_RE = re.compile(
    r"(?P<sent>\d+)\s+packets transmitted,\s+(?P<received>\d+)\s+(?:packets )?received",
    re.IGNORECASE,
)
_LOSS_RE = re.compile(r"(?P<loss>[\d.]+)%\s+packet loss", re.IGNORECASE)
_RTT_SAMPLE_RE = re.compile(r"\btime[=<](?P<rtt>[\d.]+)\s*ms", re.IGNORECASE)
_RTT_SUMMARY_RE = re.compile(
    r"(?:rtt|round-trip) min/avg/max/(?:mdev|stddev)\s*=\s*"
    r"(?P<minimum>[\d.]+)/(?P<average>[\d.]+)/(?P<maximum>[\d.]+)/(?P<mdev>[\d.]+)\s*ms",
    re.IGNORECASE,
)


class ProbeBudgetExhausted(RuntimeError):
    pass


@dataclass
class ProbeBudget:
    config: BudgetConfig
    adaptive_config: AdaptiveBudgetConfig | None = None
    tool_calls: int = 0
    active_probes: int = 0
    probe_packets: int = 0
    stage: str = "family_probe"
    family: str | None = None
    reserved_by_family: dict[str, int] | None = None
    spent_by_stage: dict[str, int] | None = None
    stage_caps: dict[str, int] | None = None
    tool_call_limit: int | None = None
    active_probe_limit: int | None = None
    probe_packet_limit: int | None = None
    escalation_events: list[dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        self.reserved_by_family = dict(self.reserved_by_family or {})
        self.spent_by_stage = dict(self.spent_by_stage or {})
        self.stage_caps = dict(self.stage_caps or {})
        self.tool_call_limit = self.config.max_extra_tool_calls_per_hard_case
        self.active_probe_limit = self.config.max_active_probes_per_case
        self.probe_packet_limit = self.config.max_probe_packets_per_case
        self.escalation_events = list(self.escalation_events or [])

    @property
    def remaining_tool_calls(self) -> int:
        return max(0, int(self.tool_call_limit or 0) - self.tool_calls)

    @property
    def remaining_stage_tool_calls(self) -> int:
        """Return calls usable by the current stage after caps/reserves.

        Planning against the global remainder can over-schedule work that the
        stage allocator will correctly reject.  Exposing the effective value
        keeps planners and the enforcement boundary on the same contract.
        """
        available = int(self.tool_call_limit or 0) - self._protected_calls() - self.tool_calls
        cap = (self.stage_caps or {}).get(self.stage)
        if cap is not None:
            available = min(available, cap - (self.spent_by_stage or {}).get(self.stage, 0))
        return max(0, available)

    @property
    def remaining_active_probes(self) -> int:
        return max(0, int(self.active_probe_limit or 0) - self.active_probes)

    @property
    def remaining_probe_packets(self) -> int:
        return max(0, int(self.probe_packet_limit or 0) - self.probe_packets)

    def grant_escalation(
        self,
        *,
        reason: str,
        tool_calls: int = 0,
        active_probes: int = 0,
        probe_packets: int = 0,
        stage: str | None = None,
    ) -> dict[str, int]:
        """Grant a bounded, traceable extension without weakening any gate."""
        config = self.adaptive_config
        empty = {"tool_calls": 0, "active_probes": 0, "probe_packets": 0}
        if config is None or not config.enabled:
            return empty

        base_tools = self.config.max_extra_tool_calls_per_hard_case
        base_probes = self.config.max_active_probes_per_case
        base_packets = self.config.max_probe_packets_per_case
        tool_ceiling = base_tools + max(0, int(config.max_extra_tool_calls))
        probe_ceiling = base_probes + max(0, int(config.max_extra_active_probes))
        packet_ceiling = base_packets + max(0, int(config.max_extra_probe_packets))

        current_tools = int(self.tool_call_limit or base_tools)
        current_probes = int(self.active_probe_limit or base_probes)
        current_packets = int(self.probe_packet_limit or base_packets)
        granted = {
            "tool_calls": min(max(0, int(tool_calls)), max(0, tool_ceiling - current_tools)),
            "active_probes": min(max(0, int(active_probes)), max(0, probe_ceiling - current_probes)),
            "probe_packets": min(max(0, int(probe_packets)), max(0, packet_ceiling - current_packets)),
        }
        if not any(granted.values()):
            return granted

        self.tool_call_limit = current_tools + granted["tool_calls"]
        self.active_probe_limit = current_probes + granted["active_probes"]
        self.probe_packet_limit = current_packets + granted["probe_packets"]
        if stage and granted["tool_calls"]:
            assert self.stage_caps is not None
            current_cap = self.stage_caps.get(stage)
            if current_cap is not None:
                self.stage_caps[stage] = current_cap + granted["tool_calls"]
        assert self.escalation_events is not None
        self.escalation_events.append(
            {
                "reason": reason,
                "stage": stage,
                "granted": dict(granted),
                "effective_limits": {
                    "tool_calls": self.tool_call_limit,
                    "active_probes": self.active_probe_limit,
                    "probe_packets": self.probe_packet_limit,
                },
            }
        )
        return granted

    def configure_stages(
        self,
        *,
        family: str | None,
        family_probe_reserve: int = 0,
        evidence_collection_reserve: int = 0,
        final_verification_reserve: int = 0,
        triage_cap: int | None = None,
        semantic_closure_cap: int | None = None,
    ) -> None:
        total = self.config.max_extra_tool_calls_per_hard_case
        family_probe_reserve = max(0, min(int(family_probe_reserve), total))
        evidence_collection_reserve = max(
            0,
            min(int(evidence_collection_reserve), total - family_probe_reserve),
        )
        final_verification_reserve = max(
            0,
            min(
                int(final_verification_reserve),
                total - family_probe_reserve - evidence_collection_reserve,
            ),
        )
        self.family = family
        self.reserved_by_family = {
            "family_probe": family_probe_reserve,
            "evidence_collection": evidence_collection_reserve,
            "final_verification": final_verification_reserve,
        }
        self.stage_caps = {
            key: value
            for key, value in {
                "triage": triage_cap,
                "semantic_closure": semantic_closure_cap,
            }.items()
            if value is not None
        }

    @contextmanager
    def use_stage(self, stage: str):
        previous = self.stage
        self.stage = stage
        try:
            yield self
        finally:
            self.stage = previous

    def _protected_calls(self) -> int:
        reservations = self.reserved_by_family or {}
        spent = self.spent_by_stage or {}
        if self.stage in {"triage", "semantic_closure"}:
            protected = ("family_probe", "evidence_collection", "final_verification")
        elif self.stage == "family_probe":
            protected = ("evidence_collection", "final_verification")
        elif self.stage == "evidence_collection":
            protected = ("final_verification",)
        else:
            protected = ()
        return sum(max(0, reservations.get(item, 0) - spent.get(item, 0)) for item in protected)

    def _stage_allows_call(self) -> bool:
        cap = (self.stage_caps or {}).get(self.stage)
        if cap is not None and (self.spent_by_stage or {}).get(self.stage, 0) >= cap:
            return False
        available_after_reserve = int(self.tool_call_limit or 0) - self._protected_calls()
        return self.tool_calls < available_after_reserve

    def reserve_probe(self) -> None:
        if self.active_probes >= int(self.active_probe_limit or 0):
            raise ProbeBudgetExhausted("active probe budget exhausted")
        self.active_probes += 1

    def reserve_invocation(self, *, packets: int = 0) -> None:
        if self.tool_calls >= int(self.tool_call_limit or 0):
            raise ProbeBudgetExhausted("tool call budget exhausted")
        if not self._stage_allows_call():
            raise ProbeBudgetExhausted(f"{self.stage} budget exhausted or reserved for a later stage")
        if self.probe_packets + packets > int(self.probe_packet_limit or 0):
            raise ProbeBudgetExhausted("probe packet budget exhausted")
        self.tool_calls += 1
        self.probe_packets += packets
        assert self.spent_by_stage is not None
        self.spent_by_stage[self.stage] = self.spent_by_stage.get(self.stage, 0) + 1

    def snapshot(self) -> dict[str, int]:
        return {
            "tool_calls": self.tool_calls,
            "active_probes": self.active_probes,
            "probe_packets": self.probe_packets,
        }

    def allocation_snapshot(self) -> dict[str, object]:
        spent = self.spent_by_stage or {}
        return {
            "budget_initial": self.config.max_extra_tool_calls_per_hard_case,
            "budget_effective": int(self.tool_call_limit or 0),
            "budget_family": self.family,
            "adaptive_escalation": list(self.escalation_events or []),
            "budget_reserved_by_family": dict(self.reserved_by_family or {}),
            "budget_spent_triage": spent.get("triage", 0),
            "budget_spent_semantic_closure": spent.get("semantic_closure", 0),
            "budget_spent_family_probe": spent.get("family_probe", 0),
            "budget_spent_evidence_collection": spent.get("evidence_collection", 0),
            "budget_spent_final_verification": spent.get("final_verification", 0),
            "budget_remaining_at_gate": max(
                0,
                int(self.tool_call_limit or 0) - self.tool_calls,
            ),
        }


@dataclass(frozen=True)
class ToolInvocation:
    success: bool
    data: Mapping[str, Any] | None = None
    error: str | None = None
    timed_out: bool = False


def parse_ping_payload(payload: Mapping[str, Any]) -> PingObservation:
    """Parse the real Linux ping output returned by AgentToolkit.ping_test."""
    output = str(payload.get("output") or "")
    requested = int(payload.get("count") or 0)
    summary = _PACKET_SUMMARY_RE.search(output)
    sent = int(summary.group("sent")) if summary else requested
    received = int(summary.group("received")) if summary else 0
    loss_match = _LOSS_RE.search(output)
    if sent:
        loss_rate = max(0.0, min(1.0, (sent - received) / sent))
    elif loss_match:
        loss_rate = min(1.0, max(0.0, float(loss_match.group("loss")) / 100.0))
    else:
        loss_rate = 1.0

    samples = tuple(float(match.group("rtt")) for match in _RTT_SAMPLE_RE.finditer(output))
    rtt_summary = _RTT_SUMMARY_RE.search(output)
    values = (
        tuple(float(rtt_summary.group(name)) for name in ("minimum", "average", "maximum", "mdev"))
        if rtt_summary
        else (None, None, None, None)
    )
    return PingObservation(
        source=str(payload.get("source") or ""),
        destination=str(payload.get("destination") or ""),
        requested_count=requested,
        sent=sent,
        received=received,
        loss_rate=loss_rate,
        payload_size=int(payload["payload_size"]) if payload.get("payload_size") is not None else None,
        dont_fragment=bool(payload.get("dont_fragment")),
        return_code=int(payload.get("return_code") or 0),
        rtt_samples_ms=samples,
        rtt_min_ms=values[0],
        rtt_avg_ms=values[1],
        rtt_max_ms=values[2],
        rtt_mdev_ms=values[3],
    )


async def invoke_tool(
    context: Any,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    budget: ProbeBudget,
    timeout_seconds: float,
    packets: int = 0,
) -> ToolInvocation:
    """Invoke a real context tool with harness accounting and timeout handling."""
    tools = getattr(context, "tools", None)
    if tools is None:
        return ToolInvocation(success=False, error="diagnostic context has no tool gateway")
    method = getattr(tools, tool_name, None)
    if not callable(method):
        return ToolInvocation(success=False, error=f"tool is unavailable: {tool_name}")
    try:
        budget.reserve_invocation(packets=packets)
    except ProbeBudgetExhausted as exc:
        return ToolInvocation(success=False, error=str(exc))

    try:
        if inspect.iscoroutinefunction(method):
            result = await asyncio.wait_for(method(**arguments), timeout=timeout_seconds)
        else:
            # AgentToolkit's synchronous connectivity methods enforce their own
            # subprocess timeouts (ping: 30s, traceroute: 12s). Calling them
            # directly also preserves SessionToolGateway's serialized counter.
            result = method(**arguments)
            if inspect.isawaitable(result):
                result = await asyncio.wait_for(result, timeout=timeout_seconds)
    except TimeoutError:
        return ToolInvocation(success=False, error=f"{tool_name} timed out", timed_out=True)
    except Exception as exc:  # noqa: BLE001 - normalize tool boundary failures
        return ToolInvocation(success=False, error=f"{tool_name} failed: {type(exc).__name__}: {exc}")

    if hasattr(result, "success"):
        if not bool(result.success):
            return ToolInvocation(success=False, error=str(result.error or f"{tool_name} failed"))
        data = result.data
    elif isinstance(result, Mapping) and "success" in result:
        if not bool(result.get("success")):
            return ToolInvocation(success=False, error=str(result.get("error") or f"{tool_name} failed"))
        data = result.get("data")
    else:
        data = result
    if not isinstance(data, Mapping):
        return ToolInvocation(success=False, error=f"{tool_name} returned a non-object payload")
    return ToolInvocation(success=True, data=data)


async def invoke_ping(
    context: Any,
    *,
    source: str,
    destination: str,
    count: int,
    budget: ProbeBudget,
    timeout_seconds: float,
    payload_size: int | None = None,
    dont_fragment: bool = False,
    source_interface: str | None = None,
) -> tuple[PingObservation | None, str | None]:
    safe_count = max(1, min(int(count), 20))
    arguments = {
        "src": source,
        "dst_ip": destination,
        "count": safe_count,
        "payload_size": payload_size,
        "dont_fragment": dont_fragment,
    }
    if source_interface is not None:
        arguments["source_interface"] = source_interface
    return await _invoke_ping_tool(
        context,
        "ping_test",
        arguments,
        budget=budget,
        timeout_seconds=timeout_seconds,
        packets=safe_count,
        payload_name="ping",
    )


async def invoke_link_ping(
    context: Any,
    *,
    source: str,
    target_device: str,
    source_interface: str,
    target_interface: str,
    count: int,
    budget: ProbeBudget,
    timeout_seconds: float,
    payload_size: int | None = None,
    dont_fragment: bool = False,
) -> tuple[PingObservation | None, str | None]:
    """Invoke the topology-validated single-link ping tool."""
    safe_count = max(1, min(int(count), 20))
    arguments = {
        "src": source,
        "target_device": target_device,
        "source_interface": source_interface,
        "target_interface": target_interface,
        "count": safe_count,
        "payload_size": payload_size,
    }
    if dont_fragment:
        arguments["dont_fragment"] = True
    return await _invoke_ping_tool(
        context,
        "ping_link_test",
        arguments,
        budget=budget,
        timeout_seconds=timeout_seconds,
        packets=safe_count,
        payload_name="link ping",
    )


async def _invoke_ping_tool(
    context: Any,
    tool_name: str,
    arguments: Mapping[str, Any],
    *,
    budget: ProbeBudget,
    timeout_seconds: float,
    packets: int,
    payload_name: str,
) -> tuple[PingObservation | None, str | None]:
    invocation = await invoke_tool(
        context,
        tool_name,
        arguments,
        budget=budget,
        timeout_seconds=timeout_seconds,
        packets=packets,
    )
    if not invocation.success or invocation.data is None:
        return None, invocation.error
    try:
        return parse_ping_payload(invocation.data), None
    except (TypeError, ValueError) as exc:
        return None, f"invalid {payload_name} payload: {exc}"


def tool_error_evidence(
    *,
    evidence_id: str,
    probe_id: str,
    entity_id: str,
    error: str,
    source: str,
) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        entity_type="path",
        entity_id=entity_id,
        category="tool_error",
        value={"error": error},
        source=source,
        timestamp=datetime.now(UTC),
        reliability=0.0,
        probe_id=probe_id,
        origin=EvidenceOrigin.UNKNOWN,
        supports_submission=False,
    )


def select_probe_pairs(
    context: Any,
    *,
    family: str,
    max_pairs: int,
    evidence: Sequence[Evidence] = (),
    anomaly_pairs: int | None = None,
    control_pairs: int = 0,
    link_isolation: bool = False,
    packets_per_round: int | None = None,
    repeat_rounds: int | None = None,
    control_repeat_rounds: int | None = None,
    link_breadth_packets_per_round: int = 20,
    link_breadth_repeat_rounds: int = 1,
    diverse_endpoint_pairs: bool = False,
    stratified_attachment_coverage: bool = False,
    covered_attachment_domains: Sequence[str] = (),
    preferred_leaves: Sequence[str] = (),
) -> list[ProbePair]:
    """Select discriminative endpoints from public observations, then inventory."""
    if stratified_attachment_coverage:
        coverage_pairs = _stratified_attachment_pairs(
            context,
            max_pairs=max_pairs,
            covered_attachment_domains=covered_attachment_domains,
            preferred_leaves=preferred_leaves,
        )
        if coverage_pairs or max_pairs <= 0:
            return coverage_pairs

    symptoms = getattr(context, "symptoms", {}) or {}
    observations = symptoms.get("observations", {}) if isinstance(symptoms, Mapping) else {}
    pingmesh = observations.get("pingmesh_metrics", {}) if isinstance(observations, Mapping) else {}
    anomalies = list(pingmesh.get("anomalies", [])) if isinstance(pingmesh, Mapping) else []

    # Runtime/replay contexts do not always retain the original Pingmesh
    # anomaly block.  The Evidence Store is the canonical observation surface
    # after routing, so recover the same endpoint fields from it instead of
    # falling back to arbitrary inventory pairs.  Only typed live observations
    # are admitted; base prose and tool errors can never select a probe path.
    category_types = {
        "packet_loss_rate": "packet_loss",
        "latency_median": "latency",
        "latency_p95": "latency",
        "packet_size_threshold": "mtu",
        "payload_integrity_failure": "packet_loss",
    }
    for item in evidence:
        if item.origin is EvidenceOrigin.BASE_CLAIM or not item.supports_submission or item.reliability <= 0:
            continue
        anomaly_type = category_types.get(item.category)
        if anomaly_type is None:
            continue
        metadata = item.metadata if isinstance(item.metadata, Mapping) else {}
        source = str(metadata.get("src_name") or metadata.get("source") or "").strip()
        destination = str(metadata.get("dst_ip") or metadata.get("destination_ip") or "").strip()
        source_attachment = attachment_from_metadata(metadata, "source")
        destination_attachment = attachment_from_metadata(metadata, "destination")
        if (not source or not destination) and not (source_attachment and destination_attachment):
            continue
        anomalies.append(
            {
                "type": anomaly_type,
                "src_name": source,
                "dst_name": metadata.get("dst_name") or metadata.get("destination_name"),
                "dst_ip": destination,
                # Pingmesh retains these field names for wire compatibility;
                # their value is the client attachment switch (leaf or edge).
                "src_leaf": source_attachment,
                "dst_leaf": destination_attachment,
                "src_attachment": source_attachment,
                "dst_attachment": destination_attachment,
                "value": item.value if isinstance(item.value, (int, float)) else metadata.get("observed_value", 0),
                "baseline": metadata.get("baseline"),
                "threshold": metadata.get("threshold"),
                "severity": metadata.get("severity", "high"),
                "persistence": metadata.get("persistence", "persistent"),
                "evidence_id": item.evidence_id,
            }
        )

    def family_match(item: Mapping[str, Any]) -> bool:
        anomaly_type = str(item.get("type") or "").lower()
        if family == "mtu":
            return "mtu" in anomaly_type or "fragment" in anomaly_type
        if family == "high_latency":
            return "latency" in anomaly_type or "rtt" in anomaly_type
        if family in {"packet_loss", "packet_corruption"}:
            return "loss" in anomaly_type or "unreachable" in anomaly_type
        return False

    severity = {"high": 3, "medium": 2, "low": 1}
    persistence = {"persistent": 3, "steady_only": 2, "early_only": 1, "full_window": 1}
    ranked = sorted(
        (item for item in anomalies if isinstance(item, Mapping) and family_match(item)),
        key=lambda item: (
            -severity.get(str(item.get("severity")), 0),
            -persistence.get(str(item.get("persistence")), 0),
            -float(item.get("value") or 0.0),
            str(item.get("src_name") or ""),
            str(item.get("dst_ip") or ""),
        ),
    )
    selected: list[ProbePair] = []
    seen: set[tuple[str, str]] = set()
    used_sources: set[str] = set()
    used_destinations: set[str] = set()
    ordered_ranked = ranked
    if diverse_endpoint_pairs:
        # A stochastic impairment may affect one client-facing edge.  Reusing
        # the same source/destination across every sample leaves most access
        # domains unobserved on a large fabric.  Greedily cover new endpoints
        # first, then retain the original anomaly ordering for any remainder.
        remaining = list(ranked)
        ordered_ranked = []
        while remaining:
            best_index = max(
                range(len(remaining)),
                key=lambda index: (
                    int(str(remaining[index].get("src_name") or "") not in used_sources)
                    + int(str(remaining[index].get("dst_name") or "") not in used_destinations),
                    -index,
                ),
            )
            item = remaining.pop(best_index)
            ordered_ranked.append(item)
            used_sources.add(str(item.get("src_name") or ""))
            used_destinations.add(str(item.get("dst_name") or ""))
        used_sources.clear()
        used_destinations.clear()
    for item in ordered_ranked:
        source = str(item.get("src_name") or "").strip()
        destination = str(item.get("dst_ip") or "").strip()
        if not source or not destination or (source, destination) in seen:
            continue
        seen.add((source, destination))
        used_sources.add(source)
        used_destinations.add(str(item.get("dst_name") or destination))
        selected.append(
            ProbePair(
                source=source,
                destination=destination,
                destination_name=str(item.get("dst_name") or "") or None,
                source_leaf=attachment_from_metadata(item, "source"),
                destination_leaf=attachment_from_metadata(item, "destination"),
                metadata={
                    "selection": "anomaly",
                    "control": False,
                    "anomaly_type": item.get("type"),
                    "observed_value": item.get("value"),
                    "baseline": item.get("baseline"),
                    "threshold": item.get("threshold"),
                    **({"evidence_id": item["evidence_id"]} if item.get("evidence_id") else {}),
                },
            )
        )
        if anomaly_pairs is not None and len(selected) >= anomaly_pairs:
            break

    if family in {"high_latency", "packet_loss"} and (control_pairs or link_isolation):
        matrix = _path_contrast_pairs(
            context,
            ranked=ranked,
            selected=selected,
            max_pairs=max_pairs,
            control_pairs=control_pairs,
            link_isolation=link_isolation,
        )
        if matrix:
            has_contrast_matrix = any(
                pair.metadata.get("selection") in {"healthy_control", "link_isolation"} for pair in matrix
            )
            if family == "packet_loss" and has_contrast_matrix:
                matrix = [
                    ProbePair(
                        source=pair.source,
                        destination=pair.destination,
                        destination_name=pair.destination_name,
                        source_leaf=pair.source_leaf,
                        destination_leaf=pair.destination_leaf,
                        metadata={
                            **pair.metadata,
                            "packets_per_round": (
                                max(1, int(link_breadth_packets_per_round))
                                if pair.metadata.get("selection") == "link_isolation"
                                else packets_per_round
                            ),
                            "repeat_rounds": (
                                control_repeat_rounds
                                if pair.metadata.get("selection") == "healthy_control"
                                else max(1, int(link_breadth_repeat_rounds))
                                if pair.metadata.get("selection") == "link_isolation"
                                else repeat_rounds
                            ),
                        },
                    )
                    for pair in matrix
                ]
            return matrix[:max_pairs]

    return selected[:max_pairs] or _fallback_inventory_pairs(
        context,
        max_pairs=max_pairs,
        preferred_leaves=preferred_leaves,
    )


def _load_context_manifest(context: Any):
    manifest, _source = load_context_manifest(context, include_context_payload=False)
    return manifest


def _path_contrast_pairs(
    context: Any,
    *,
    ranked: list[Mapping[str, Any]],
    selected: list[ProbePair],
    max_pairs: int,
    control_pairs: int,
    link_isolation: bool,
) -> list[ProbePair]:
    """Add controls and one-hop probes across a suspected attachment cut.

    The inventory supplies the adjacent fabric devices, so this works for
    leaf/spine, edge/aggregation, and future multi-tier fabrics without
    encoding role names.
    """
    manifest = _load_context_manifest(context)
    if manifest is None:
        return selected

    attachment_counts: dict[str, int] = {}
    for item in ranked:
        for side in ("source", "destination"):
            attachment = attachment_from_metadata(item, side)
            if attachment:
                attachment_counts[attachment] = attachment_counts.get(attachment, 0) + 1
    peak = max(attachment_counts.values(), default=0)
    suspect_attachment = min(
        (device for device, count in attachment_counts.items() if count == peak),
        default=None,
    )
    if suspect_attachment is None:
        return selected
    # A single anomalous flow implicates both attachment cuts equally. Probe
    # both instead of breaking the tie lexically; with stronger concentration,
    # retain the cheaper single-cut plan.
    suspect_attachments = [
        device
        for device, count in sorted(attachment_counts.items(), key=lambda item: (-item[1], item[0]))
        if count >= max(1, peak * 0.75)
    ][:2]

    clients = [client for client in manifest.clients() if client.data_ip]
    anomaly_keys = {(str(item.get("src_name") or ""), str(item.get("dst_ip") or "")) for item in ranked}
    controls: list[ProbePair] = []
    if control_pairs > 0:
        for source in clients:
            for destination in clients:
                key = (source.name, str(destination.data_ip or ""))
                if (
                    source.name == destination.name
                    or source.attached_switch == destination.attached_switch
                    or set(suspect_attachments).intersection({source.attached_switch, destination.attached_switch})
                    or key in anomaly_keys
                ):
                    continue
                controls.append(
                    ProbePair(
                        source=source.name,
                        destination=str(destination.data_ip),
                        destination_name=destination.name,
                        source_leaf=source.attached_switch,
                        destination_leaf=destination.attached_switch,
                        metadata={
                            "selection": "healthy_control",
                            "control": True,
                            "suspect_attachment": suspect_attachment,
                            "suspect_leaf": suspect_attachment,
                        },
                    )
                )
                break
            if len(controls) >= control_pairs:
                break

    isolation_by_attachment: dict[str, list[ProbePair]] = {device: [] for device in suspect_attachments}
    if link_isolation:
        for attachment in suspect_attachments:
            for link in manifest.links:
                endpoints = link.endpoints
                if attachment not in {endpoints[0].device, endpoints[1].device}:
                    continue
                suspect_endpoint = endpoints[0] if endpoints[0].device == attachment else endpoints[1]
                peer_endpoint = endpoints[1] if endpoints[0].device == attachment else endpoints[0]
                peer = peer_endpoint.device
                peer_device = manifest.device(peer)
                if peer_device is None or peer_device.role.value == "client":
                    continue
                isolation_by_attachment[attachment].append(
                    ProbePair(
                        source=peer,
                        destination=attachment,
                        destination_name=attachment,
                        source_leaf=peer,
                        destination_leaf=attachment,
                        metadata={
                            "selection": "link_isolation",
                            "control": False,
                            "suspect_attachment": attachment,
                            "suspect_leaf": attachment,
                            "candidate_attachments": tuple(suspect_attachments),
                            "candidate_leafs": tuple(suspect_attachments),
                            "link_endpoints": [endpoints[0].device, endpoints[1].device],
                            "link_probe": True,
                            "source_interface": peer_endpoint.interface,
                            "target_device": attachment,
                            "target_interface": suspect_endpoint.interface,
                        },
                    )
                )

    isolation: list[ProbePair] = []
    capacity = max(0, max_pairs - min(control_pairs, len(controls)))
    while len(isolation) < capacity and any(isolation_by_attachment.values()):
        progressed = False
        for attachment in suspect_attachments:
            if isolation_by_attachment[attachment] and len(isolation) < capacity:
                isolation.append(isolation_by_attachment[attachment].pop(0))
                progressed = True
        if not progressed:
            break

    # Preserve room for every isolating neighbor, then for controls. Any spare
    # slots remain available to public anomalous client pairs.
    reserved = len(isolation) + len(controls[:control_pairs])
    anomaly_slots = max(0, max_pairs - reserved)
    return [*selected[:anomaly_slots], *controls[:control_pairs], *isolation]


def _fallback_inventory_pairs(
    context: Any,
    *,
    max_pairs: int,
    preferred_leaves: Sequence[str] = (),
) -> list[ProbePair]:
    manifest = _load_context_manifest(context)
    if manifest is None:
        return []
    clients = [client for client in manifest.clients() if client.data_ip]
    preferred = {str(item) for item in preferred_leaves if item}
    if preferred:
        # When passive telemetry has no differentiating symptom (notably
        # corruption), use the base agent's *read-only investigation scope*
        # solely to schedule real probes.  It is never fault evidence. Cover
        # distinct access edges on those leaves against controls elsewhere.
        focused = [client for client in clients if str(client.attached_switch or "") in preferred]
        controls = [client for client in clients if str(client.attached_switch or "") not in preferred]
        focused_pairs: list[ProbePair] = []
        for index, source in enumerate(focused):
            if not controls:
                break
            destination = controls[index % len(controls)]
            focused_pairs.append(
                ProbePair(
                    source=source.name,
                    destination=str(destination.data_ip),
                    destination_name=destination.name,
                    source_leaf=source.attached_switch,
                    destination_leaf=destination.attached_switch,
                    metadata={
                        "selection": "investigation_scope",
                        "coverage": "focused_access_edge",
                    },
                )
            )
            if len(focused_pairs) >= max_pairs:
                return focused_pairs
        if focused_pairs:
            # Preserve the focused probes first and fill any spare slots with
            # topology-diverse sentinels below.
            max_pairs -= len(focused_pairs)
        else:
            focused_pairs = []
    else:
        focused_pairs = []
    # Maximize topology coverage before reusing an endpoint leaf. This matters
    # for payload-integrity sentinels: three disjoint pairs cover six leaf
    # domains, whereas the old nested loop repeatedly used the first source.
    representatives = []
    seen_leafs: set[str] = set()
    for client in clients:
        leaf = str(client.attached_switch or "")
        if not leaf or leaf in seen_leafs:
            continue
        seen_leafs.add(leaf)
        representatives.append(client)
    pairs: list[ProbePair] = list(focused_pairs)
    for index in range(0, len(representatives) - 1, 2):
        source = representatives[index]
        destination = representatives[index + 1]
        pairs.append(
            ProbePair(
                source=source.name,
                destination=str(destination.data_ip),
                destination_name=destination.name,
                source_leaf=source.attached_switch,
                destination_leaf=destination.attached_switch,
                metadata={"selection": "inventory_fallback", "coverage": "disjoint_leaf_pair"},
            )
        )
        if len(pairs) >= max_pairs + len(focused_pairs):
            return pairs
    for source in clients:
        for destination in clients:
            if source.name == destination.name or source.attached_switch == destination.attached_switch:
                continue
            if any(pair.source == source.name and pair.destination == str(destination.data_ip) for pair in pairs):
                continue
            pairs.append(
                ProbePair(
                    source=source.name,
                    destination=str(destination.data_ip),
                    destination_name=destination.name,
                    source_leaf=source.attached_switch,
                    destination_leaf=destination.attached_switch,
                    metadata={"selection": "inventory_fallback"},
                )
            )
            if len(pairs) >= max_pairs + len(focused_pairs):
                return pairs
    return pairs


def _stratified_attachment_pairs(
    context: Any,
    *,
    max_pairs: int,
    covered_attachment_domains: Sequence[str] = (),
    preferred_leaves: Sequence[str] = (),
) -> list[ProbePair]:
    """Choose sentinels that maximize distinct attachment-domain coverage.

    Healthy verification certifies a bounded topology scope, so two different
    client flows on the same attachment pair are not independent coverage.
    Select one representative client per attachment and pair disjoint
    attachments first. On an odd-sized topology, the final attachment reuses
    one already covered peer because no fully disjoint pair is possible.
    """
    manifest = _load_context_manifest(context)
    if manifest is None or max_pairs <= 0:
        return []
    representatives: dict[str, Any] = {}
    for client in manifest.clients():
        attachment = str(client.attached_switch or "")
        if attachment and client.data_ip and attachment not in representatives:
            representatives[attachment] = client
    preferred = {str(item) for item in preferred_leaves if item}
    already_covered = {str(item) for item in covered_attachment_domains if item}
    attachments = sorted(
        representatives,
        key=lambda item: (item in already_covered, item not in preferred, item),
    )
    if len(attachments) < 2:
        return []

    pairs: list[ProbePair] = []
    for index in range(0, len(attachments) - 1, 2):
        source_attachment = attachments[index]
        destination_attachment = attachments[index + 1]
        source = representatives[source_attachment]
        destination = representatives[destination_attachment]
        pairs.append(
            ProbePair(
                source=source.name,
                destination=str(destination.data_ip),
                destination_name=destination.name,
                source_leaf=source_attachment,
                destination_leaf=destination_attachment,
                metadata={
                    "selection": "stratified_attachment_coverage",
                    "coverage": "disjoint_attachment_pair",
                },
            )
        )
        if len(pairs) >= max_pairs:
            return pairs

    if len(attachments) % 2 and len(pairs) < max_pairs:
        source_attachment = attachments[-1]
        destination_attachment = attachments[0]
        source = representatives[source_attachment]
        destination = representatives[destination_attachment]
        pairs.append(
            ProbePair(
                source=source.name,
                destination=str(destination.data_ip),
                destination_name=destination.name,
                source_leaf=source_attachment,
                destination_leaf=destination_attachment,
                metadata={
                    "selection": "stratified_attachment_coverage",
                    "coverage": "odd_attachment_remainder",
                },
            )
        )
    return pairs


def percentile_nearest_rank(samples: tuple[float, ...], percentile: float) -> float | None:
    if not samples:
        return None
    ordered = sorted(samples)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[min(len(ordered) - 1, rank - 1)]


def sample_mean(samples: tuple[float, ...]) -> float | None:
    return fmean(samples) if samples else None


__all__ = [
    "ProbeBudget",
    "ProbeBudgetExhausted",
    "ToolInvocation",
    "invoke_ping",
    "invoke_tool",
    "parse_ping_payload",
    "percentile_nearest_rank",
    "sample_mean",
    "select_probe_pairs",
    "tool_error_evidence",
]
