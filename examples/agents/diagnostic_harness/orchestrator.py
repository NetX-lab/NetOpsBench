"""Minimal wrapper entry point for the staged diagnostic harness."""

from __future__ import annotations

import inspect
import math
import re
from dataclasses import asdict, replace
from datetime import UTC, datetime
from ipaddress import ip_address, ip_network
from typing import Any

from netopsbench.sdk.agents import DiagnosisResult

from .config import HarnessConfig
from .evidence.base_tools import evidence_from_base_tool_observations, trace_step_count
from .evidence.cache import TTLToolCache
from .evidence.observations import evidence_from_public_observations
from .evidence.reconciliation import reconcile_active_path_coverage
from .evidence.semantic import evidence_from_diagnosis, extract_semantic_family_hints
from .evidence.store import EvidenceStore
from .evidence.validator import can_plan_from, can_support_fault
from .hypotheses import DiagnosabilityGate, DiagnosisReducer, HypothesisScorer, MultiFaultAnalyzer
from .models import BaseAgentAssessment, DiagnosisView, Evidence, EvidenceOrigin, ProbeOutcome, ProbePair
from .normalization.fault_type import FaultTypeNormalizer
from .normalization.interface import TopologyIndex
from .normalization.result import ResultNormalizer
from .probes import (
    DeterministicProbePlanner,
    DirectionalLinkLatencyProbe,
    LinkPayloadIntegrityProbe,
    MTULinkSweepProbe,
    MTUPacketSizeSweepProbe,
    PayloadIntegrityProbe,
    ProbeBudget,
    RepeatedPacketLossProbe,
    RTTMatrixProbe,
    select_probe_pairs,
)
from .routing import BoundedFamilyReplanner
from .routing.hard_case_router import HardCaseRouter
from .semantic_closure import SemanticRuntimeClosure, _concentrated_route_queries
from .telemetry import CaseTraceWriter
from .topology import InterfaceRanker, PeerConsistencyCollector, TopologyGraph, TopologyScalePolicy
from .topology.path_analysis import has_unresolved_ecmp_ambiguity, scope_path_evidence
from .topology.semantics import attachment_from_metadata
from .verification.base_reliability import BaseAgentReliability
from .verification.contracts import EvidenceContractEvaluator
from .verification.operational_closure import OperationalClosure, device_down_from_link_probes
from .verification.schema_validator import FinalResultValidator
from .verification.stop_verifier import StopVerifier

_IMPAIRMENT_FAMILIES = frozenset({"packet_loss", "mtu", "high_latency", "packet_corruption"})
_OPERATIONAL_FAMILIES = frozenset(
    {"bgp_verification", "healthy_verification", "link_state_verification", "temporal_verification"}
)
_SEMANTIC_FAMILIES = frozenset({"acl", "route_policy", "static_route", "runtime_semantic"})


def _consumes_causal_family_replan(family: str | None) -> bool:
    """Return whether entering *family* consumes the one causal replan.

    ``runtime_semantic`` is a bounded exclusion/verification stage: operators
    may check whether a route/config explanation exists before testing the
    observed impairment.  Counting that check as the only causal family
    transition stranded a deliberately reserved impairment budget.  All real
    diagnosis-family transitions remain bounded by ``max_family_replans``.
    """
    return family is not None and family != "runtime_semantic"


def _loss_isolation_conflicts_with_episode(store: EvidenceStore, *, warning_threshold: float) -> bool:
    """Whether persistent end-to-end loss survived healthy one-hop controls.

    This is a discriminator trigger, not fault evidence.  It authorizes one
    bounded payload-integrity check on the topology-ranked candidate so loss
    and corruption are not conflated when ICMP link isolation is clean.
    """
    episode_loss = False
    isolated_rates: list[float] = []
    for item in store.all():
        if item.category != "packet_loss_rate" or not can_support_fault(item):
            continue
        try:
            value = float(item.value)
        except (TypeError, ValueError):
            continue
        if (item.source == "pingmesh_episode" and value >= warning_threshold) or item.metadata.get(
            "weak_performance_symptom"
        ):
            episode_loss = True
        if item.metadata.get("selection") == "link_isolation" and item.path_observation_confidence >= 1.0:
            isolated_rates.append(value)
    return episode_loss and len(isolated_rates) >= 2 and max(isolated_rates) < warning_threshold


def _has_weak_loss_symptom(store: EvidenceStore, *, minimum_rate: float = 0.02) -> bool:
    """Allow one discriminator probe without treating weak loss as proof."""
    for item in store.all():
        if (
            item.category != "packet_loss_rate"
            or item.origin is EvidenceOrigin.BASE_CLAIM
            or not can_support_fault(item)
        ):
            continue
        try:
            if float(item.value) >= minimum_rate:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _cross_validated_link_loss(
    store: EvidenceStore,
    *,
    warning_threshold: float,
) -> tuple[Evidence, ...]:
    """Promote a weak exact-link sample only after independent confirmation.

    A single bounded payload capture with missing sequences remains planning
    evidence.  When a separate repeated ping also observes material loss on
    the same physical link, the two methods cross-validate both the anomaly
    and any direction reported by the payload probe.  This keeps random one-
    sample loss below the submission boundary while closing a real evidence
    conversion gap.
    """
    strong_by_link: dict[str, Evidence] = {}
    planning_by_link: dict[str, list[Evidence]] = {}
    for item in store.all():
        if (
            item.category != "packet_loss_rate"
            or item.origin is not EvidenceOrigin.ACTIVE_PROBE
            or item.path_observation_confidence < 1.0
            or len(item.covered_links) != 1
        ):
            continue
        try:
            rate = float(item.value)
        except (TypeError, ValueError):
            continue
        if can_support_fault(item) and rate >= warning_threshold:
            strong_by_link[item.covered_links[0]] = item
        elif (
            item.source == "payload_integrity_link_test"
            and not item.supports_submission
            and int(item.metadata.get("sent") or 0) >= 40
            and int(item.metadata.get("missing_packets") or 0) >= 2
        ):
            planning_by_link.setdefault(item.covered_links[0], []).append(item)

    promoted: list[Evidence] = []
    existing = {item.evidence_id for item in store.all()}
    for link_id, planning in planning_by_link.items():
        # Two independent bounded integrity batches on one exact physical
        # link are also a valid persistence check for random loss, including
        # client access links where the network-only ping_link_test is not
        # applicable. Require material missing sequences in both batches and
        # an aggregate rate above the existing warning threshold.
        independent = {item.independence_key: item for item in planning}
        batches = list(independent.values())
        strong = strong_by_link.get(link_id)
        repeated_payload = len(batches) >= 2 and all(
            int(item.metadata.get("sent") or 0) >= 40 and int(item.metadata.get("missing_packets") or 0) >= 2
            for item in batches[:2]
        )
        total_sent = sum(int(item.metadata.get("sent") or 0) for item in batches[:2])
        total_missing = sum(int(item.metadata.get("missing_packets") or 0) for item in batches[:2])
        repeated_payload = bool(repeated_payload and total_sent > 0 and total_missing / total_sent >= warning_threshold)
        item = batches[0]
        cross_method = strong is not None and strong.independence_key != item.independence_key
        if not cross_method and not repeated_payload:
            continue
        evidence_id = f"cross-validated-link-loss:{item.evidence_id}"
        if evidence_id in existing:
            continue
        promoted.append(
            replace(
                item,
                evidence_id=evidence_id,
                source="cross_probe_link_correlation",
                reliability=(
                    min(item.reliability, strong.reliability)
                    if cross_method and strong is not None
                    else min(batch.reliability for batch in batches[:2])
                ),
                supports_submission=True,
                metadata={
                    **item.metadata,
                    "planning_only": False,
                    "cross_validated": True,
                    "derived_from": (
                        (strong.evidence_id, item.evidence_id)
                        if cross_method and strong is not None
                        else tuple(batch.evidence_id for batch in batches[:2])
                    ),
                    "validation_mode": "cross_method" if cross_method else "repeated_payload_batches",
                },
            )
        )
    return tuple(promoted)


def _has_reliable_abnormal_path_evidence(store: EvidenceStore, *, warning_threshold: float) -> bool:
    """Require a real symptom before spending any adaptive probe budget."""
    for item in store.all():
        if not can_plan_from(item) or item.origin is EvidenceOrigin.BASE_CLAIM:
            continue
        if item.category == "packet_loss_rate":
            try:
                if float(item.value) >= warning_threshold or item.metadata.get("weak_performance_symptom"):
                    return True
            except (TypeError, ValueError):
                continue
        if item.category in {"latency_median", "latency_p95"} and (
            item.metadata.get("category_anomaly")
            or item.metadata.get("absolute_anomaly")
            or item.metadata.get("relative_anomaly")
        ):
            return True
        if item.category == "packet_size_threshold" and isinstance(item.value, dict):
            if item.value.get("size_dependent_failure"):
                return True
        if item.category == "payload_integrity_failure" and bool(item.value):
            return True
    return False


def _has_nonbase_latency_observation(store: EvidenceStore) -> bool:
    """Return whether real observations justify broader latency isolation.

    A small first RTT matrix can hit only healthy ECMP members.  Preserve the
    independent episode symptom as a planning trigger, while excluding base
    prose and failed tools from escalation.
    """
    return any(
        item.category in {"latency_median", "latency_p95"}
        and item.reliability > 0
        and item.origin is not EvidenceOrigin.BASE_CLAIM
        for item in store.all()
    )


def _has_directional_latency_for_candidate(
    evidence: list[Evidence],
    candidate: Any | None,
) -> bool:
    """Whether a one-way sample has identified an anomalous link endpoint.

    A healthy exact-link sample is useful negative evidence, but it does not
    satisfy the direct-interface requirement for a latency diagnosis.  Only
    the asymmetric probe contract can name the fault-owning endpoint.
    """
    return bool(
        candidate
        and any(
            item.source.removeprefix("base_tool:") == "latency_link_test"
            and candidate.link_id in item.covered_links
            and item.path_observation_confidence >= 1.0
            and item.metadata.get("interface_direct_anomaly") is True
            and (
                item.metadata.get("fault_endpoint_device"),
                item.metadata.get("fault_endpoint_interface"),
            )
            in {
                (candidate.primary_device, candidate.primary_interface),
                (candidate.peer_device, candidate.peer_interface),
            }
            for item in evidence
        )
    )


def _select_latency_directional_candidate(
    evidence: list[Evidence],
    candidates: list[Any],
) -> Any | None:
    """Prefer a candidate with a directly observed abnormal one-hop RTT.

    End-to-end latency implicates every link on a path and can make an access
    edge tie with the actual fabric member.  A link-isolation probe between
    adjacent devices is more specific, so use it to choose the endpoint probe
    target without changing the ranker or any submission threshold.
    """
    if not candidates:
        return None
    directly_abnormal_links = {
        item.covered_links[0]
        for item in evidence
        if item.category in {"latency_median", "latency_p95"}
        and can_plan_from(item)
        and item.metadata.get("link_probe") is True
        and item.metadata.get("category_anomaly") is True
        and item.observed_path is not None
        and len(item.covered_links) == 1
        and item.path_observation_confidence >= 1.0
    }
    direct = [item for item in candidates if item.link_id in directly_abnormal_links]
    if not direct:
        return candidates[0]
    return max(
        direct,
        key=lambda item: (item.score, item.endpoint_confidence, item.link_id),
    )


def _blackhole_semantic_closure_is_admissible(
    result: DiagnosisResult,
    store: EvidenceStore,
    *,
    minimum_confidence: float,
) -> bool:
    """Require route/consequence binding before a semantic blackhole submit."""
    findings = result.findings if isinstance(result.findings, dict) else {}
    if findings.get("fault_type") != "blackhole_route":
        return True
    if result.confidence < minimum_confidence:
        return False
    routes: list[tuple[str, Evidence]] = []
    consequences: list[Evidence] = []
    live_discard_configs: list[tuple[str, Evidence]] = []
    for item in store.all():
        if not can_support_fault(item) or item.origin is EvidenceOrigin.BASE_CLAIM:
            continue
        if item.category == "route_presence" and item.source == "get_route_table":
            value = item.value if isinstance(item.value, dict) else {}
            destination = str(value.get("prefix") or value.get("destination") or item.metadata.get("prefix") or "")
            if destination and bool(value.get("is_discard")) and value.get("selected") is not False:
                routes.append((destination, item))
        if item.category == "packet_loss_rate" and item.source != "get_route_table":
            try:
                if float(item.value) >= 0.80:
                    consequences.append(item)
            except (TypeError, ValueError):
                continue
        if item.source == "get_device_config" and item.category in {
            "configured_static_route",
            "configuration_difference",
        }:
            value = item.value if isinstance(item.value, dict) else {}
            prefix = str(value.get("prefix") or item.metadata.get("prefix") or "")
            next_hop = str(value.get("next_hop") or value.get("nexthop") or "").lower()
            if prefix and next_hop in {"null0", "blackhole", "discard"}:
                live_discard_configs.append((prefix, item))
    for destination, route in routes:
        if any(
            prefix == destination and config.independence_key != route.independence_key
            for prefix, config in live_discard_configs
        ):
            return True
        for consequence in consequences:
            observed_ips = {
                str(consequence.metadata.get(key) or "") for key in ("src_ip", "dst_ip", "source", "destination")
            }
            destination_matches = destination in observed_ips
            if not destination_matches:
                try:
                    network = ip_network(destination, strict=False)
                    destination_matches = any(ip_address(observed) in network for observed in observed_ips if observed)
                except ValueError:
                    destination_matches = False
            if destination_matches and route.source != consequence.source:
                return True
        # ECMP can turn a selected discard route into persistent partial loss
        # rather than one 80-100% loss row. Accept that consequence only when
        # the same routed destination is independently affected from at least
        # three source leaves. This is a route/consequence binding rule, not a
        # relaxed diagnosis confidence or margin gate.
        affected_source_leaves: set[str] = set()
        for consequence in store.all():
            if (
                consequence.category != "packet_loss_rate"
                or consequence.source == "get_route_table"
                or not can_support_fault(consequence)
            ):
                continue
            try:
                loss_rate = float(consequence.value)
            except (TypeError, ValueError):
                continue
            if not 0.10 <= loss_rate < 0.80:
                continue
            pairs = (
                (
                    str(consequence.metadata.get("dst_ip") or ""),
                    attachment_from_metadata(consequence.metadata, "source") or "",
                ),
                (
                    str(consequence.metadata.get("src_ip") or ""),
                    attachment_from_metadata(consequence.metadata, "destination") or "",
                ),
            )
            for observed, source_leaf in pairs:
                if not observed or not source_leaf:
                    continue
                try:
                    matches = ip_address(observed) in ip_network(destination, strict=False)
                except ValueError:
                    matches = observed == destination
                if matches:
                    affected_source_leaves.add(source_leaf)
        if len(affected_source_leaves) >= 3:
            return True
    return False


def _base_agent_name(base_agent: Any) -> str:
    return str(getattr(base_agent, "name", None) or base_agent.__class__.__name__)


def _contained_base_result(
    base_agent: Any,
    *,
    error_type: str,
    stage: str,
    reasoning: str,
    **metadata_fields: Any,
) -> DiagnosisResult:
    return DiagnosisResult(
        agent_name=_base_agent_name(base_agent),
        verdict="inconclusive",
        success=False,
        findings={"fault_type": None, "location": {"device": None, "interface": None}, "evidence": []},
        confidence=0.0,
        reasoning=reasoning,
        metadata={
            "error_type": error_type,
            "agent_failure_stage": stage,
            "base_exception_contained": True,
            **metadata_fields,
        },
    )


def _contained_base_failure(base_agent: Any, error: Exception) -> DiagnosisResult:
    """Fail closed around provider-specific agent exceptions.

    Raw exception text is deliberately not copied into the diagnosis: API
    libraries occasionally include endpoint parameters or credentials in
    exception strings. A bounded error class is sufficient for routing.
    """
    error_name = type(error).__name__
    marker = f"{error_name} {error}".lower()
    if re.search(r"recursion|graphrecursion", marker):
        error_type = "GraphRecursionError"
        stage = "recursion"
        reasoning = "Base agent graph recursion limit was reached before a valid result."
    elif re.search(r"connection closed|transport closed|broken pipe|end of stream|session unavailable", marker):
        error_type = "MCPInfrastructureError"
        stage = "tool_infrastructure"
        reasoning = "Base agent infrastructure connection closed before a valid result."
    elif re.search(r"timeout|timed out|provider|api|rate.?limit", marker):
        error_type = "ProviderError"
        stage = "provider"
        reasoning = "Base agent provider error occurred before a valid result."
    else:
        error_type = "AgentRuntimeError"
        stage = "diagnose"
        reasoning = "Base agent runtime error occurred before a valid result."
    return _contained_base_result(
        base_agent,
        error_type=error_type,
        stage=stage,
        reasoning=reasoning,
    )


def _contained_base_schema_failure(base_agent: Any, value: Any) -> DiagnosisResult:
    """Turn a non-conforming return value into a bounded schema failure."""
    return _contained_base_result(
        base_agent,
        error_type="SchemaError",
        stage="schema",
        reasoning="Base agent returned an invalid schema instead of DiagnosisResult.",
        invalid_return_type=type(value).__name__,
    )


class DiagnosticHarness:
    """Wrap an existing agent while preserving an immediate rollback path."""

    def __init__(self, base_agent: Any, *, config: HarnessConfig | None = None, name: str | None = None):
        self.base_agent = base_agent
        self.config = config or HarnessConfig()
        base_name = getattr(base_agent, "name", base_agent.__class__.__name__)
        self.name = name or f"diagnostic-harness[{base_name}]"
        self._normalizer = ResultNormalizer(FaultTypeNormalizer(self.config.normalization.fault_type_aliases))
        self._validator = FinalResultValidator(self.config.normalization)
        self._router = HardCaseRouter(self.config.hard_case_router, self.config.normalization)
        self._replanner = BoundedFamilyReplanner(self.config.hard_case_router)
        self._planner = DeterministicProbePlanner()
        self._scorer = HypothesisScorer(self.config.hypothesis)
        self._gate = DiagnosabilityGate(self.config.diagnosability)
        self._multi_fault = MultiFaultAnalyzer()
        self._reducer = DiagnosisReducer(self._scorer, self._gate, self._multi_fault)
        self._stop_verifier = StopVerifier(self._validator)
        self._semantic_closure = SemanticRuntimeClosure()
        self._base_reliability = BaseAgentReliability()
        self._operational_closure = OperationalClosure(self.config.operational_verification)
        self._contracts = EvidenceContractEvaluator()
        self._scale_policy = TopologyScalePolicy(self.config.scale_policy)

    async def diagnose(self, context: Any) -> DiagnosisResult:
        if not self.config.enabled:
            original = self.base_agent.diagnose(context)
            if inspect.isawaitable(original):
                original = await original
            if not isinstance(original, DiagnosisResult):
                raise TypeError("base agent diagnose() must return DiagnosisResult")
            return original
        # Keep a cursor so only observations produced by the wrapped base
        # agent are adapted.  Later harness probes are added through their
        # normal Evidence converters and must not be double-counted.
        base_trace_start = trace_step_count(context)
        try:
            initial = self.base_agent.diagnose(context)
            if inspect.isawaitable(initial):
                initial = await initial
        except Exception as exc:  # noqa: BLE001 - contain third-party agent/provider failures
            initial = _contained_base_failure(self.base_agent, exc)
        if not isinstance(initial, DiagnosisResult):
            initial = _contained_base_schema_failure(self.base_agent, initial)
        topology = TopologyIndex.from_context(context)
        normalized = (
            self._normalizer.normalize(initial, context=context, topology=topology)
            if self.config.normalization.enabled
            else None
        )
        candidate = normalized.result if normalized is not None else initial
        normalization_errors = normalized.validation_errors if normalized is not None else ()
        semantic_hints = extract_semantic_family_hints(candidate, topology)
        base_tool_evidence = evidence_from_base_tool_observations(
            context,
            topology,
            start_index=base_trace_start,
            latency_threshold_ms=self.config.latency_probe.absolute_threshold_ms,
            latency_relative_multiplier=self.config.latency_probe.relative_multiplier_threshold,
        )
        evidence_store = EvidenceStore(
            [
                *evidence_from_public_observations(context),
                *base_tool_evidence,
                *evidence_from_diagnosis(candidate, semantic_hints, topology),
            ]
        )
        base_assessment = self._base_reliability.assess(
            candidate,
            evidence_store=evidence_store,
            normalization_errors=normalization_errors,
        )

        decision = self._router.route(
            result=candidate,
            evidence_store=evidence_store,
            normalization_errors=normalization_errors,
            base_assessment=base_assessment,
            semantic_family_hints=semantic_hints,
        )
        if not self.config.hard_case_router.enabled:
            decision = replace(decision, fast_path=True, reasons=())

        contract = self._contracts.evaluate(candidate, evidence_store.all(), require_location=True)
        if not contract.satisfied:
            decision = replace(
                decision,
                fast_path=False,
                family=self._verification_family(candidate, decision.family),
                reasons=(*decision.reasons, *(f"contract:{item}" for item in contract.missing_requirements)),
            )

        candidate = self._with_route_metadata(
            candidate, decision, base_assessment=base_assessment, semantic_hints=semantic_hints
        )
        report = self._validator.validate(candidate, topology=topology)
        if decision.fast_path and report.valid:
            return self._write_case_trace(
                context,
                candidate,
                initial=initial,
                normalized=candidate,
                decision=decision,
                evidence_store=evidence_store,
            )
        route_policy_result = self._resolve_route_policy(candidate, decision)
        if route_policy_result is not None:
            route_policy_report = self._validator.validate(route_policy_result, topology=topology)
            route_policy_contract = self._contracts.evaluate(
                route_policy_result,
                evidence_store.all(),
                require_location=True,
            )
            if route_policy_report.valid and route_policy_contract.satisfied:
                return self._write_case_trace(
                    context,
                    route_policy_result,
                    initial=initial,
                    normalized=candidate,
                    decision=decision,
                    evidence_store=evidence_store,
                )
        outcome = None
        budget = None
        probe_history: list[ProbeOutcome] = []
        attempted_families = {decision.family} if decision.family else set()
        family_replans = 0
        adaptive_escalation = {
            "eligible": False,
            "trigger": None,
            "route_candidates_considered": 0,
            "ecmp_candidates_considered": 0,
            "ecmp_candidates_probed": 0,
            "ecmp_candidate_links": [],
            "ecmp_probe_targets": [],
            "attachment_fabric_degree": 0,
            "probe_frontier_limit": self.config.adaptive_budget.max_ecmp_probe_candidates,
            "scale_policy": None,
        }
        graph = None
        scale_plan = None
        topology_error = None
        if self.config.topology_ranker.enabled or self.config.adaptive_budget.enabled:
            try:
                graph = TopologyGraph.from_context(context)
                scale_plan = self._scale_policy.plan(
                    graph,
                    adaptive=self.config.adaptive_budget,
                    ranker=self.config.topology_ranker_config,
                )
                adaptive_escalation.update(
                    attachment_fabric_degree=scale_plan.max_ecmp_width,
                    probe_frontier_limit=scale_plan.probe_frontier_limit,
                    scale_policy=scale_plan.as_metadata(),
                )
            except Exception as exc:  # noqa: BLE001 - local topology planning must fail closed
                topology_error = f"{type(exc).__name__}: {exc}"
        if decision.family in _OPERATIONAL_FAMILIES:
            budget = self._new_budget(decision.family, evidence_store=evidence_store)
            operational_result = None
            operational_missing: tuple[str, ...] = ()
            with budget.use_stage("triage"):
                if decision.family == "bgp_verification":
                    operational = await self._operational_closure.verify_bgp(
                        context,
                        base_result=candidate,
                        topology=topology,
                        evidence=evidence_store.all(),
                        budget=budget,
                    )
                elif decision.family == "healthy_verification":
                    operational = await self._operational_closure.verify_healthy(
                        context,
                        base_result=candidate,
                        topology=topology,
                        evidence=evidence_store.all(),
                        budget=budget,
                    )
                elif decision.family == "link_state_verification":
                    operational = await self._operational_closure.verify_link_state(
                        context,
                        base_result=candidate,
                        topology=topology,
                        evidence=evidence_store.all(),
                        budget=budget,
                    )
                else:
                    operational = await self._operational_closure.verify_temporal(
                        context,
                        base_result=candidate,
                        topology=topology,
                        evidence=evidence_store.all(),
                        budget=budget,
                    )
            evidence_store.add_all(operational.outcome.evidence)
            probe_history.append(operational.outcome)
            candidate = self._with_probe_metadata(candidate, operational.outcome, evidence_store, budget)
            if operational.result is not None:
                operational_result = self._with_probe_metadata(
                    operational.result,
                    operational.outcome,
                    evidence_store,
                    budget,
                )
                operational_report = self._validator.validate(operational_result, topology=topology)
                operational_contract = self._contracts.evaluate(
                    operational_result,
                    evidence_store.all(),
                    require_location=True,
                )
                if operational_report.valid and operational_contract.satisfied:
                    return self._write_case_trace(
                        context,
                        operational_result,
                        initial=initial,
                        normalized=candidate,
                        decision=decision,
                        evidence_store=evidence_store,
                        probe_history=probe_history,
                        budget=budget,
                    )
                operational_missing = operational_contract.missing_requirements
            if operational.next_family:
                next_family = self._verification_family(
                    operational.result or candidate,
                    operational.next_family,
                )
                self._configure_budget(
                    budget,
                    next_family,
                    evidence_store=evidence_store,
                )
                decision = replace(
                    decision,
                    family=next_family,
                    reasons=(*decision.reasons, f"{operational.outcome.probe_id}_{operational.status}"),
                )
                candidate = self._with_route_metadata(
                    candidate, decision, base_assessment=base_assessment, semantic_hints=semantic_hints
                )
                attempted_families.add(next_family)
                if _consumes_causal_family_replan(next_family):
                    family_replans += 1
            elif operational_result is None or operational_missing:
                replan = self._replanner.select(
                    evidence_store,
                    current_family=decision.family,
                    attempted_families=attempted_families,
                    transitions=family_replans,
                    allowed_families=_IMPAIRMENT_FAMILIES | _SEMANTIC_FAMILIES,
                )
                if replan is not None:
                    self._configure_budget(budget, replan.family, evidence_store=evidence_store)
                    decision = replace(
                        decision,
                        family=replan.family,
                        reasons=(*decision.reasons, replan.reason),
                    )
                    attempted_families.add(replan.family)
                    family_replans += 1
                    candidate = self._with_route_metadata(
                        candidate,
                        decision,
                        base_assessment=base_assessment,
                        semantic_hints=semantic_hints,
                    )
            # Operational closures must not fall through to the impairment-only
            # scorer.  They may transition only when the closure explicitly
            # selected a different, independently observable family.
            if decision.family in _OPERATIONAL_FAMILIES:
                missing = operational_missing or (f"{decision.family}_verification_incomplete",)
                inconclusive = self._with_contract_inconclusive(operational_result or candidate, missing)
                return self._write_case_trace(
                    context,
                    inconclusive,
                    initial=initial,
                    normalized=candidate,
                    decision=decision,
                    evidence_store=evidence_store,
                    probe_history=probe_history,
                    budget=budget,
                )
        if decision.family in _SEMANTIC_FAMILIES:
            budget = self._new_budget(decision.family, evidence_store=evidence_store)
            semantic_result = None
            semantic_missing: tuple[str, ...] = ()
            observable_runtime_family = (
                self._router.observable_family(evidence_store) if decision.family == "runtime_semantic" else None
            )
            reserve_partial_loss_budget = (
                observable_runtime_family == "packet_loss" and self._router.has_partial_packet_loss(evidence_store)
            )
            route_query_limit = 2
            allow_multileaf_route_queries = False
            if (
                decision.family == "runtime_semantic"
                and reserve_partial_loss_budget
                and self.config.adaptive_budget.enabled
            ):
                semantic_device_checks = 2
                route_candidates = _concentrated_route_queries(
                    evidence_store.all(),
                    topology,
                    limit=(
                        scale_plan.route_candidate_limit
                        if scale_plan is not None
                        else self.config.adaptive_budget.max_failure_domain_candidates
                    ),
                    allow_multileaf=True,
                )
                adaptive_escalation["route_candidates_considered"] = len(route_candidates)
                # Reserve two cheap endpoint ACL/config contrasts, then scan
                # the local transit failure domain.  This grows with ECMP
                # width, never with unrelated devices in the fabric.
                needed = max(
                    0,
                    len(route_candidates) + semantic_device_checks - route_query_limit,
                )
                if needed:
                    grant = budget.grant_escalation(
                        reason="unreliable_base_concentrated_route_ambiguity",
                        tool_calls=min(needed, self.config.adaptive_budget.semantic_route_extra_tool_calls),
                        stage="semantic_closure",
                    )
                    if grant["tool_calls"]:
                        route_query_limit = min(
                            len(route_candidates),
                            route_query_limit + grant["tool_calls"] - semantic_device_checks,
                        )
                        allow_multileaf_route_queries = True
                        adaptive_escalation.update(
                            eligible=True,
                            trigger="unreliable_base_concentrated_route_ambiguity",
                        )
            with budget.use_stage("semantic_closure"):
                semantic = await self._semantic_closure.inspect(
                    context,
                    base_result=candidate,
                    topology=topology,
                    evidence=evidence_store.all(),
                    budget=budget,
                    requested_family=decision.family,
                    # When public observations already identify an impairment
                    # family, inspect the highest-correlation device for an ACL or
                    # static-route semantic explanation, then reserve ten calls
                    # for the frozen loss/corruption isolation loop. Explicit ACL
                    # and static-route branches retain the normal Top-3 search.
                    # Top-2 preserves both endpoints of a symmetric outage and
                    # leaves one of the five semantic calls for direct route
                    # confirmation. This is high-recall candidate generation,
                    # not a relaxed submission gate.
                    max_candidate_devices=2,
                    max_targeted_route_queries=route_query_limit,
                    allow_multileaf_route_queries=allow_multileaf_route_queries,
                )
            evidence_store.add_all(semantic.outcome.evidence)
            probe_history.append(semantic.outcome)
            candidate = self._with_probe_metadata(candidate, semantic.outcome, evidence_store, budget)
            if semantic.result is not None:
                semantic_result = self._with_probe_metadata(
                    semantic.result,
                    semantic.outcome,
                    evidence_store,
                    budget,
                )
                semantic_report = self._validator.validate(semantic_result, topology=topology)
                semantic_admissible = _blackhole_semantic_closure_is_admissible(
                    semantic_result,
                    evidence_store,
                    minimum_confidence=self.config.diagnosability.submit_confidence,
                )
                semantic_contract = self._contracts.evaluate(
                    semantic_result,
                    evidence_store.all(),
                    require_location=True,
                )
                if semantic_report.valid and semantic_admissible and semantic_contract.satisfied:
                    return self._write_case_trace(
                        context,
                        semantic_result,
                        initial=initial,
                        normalized=candidate,
                        decision=decision,
                        evidence_store=evidence_store,
                        probe_history=probe_history,
                        budget=budget,
                    )
                semantic_missing = semantic_contract.missing_requirements
            if semantic_result is None or semantic_missing:
                replan = self._replanner.select(
                    evidence_store,
                    current_family=decision.family,
                    attempted_families=attempted_families,
                    transitions=family_replans,
                    allowed_families=_IMPAIRMENT_FAMILIES,
                )
                if replan is not None:
                    self._configure_budget(budget, replan.family, evidence_store=evidence_store)
                    decision = replace(
                        decision,
                        family=replan.family,
                        reasons=(*decision.reasons, replan.reason),
                    )
                    attempted_families.add(replan.family)
                    family_replans += 1
                    candidate = self._with_route_metadata(
                        candidate,
                        decision,
                        base_assessment=base_assessment,
                        semantic_hints=semantic_hints,
                    )
            # A semantic branch with positive live evidence, or without an
            # independently observable impairment, still fails closed in its
            # own evidence contract.
            if decision.family in _SEMANTIC_FAMILIES:
                missing = semantic_missing or (f"{decision.family}_verification_incomplete",)
                inconclusive = self._with_contract_inconclusive(semantic_result or candidate, missing)
                return self._write_case_trace(
                    context,
                    inconclusive,
                    initial=initial,
                    normalized=candidate,
                    decision=decision,
                    evidence_store=evidence_store,
                    probe_history=probe_history,
                    budget=budget,
                )
        cache = TTLToolCache()
        planned_actions = self._planner.plan(
            family=decision.family or "",
            evidence=evidence_store.all(),
        )
        if self.config.impairment_probes.enabled and decision.family in _IMPAIRMENT_FAMILIES:
            # A runtime-failure semantic closure and any subsequent impairment
            # probes share one case budget. Do not reset the counters here.
            if budget is None:
                budget = self._new_budget(decision.family, evidence_store=evidence_store)
            with budget.use_stage("family_probe"):
                if decision.family == "packet_loss":
                    # Preserve an observed anomalous endpoint pair alongside
                    # the healthy control and one-hop isolation matrix. A
                    # fabric-only matrix cannot traverse a faulty client edge.
                    outcome = await self._run_impairment_probe(
                        context,
                        family=decision.family,
                        result=candidate,
                        budget=budget,
                        evidence=evidence_store.all(),
                        skip_anomaly_pair=False,
                    )
                    evidence_store.add_all(outcome.evidence)
                    probe_history.append(outcome)
                else:
                    outcome = await self._run_impairment_probe(
                        context,
                        family=decision.family,
                        result=candidate,
                        budget=budget,
                        evidence=evidence_store.all(),
                    )
                    evidence_store.add_all(outcome.evidence)
                    probe_history.append(outcome)
            candidate = self._with_probe_metadata(candidate, outcome, evidence_store, budget)
            report = self._validator.validate(candidate, topology=topology)

        ranked_interfaces = []
        peer_collection = {
            "attempted": False,
            "trigger": None,
            "ecmp_ambiguity": False,
            "candidate_count": 0,
        }
        integrity_link_collection = {
            "attempted": False,
            "candidate_count": 0,
            "trigger": None,
        }
        interface_ranker = None
        if self.config.topology_ranker.enabled:
            try:
                if graph is None:
                    graph = TopologyGraph.from_context(context)
                if scale_plan is None:
                    scale_plan = self._scale_policy.plan(
                        graph,
                        adaptive=self.config.adaptive_budget,
                        ranker=self.config.topology_ranker_config,
                    )
                evidence_store.replace_all(
                    scope_path_evidence(
                        graph,
                        item,
                        max_paths=scale_plan.path_sample_limit,
                    )
                    for item in evidence_store.all()
                )
                location = self._location(candidate)
                ranking_family = self._router.observable_family(evidence_store) or decision.family
                interface_ranker = InterfaceRanker(
                    graph,
                    self.config.topology_ranker_config,
                    family=ranking_family,
                )
                # Scale with the two endpoint failure domains, never with the
                # total server count. A path through N-way ECMP has up to 2N
                # independently faulty fabric edges; a fixed Top-K becomes an
                # ordering lottery once N grows beyond the small test fabric.
                attachment_fabric_degree = graph.max_attachment_fabric_degree()
                probe_frontier_limit = scale_plan.probe_frontier_limit
                adaptive_escalation.update(
                    attachment_fabric_degree=attachment_fabric_degree,
                    probe_frontier_limit=probe_frontier_limit,
                    scale_policy=scale_plan.as_metadata(),
                )
                ranked_interfaces = interface_ranker.rank(
                    evidence_store.all(),
                    initial_device=location.get("device"),
                    initial_interface=location.get("interface"),
                )
                top_ranked_loss = ranked_interfaces[0] if ranked_interfaces else None
                has_direct_loss = any(
                    item.category == "packet_loss_rate"
                    and can_support_fault(item)
                    and item.origin is not EvidenceOrigin.BASE_CLAIM
                    and float(item.value) >= self.config.packet_loss_probe.warning_threshold
                    and item.path_observation_confidence >= 1.0
                    for item in evidence_store.all()
                )
                loss_integrity_discriminator = _loss_isolation_conflicts_with_episode(
                    evidence_store,
                    warning_threshold=self.config.packet_loss_probe.warning_threshold,
                )
                concentrated_access_candidate = bool(
                    top_ranked_loss is not None
                    and top_ranked_loss.layer == "access"
                    and top_ranked_loss.link_id in interface_ranker.concentrated_access_links(evidence_store.all())
                )
                weak_loss_discriminator = bool(
                    top_ranked_loss is not None and top_ranked_loss.score > 0 and _has_weak_loss_symptom(evidence_store)
                )
                needs_loss_link_discriminator = bool(
                    has_direct_loss
                    or loss_integrity_discriminator
                    or concentrated_access_candidate
                    or weak_loss_discriminator
                )
                packets_per_link = max(
                    2,
                    min(int(self.config.corruption_probe.samples_per_link_direction), 60) * 2,
                )
                isolation_frontier = (
                    interface_ranker.healthy_link_isolation_frontier(
                        evidence_store.all(),
                        warning_threshold=self.config.packet_loss_probe.warning_threshold,
                        max_candidates=max(
                            self.config.topology_ranker_config.candidate_top_k,
                            probe_frontier_limit,
                        ),
                        # A healthy or unreliable base result often has no
                        # location.  The structured path ranker still knows
                        # which leaf concentrates the passive anomaly.  Keep
                        # the checksum sweep on that leaf's already-isolated
                        # ECMP frontier instead of falling back to a
                        # lexicographically unrelated leaf/access domain.
                        preferred_device=(
                            top_ranked_loss.primary_device if top_ranked_loss is not None else location.get("device")
                        ),
                    )
                    if needs_loss_link_discriminator and not has_direct_loss
                    else []
                )
                access_frontier = interface_ranker.access_isolation_frontier(
                    evidence_store.all(),
                    fabric_frontier=isolation_frontier,
                    max_candidates=probe_frontier_limit,
                )
                # Re-checking four already-cleared fabric links with a second
                # probe method is less discriminative than testing an access
                # edge shared by several independent abnormal flows.  This is
                # ordinary path tomography: the common endpoint is the only
                # edge present in every affected path.  Prefer that edge, but
                # still require a fresh active observation and the unchanged
                # final gate before submission.
                if concentrated_access_candidate and top_ranked_loss is not None:
                    siblings = [item for item in access_frontier if item.link_id != top_ranked_loss.link_id]
                    # A traffic-class-specific impairment may not affect a
                    # locally generated one-hop packet. Use two independent
                    # bounded batches on the concentrated edge before one
                    # sibling control; no result bypasses the normal gate.
                    integrity_candidates = [top_ranked_loss, top_ranked_loss, *siblings]
                else:
                    # Exact healthy probes already cleared the leaf's fabric
                    # cut. Spend the next bounded checksum sweep on its access
                    # edges instead of duplicating the same fabric coverage.
                    integrity_candidates = (
                        access_frontier or isolation_frontier or ([top_ranked_loss] if top_ranked_loss else [])
                    )
                if (
                    decision.family == "packet_loss"
                    and integrity_candidates
                    and needs_loss_link_discriminator
                    and budget is not None
                ):
                    if self.config.adaptive_budget.enabled:
                        required_candidates = len(integrity_candidates)
                        budget.grant_escalation(
                            reason=(
                                "confirmed_direct_loss_integrity_discriminator"
                                if has_direct_loss
                                else "isolated_leaf_integrity_discriminator"
                            ),
                            tool_calls=max(0, required_candidates - budget.remaining_tool_calls),
                            active_probes=max(0, required_candidates - budget.remaining_active_probes),
                            probe_packets=max(
                                0,
                                packets_per_link * required_candidates - budget.remaining_probe_packets,
                            ),
                            stage="evidence_collection",
                        )
                if (
                    decision.family == "packet_loss"
                    and integrity_candidates
                    and needs_loss_link_discriminator
                    and budget is not None
                    and budget.remaining_tool_calls > 0
                    and budget.remaining_active_probes > 0
                    and budget.remaining_probe_packets >= packets_per_link
                ):
                    max_integrity_candidates = min(
                        len(integrity_candidates),
                        budget.remaining_tool_calls,
                        budget.remaining_active_probes,
                        budget.remaining_probe_packets // packets_per_link,
                    )
                    integrity_link_collection.update(
                        attempted=True,
                        candidate_count=max_integrity_candidates,
                        trigger=(
                            "healthy_fabric_cut_access_checksum_discriminator"
                            if access_frontier
                            else "healthy_isolation_frontier_checksum_discriminator"
                            if isolation_frontier
                            else "concentrated_access_link_discriminator"
                            if concentrated_access_candidate and not has_direct_loss
                            else "weak_loss_checksum_discriminator"
                            if weak_loss_discriminator and not has_direct_loss
                            else "top_ranked_loss_link_discriminator"
                        ),
                    )
                    with budget.use_stage("evidence_collection"):
                        link_outcome = await LinkPayloadIntegrityProbe(self.config.corruption_probe).run(
                            context,
                            candidates=integrity_candidates,
                            budget=budget,
                            max_candidates=max_integrity_candidates,
                        )
                    evidence_store.add_all(link_outcome.evidence)
                    probe_history.append(link_outcome)
                    contrast_evidence = interface_ranker.access_path_contrast_evidence(
                        evidence_store.all(),
                        warning_threshold=self.config.packet_loss_probe.warning_threshold,
                    )
                    evidence_store.add_all(contrast_evidence)
                    decisive_integrity = any(
                        (item.category == "payload_integrity_failure" and bool(item.value) and can_support_fault(item))
                        or (
                            item.category == "packet_loss_rate"
                            and float(item.value or 0.0) >= self.config.packet_loss_probe.warning_threshold
                            and can_support_fault(item)
                        )
                        for item in link_outcome.evidence
                    )
                    clean_complete_integrity = any(
                        item.category == "payload_integrity_failure"
                        and item.value is False
                        and int(item.metadata.get("missing_packets") or 0) == 0
                        and bool(item.metadata.get("directions"))
                        for item in link_outcome.evidence
                    )
                    has_missing_sequences = any(
                        item.category == "packet_loss_rate"
                        and not item.supports_submission
                        and int(item.metadata.get("missing_packets") or 0) >= 2
                        for item in link_outcome.evidence
                    )
                    link_evidence = link_outcome.evidence
                    if (
                        has_direct_loss
                        and max_integrity_candidates == 1
                        and not decisive_integrity
                        and clean_complete_integrity
                        # A clean bounded checksum sample does not rule out a
                        # low-rate stochastic corruption process.  When an
                        # independent exact-link loss observation selected one
                        # link, repeat the integrity sample once on that same
                        # link.  This is a sequential discriminator, not a
                        # topology-wide retry or a weaker submission gate.
                        and self.config.adaptive_budget.enabled
                    ) or (
                        concentrated_access_candidate
                        and max_integrity_candidates == 1
                        and has_missing_sequences
                        and self.config.adaptive_budget.enabled
                    ):
                        budget.grant_escalation(
                            reason=(
                                "persistent_access_loss_confirmation"
                                if concentrated_access_candidate and has_missing_sequences
                                else "clean_direct_integrity_sample_repeat"
                            ),
                            tool_calls=max(0, 1 - budget.remaining_tool_calls),
                            active_probes=max(0, 1 - budget.remaining_active_probes),
                            probe_packets=max(0, packets_per_link - budget.remaining_probe_packets),
                            stage="evidence_collection",
                        )
                        if (
                            budget.remaining_tool_calls > 0
                            and budget.remaining_active_probes > 0
                            and budget.remaining_probe_packets >= packets_per_link
                        ):
                            with budget.use_stage("evidence_collection"):
                                repeat_outcome = await LinkPayloadIntegrityProbe(self.config.corruption_probe).run(
                                    context,
                                    candidates=integrity_candidates,
                                    budget=budget,
                                    max_candidates=1,
                                    probe_id="repeated-link-payload-integrity",
                                )
                            evidence_store.add_all(repeat_outcome.evidence)
                            probe_history.append(repeat_outcome)
                            link_evidence = (*link_evidence, *repeat_outcome.evidence)
                    if any(
                        item.category == "payload_integrity_failure" and bool(item.value) and can_support_fault(item)
                        for item in link_evidence
                    ):
                        decision = replace(
                            decision,
                            family="packet_corruption",
                            reasons=(*decision.reasons, "active_payload_integrity_failure"),
                        )
                        candidate = self._with_route_metadata(
                            candidate,
                            decision,
                            base_assessment=base_assessment,
                        )
                        planned_actions = self._planner.plan(
                            family=decision.family,
                            evidence=evidence_store.all(),
                            executed={"payload_integrity", "repeated_packet_loss"},
                        )
                    ranked_interfaces = interface_ranker.rank(
                        evidence_store.all(),
                        initial_device=location.get("device"),
                        initial_interface=location.get("interface"),
                    )
                ambiguity = has_unresolved_ecmp_ambiguity(evidence_store.all())
                peer_collection["ecmp_ambiguity"] = ambiguity
                peer_collection["candidate_count"] = len(ranked_interfaces)
                preliminary_hypotheses = self._scorer.score(
                    evidence_store.all(),
                    interface_candidate=ranked_interfaces[0] if ranked_interfaces else None,
                )
                preliminary_gate = self._gate.analyze(
                    preliminary_hypotheses,
                    evidence_store.all(),
                    ranked_interfaces,
                    conflicts=evidence_store.conflicts,
                    initial_device=location.get("device"),
                    initial_interface=location.get("interface"),
                )
                investigation_candidates = list(ranked_interfaces)
                adaptive_candidates: list[Any] = []
                if (
                    ambiguity
                    and not preliminary_gate.can_submit
                    and budget is not None
                    and decision.family in {"mtu", "packet_loss", "packet_corruption", "high_latency"}
                    and self.config.adaptive_budget.enabled
                    and (
                        _has_reliable_abnormal_path_evidence(
                            evidence_store,
                            warning_threshold=self.config.packet_loss_probe.warning_threshold,
                        )
                        or (decision.family == "high_latency" and _has_nonbase_latency_observation(evidence_store))
                    )
                ):
                    adaptive_candidates = interface_ranker.expansion_candidates(
                        evidence_store.all(),
                        selected=ranked_interfaces,
                        max_candidates=probe_frontier_limit,
                        initial_device=location.get("device"),
                        initial_interface=location.get("interface"),
                    )
                    adaptive_escalation["ecmp_candidates_considered"] = len(adaptive_candidates)
                    adaptive_escalation["ecmp_candidate_links"] = [item.link_id for item in adaptive_candidates]
                    if adaptive_candidates:
                        packets_per_candidate = 0
                        active_probes = 0
                        if decision.family in {"packet_loss", "packet_corruption"}:
                            packets_per_candidate = self.config.packet_loss_probe.link_breadth_packets_per_pair
                            active_probes = len(adaptive_candidates)
                        elif decision.family == "high_latency":
                            packets_per_candidate = self.config.latency_probe.samples_per_pair
                            active_probes = len(adaptive_candidates)
                        budget.grant_escalation(
                            reason="unresolved_ecmp_probe_diversity",
                            tool_calls=len(adaptive_candidates),
                            active_probes=active_probes,
                            probe_packets=packets_per_candidate * len(adaptive_candidates),
                            stage="evidence_collection",
                        )
                        adaptive_escalation.update(
                            eligible=True,
                            trigger="unresolved_ecmp_probe_diversity",
                        )
                        investigation_candidates = list(
                            {item.link_id: item for item in (*ranked_interfaces, *adaptive_candidates)}.values()
                        )
                        peer_collection["candidate_count"] = len(investigation_candidates)

                        if decision.family in {"packet_loss", "packet_corruption"}:
                            fabric_candidates = [
                                item for item in adaptive_candidates if getattr(item, "layer", "fabric") == "fabric"
                            ]
                            max_candidates = min(
                                len(fabric_candidates),
                                budget.remaining_tool_calls,
                                budget.remaining_active_probes,
                                budget.remaining_probe_packets // max(1, packets_per_candidate),
                            )
                            if max_candidates > 0:
                                screened = fabric_candidates[:max_candidates]
                                pairs = [
                                    ProbePair(
                                        source=item.peer_device,
                                        destination=item.primary_device,
                                        destination_name=item.primary_device,
                                        source_leaf=item.peer_device,
                                        destination_leaf=item.primary_device,
                                        metadata={
                                            "selection": "adaptive_ecmp_link_isolation",
                                            "link_probe": True,
                                            "source_interface": item.peer_interface,
                                            "target_device": item.primary_device,
                                            "target_interface": item.primary_interface,
                                            "packets_per_round": packets_per_candidate,
                                            "repeat_rounds": 1,
                                        },
                                    )
                                    for item in screened
                                ]
                                with budget.use_stage("evidence_collection"):
                                    adaptive_outcome = await RepeatedPacketLossProbe(
                                        replace(self.config.packet_loss_probe, max_pairs=max_candidates)
                                    ).run(
                                        context,
                                        pairs=pairs,
                                        budget=budget,
                                        probe_id="adaptive-ecmp-link-screen",
                                    )
                                screened_evidence = tuple(
                                    scope_path_evidence(
                                        graph,
                                        item,
                                        max_paths=self.config.topology_ranker_config.max_ecmp_paths,
                                    )
                                    for item in adaptive_outcome.evidence
                                )
                                evidence_store.add_all(screened_evidence)
                                probe_history.append(adaptive_outcome)
                                adaptive_escalation["ecmp_candidates_probed"] = max_candidates
                                adaptive_escalation["ecmp_probe_targets"] = [item.link_id for item in screened]
                                abnormal_links = {
                                    link_id
                                    for item in screened_evidence
                                    if item.category == "packet_loss_rate"
                                    and can_support_fault(item)
                                    and float(item.value or 0.0) >= self.config.packet_loss_probe.warning_threshold
                                    for link_id in item.covered_links
                                }
                                confirmation = [item for item in screened if item.link_id in abnormal_links]
                                integrity_packets = max(
                                    2,
                                    min(int(self.config.corruption_probe.samples_per_link_direction), 60) * 2,
                                )
                                if confirmation:
                                    budget.grant_escalation(
                                        reason="abnormal_member_integrity_confirmation",
                                        tool_calls=len(confirmation),
                                        active_probes=len(confirmation),
                                        probe_packets=integrity_packets * len(confirmation),
                                        stage="evidence_collection",
                                    )
                                confirm_count = min(
                                    len(confirmation),
                                    budget.remaining_tool_calls,
                                    budget.remaining_active_probes,
                                    budget.remaining_probe_packets // max(1, integrity_packets),
                                )
                                if confirm_count > 0:
                                    with budget.use_stage("evidence_collection"):
                                        integrity_outcome = await LinkPayloadIntegrityProbe(
                                            self.config.corruption_probe
                                        ).run(
                                            context,
                                            candidates=confirmation,
                                            budget=budget,
                                            max_candidates=confirm_count,
                                            probe_id="adaptive-abnormal-link-integrity",
                                        )
                                    evidence_store.add_all(integrity_outcome.evidence)
                                    probe_history.append(integrity_outcome)
                                    integrity_link_collection.update(
                                        attempted=True,
                                        candidate_count=(
                                            int(integrity_link_collection["candidate_count"] or 0) + confirm_count
                                        ),
                                        trigger="adaptive_abnormal_member_confirmation",
                                    )
                                    if any(
                                        item.category == "payload_integrity_failure"
                                        and bool(item.value)
                                        and can_support_fault(item)
                                        for item in integrity_outcome.evidence
                                    ):
                                        decision = replace(
                                            decision,
                                            family="packet_corruption",
                                            reasons=(*decision.reasons, "adaptive_payload_integrity_failure"),
                                        )
                                        candidate = self._with_route_metadata(
                                            candidate,
                                            decision,
                                            base_assessment=base_assessment,
                                        )
                                ranked_interfaces = interface_ranker.rank(
                                    evidence_store.all(),
                                    initial_device=location.get("device"),
                                    initial_interface=location.get("interface"),
                                )
                        elif decision.family == "high_latency":
                            # ``ping_link_test`` is a switch-to-switch tool.
                            # Access-link candidates remain useful to ranking,
                            # but sending a client endpoint to this tool only
                            # creates schema/tool errors and consumes the
                            # evidence budget without observing the network.
                            fabric_candidates = [
                                item for item in adaptive_candidates if getattr(item, "layer", "fabric") == "fabric"
                            ]
                            max_candidates = min(
                                len(fabric_candidates),
                                budget.remaining_tool_calls,
                                budget.remaining_active_probes,
                                budget.remaining_probe_packets // max(1, packets_per_candidate),
                            )
                            if max_candidates > 0:
                                pairs = [
                                    ProbePair(
                                        source=item.peer_device,
                                        destination=item.primary_device,
                                        destination_name=item.primary_device,
                                        source_leaf=item.peer_device,
                                        destination_leaf=item.primary_device,
                                        metadata={
                                            "selection": "adaptive_ecmp_link_isolation",
                                            "link_probe": True,
                                            "source_interface": item.peer_interface,
                                            "target_device": item.primary_device,
                                            "target_interface": item.primary_interface,
                                        },
                                    )
                                    for item in fabric_candidates[:max_candidates]
                                ]
                                with budget.use_stage("evidence_collection"):
                                    adaptive_outcome = await RTTMatrixProbe(
                                        replace(self.config.latency_probe, max_pairs=max_candidates)
                                    ).run(
                                        context,
                                        pairs=pairs,
                                        budget=budget,
                                        probe_id="adaptive-ecmp-rtt",
                                    )
                                evidence_store.add_all(
                                    scope_path_evidence(
                                        graph,
                                        item,
                                        max_paths=self.config.topology_ranker_config.max_ecmp_paths,
                                    )
                                    for item in adaptive_outcome.evidence
                                )
                                probe_history.append(adaptive_outcome)
                                adaptive_escalation["ecmp_candidates_probed"] = max_candidates
                                adaptive_escalation["ecmp_probe_targets"] = [
                                    item.link_id for item in fabric_candidates[:max_candidates]
                                ]
                                ranked_interfaces = interface_ranker.rank(
                                    evidence_store.all(),
                                    initial_device=location.get("device"),
                                    initial_interface=location.get("interface"),
                                )
                # Candidate discovery and ECMP screening may change Top-1.
                # Collect endpoint-discriminating evidence only after that
                # rerank, otherwise a valid directional probe can be spent on
                # the preliminary access/shared link while the final fabric
                # candidate remains unverified.
                preliminary_hypotheses = self._scorer.score(
                    evidence_store.all(),
                    interface_candidate=ranked_interfaces[0] if ranked_interfaces else None,
                )
                preliminary_gate = self._gate.analyze(
                    preliminary_hypotheses,
                    evidence_store.all(),
                    ranked_interfaces,
                    conflicts=evidence_store.conflicts,
                    initial_device=location.get("device"),
                    initial_interface=location.get("interface"),
                )
                final_latency_candidate = _select_latency_directional_candidate(evidence_store.all(), ranked_interfaces)
                already_directional = _has_directional_latency_for_candidate(
                    evidence_store.all(), final_latency_candidate
                )
                if (
                    decision.family == "high_latency"
                    and final_latency_candidate is not None
                    and not already_directional
                    and not preliminary_gate.can_submit
                    and "direct_interface_evidence" in preliminary_gate.missing_requirements
                    and budget is not None
                    and (
                        _has_reliable_abnormal_path_evidence(
                            evidence_store,
                            warning_threshold=self.config.packet_loss_probe.warning_threshold,
                        )
                        or _has_nonbase_latency_observation(evidence_store)
                    )
                ):
                    required_calls = 1
                    required_packets = self.config.latency_probe.samples_per_pair * 2
                    budget.grant_escalation(
                        reason="latency_final_candidate_endpoint_discriminator",
                        tool_calls=max(0, required_calls - budget.remaining_tool_calls),
                        active_probes=max(0, required_calls - budget.remaining_active_probes),
                        probe_packets=max(0, required_packets - budget.remaining_probe_packets),
                        stage="evidence_collection",
                    )
                    if (
                        budget.remaining_tool_calls >= required_calls
                        and budget.remaining_active_probes >= required_calls
                        and budget.remaining_probe_packets >= required_packets
                    ):
                        with budget.use_stage("evidence_collection"):
                            directional_outcome = await DirectionalLinkLatencyProbe(self.config.latency_probe).run(
                                context,
                                candidate=final_latency_candidate,
                                budget=budget,
                                probe_id=f"directional-link-latency:{final_latency_candidate.link_id}",
                            )
                        evidence_store.add_all(directional_outcome.evidence)
                        probe_history.append(directional_outcome)
                        ranked_interfaces = interface_ranker.rank(
                            evidence_store.all(),
                            initial_device=location.get("device"),
                            initial_interface=location.get("interface"),
                        )
                high_score_candidates = any(
                    item.score >= self.config.diagnosability.minimum_interface_score for item in ranked_interfaces
                )
                remaining_calls = budget.remaining_tool_calls if budget is not None else 0
                ambiguity_trigger = ambiguity and not preliminary_gate.can_submit and remaining_calls >= 2
                should_collect_peers = high_score_candidates or ambiguity_trigger
                if (
                    decision.family == "mtu"
                    and budget is not None
                    and ranked_interfaces
                    and should_collect_peers
                    and remaining_calls > 0
                ):
                    peer_collection["attempted"] = True
                    peer_collection["trigger"] = (
                        "high_score_candidate" if high_score_candidates else "unresolved_ecmp_ambiguity"
                    )
                    with budget.use_stage("evidence_collection"):
                        peer_evidence = await PeerConsistencyCollector(
                            ttl_seconds=self.config.cache.interface_state_ttl_seconds
                        ).collect(
                            context,
                            candidates=investigation_candidates,
                            budget=budget,
                            cache=cache,
                            max_candidates=len(investigation_candidates),
                            evidence=evidence_store.all(),
                        )
                    evidence_store.add_all(peer_evidence)
                    mtu_difference_links = {
                        str(item.metadata.get("link_id"))
                        for item in peer_evidence
                        if item.category == "configuration_difference"
                        and isinstance(item.value, dict)
                        and item.value.get("field") == "mtu"
                        and item.value.get("different")
                        and item.metadata.get("link_id")
                    }
                    has_size_dependent_observation = any(
                        item.category == "packet_size_threshold"
                        and isinstance(item.value, dict)
                        and item.value.get("size_dependent_failure")
                        and can_support_fault(item)
                        for item in evidence_store.all()
                    )
                    mtu_link_candidates = (
                        []
                        if has_size_dependent_observation
                        else [item for item in investigation_candidates if item.link_id in mtu_difference_links]
                    )
                    if mtu_link_candidates:
                        calls_per_link = 2
                        packets_per_link = calls_per_link * self.config.mtu_probe.packets_per_size
                        if self.config.adaptive_budget.enabled:
                            budget.grant_escalation(
                                reason="peer_mtu_difference_dataplane_confirmation",
                                tool_calls=max(
                                    0,
                                    calls_per_link * len(mtu_link_candidates) - budget.remaining_tool_calls,
                                ),
                                active_probes=max(
                                    0,
                                    len(mtu_link_candidates) - budget.remaining_active_probes,
                                ),
                                probe_packets=max(
                                    0,
                                    packets_per_link * len(mtu_link_candidates) - budget.remaining_probe_packets,
                                ),
                                stage="evidence_collection",
                            )
                        max_mtu_links = min(
                            len(mtu_link_candidates),
                            budget.remaining_tool_calls // calls_per_link,
                            budget.remaining_active_probes,
                            budget.remaining_probe_packets // max(1, packets_per_link),
                        )
                        if max_mtu_links > 0:
                            with budget.use_stage("evidence_collection"):
                                mtu_link_outcome = await MTULinkSweepProbe(self.config.mtu_probe).run(
                                    context,
                                    candidates=mtu_link_candidates,
                                    evidence=evidence_store.all(),
                                    budget=budget,
                                    max_candidates=max_mtu_links,
                                )
                            evidence_store.add_all(mtu_link_outcome.evidence)
                            probe_history.append(mtu_link_outcome)
                    if adaptive_candidates:
                        adaptive_escalation["ecmp_candidates_probed"] = len(adaptive_candidates)
                        adaptive_escalation["ecmp_probe_targets"] = [item.link_id for item in adaptive_candidates]
                    ranked_interfaces = interface_ranker.rank(
                        evidence_store.all(),
                        initial_device=location.get("device"),
                        initial_interface=location.get("interface"),
                    )
                if (
                    decision.family == "packet_corruption"
                    and not integrity_link_collection["attempted"]
                    and budget is not None
                    and investigation_candidates
                ):
                    calls_remaining = budget.remaining_tool_calls
                    probes_remaining = budget.remaining_active_probes
                    packets_per_link = max(
                        2,
                        min(int(self.config.corruption_probe.samples_per_link_direction), 60) * 2,
                    )
                    packet_capacity = max(
                        0,
                        budget.remaining_probe_packets // packets_per_link,
                    )
                    max_candidates = min(
                        len(investigation_candidates),
                        calls_remaining,
                        probes_remaining,
                        packet_capacity,
                    )
                    if max_candidates > 0:
                        integrity_link_collection.update(
                            attempted=True,
                            candidate_count=max_candidates,
                            trigger="end_to_end_checksum_failure",
                        )
                        with budget.use_stage("evidence_collection"):
                            link_outcome = await LinkPayloadIntegrityProbe(self.config.corruption_probe).run(
                                context,
                                candidates=investigation_candidates,
                                budget=budget,
                                max_candidates=max_candidates,
                            )
                        evidence_store.add_all(link_outcome.evidence)
                        probe_history.append(link_outcome)
                        if adaptive_candidates:
                            adaptive_count = min(max_candidates, len(adaptive_candidates))
                            adaptive_escalation["ecmp_candidates_probed"] = adaptive_count
                            adaptive_escalation["ecmp_probe_targets"] = [
                                item.link_id for item in adaptive_candidates[:adaptive_count]
                            ]
                        ranked_interfaces = interface_ranker.rank(
                            evidence_store.all(),
                            initial_device=location.get("device"),
                            initial_interface=location.get("interface"),
                        )

                promoted_loss = _cross_validated_link_loss(
                    evidence_store,
                    warning_threshold=self.config.packet_loss_probe.warning_threshold,
                )
                if promoted_loss:
                    evidence_store.add_all(promoted_loss)
                    ranked_interfaces = interface_ranker.rank(
                        evidence_store.all(),
                        initial_device=location.get("device"),
                        initial_interface=location.get("interface"),
                    )

                # A provider may label a device-wide outage as generic loss,
                # especially after schema/recursion failure.  Reclassify only
                # when existing direct one-hop probes form a device-scope
                # pattern with independent healthy peer controls.  No new
                # calls or budget are consumed and tool errors remain inert.
                if decision.family == "packet_loss":
                    device_closure = device_down_from_link_probes(
                        candidate,
                        topology=topology,
                        evidence=evidence_store.all(),
                    )
                    if device_closure is not None and device_closure.result is not None:
                        evidence_store.add_all(device_closure.outcome.evidence)
                        probe_history.append(device_closure.outcome)
                        device_result = self._with_probe_metadata(
                            device_closure.result,
                            device_closure.outcome,
                            evidence_store,
                            budget,
                        )
                        device_report = self._validator.validate(device_result, topology=topology)
                        device_contract = self._contracts.evaluate(
                            device_result,
                            evidence_store.all(),
                            require_location=True,
                        )
                        if device_report.valid and device_contract.satisfied:
                            return self._write_case_trace(
                                context,
                                device_result,
                                initial=initial,
                                normalized=candidate,
                                decision=decision,
                                evidence_store=evidence_store,
                                probe_history=probe_history,
                                ranked_interfaces=ranked_interfaces,
                                budget=budget,
                                cache=cache,
                            )
            except Exception as exc:  # noqa: BLE001 - topology projection must fail closed
                topology_error = f"{type(exc).__name__}: {exc}"

        # Reconcile passive/aggregate path symptoms only after all bounded
        # exact-link observations have been collected. Partial ECMP coverage
        # changes nothing; complete newer coverage demotes the old symptom to
        # planning-only while retaining it in the immutable audit trail.
        reconciled = reconcile_active_path_coverage(
            evidence_store.all(),
            warning_threshold=self.config.packet_loss_probe.warning_threshold,
        )
        evidence_store.apply_reconciliation(reconciled)
        if interface_ranker is not None:
            location = self._location(candidate)
            ranked_interfaces = interface_ranker.rank(
                evidence_store.all(),
                initial_device=location.get("device"),
                initial_interface=location.get("interface"),
            )

        coverage_closed = any(
            item.category == "coverage_certificate"
            and item.source == "evidence_reconciliation"
            and bool(item.metadata.get("coverage_complete"))
            for item in evidence_store.all()
        )
        if coverage_closed and budget is not None:
            self._grant_complete_coverage_healthy_budget(budget, topology)
            with budget.use_stage("final_verification"):
                healthy = await self._operational_closure.verify_healthy(
                    context,
                    base_result=candidate,
                    topology=topology,
                    evidence=evidence_store.all(),
                    budget=budget,
                )
            evidence_store.add_all(healthy.outcome.evidence)
            probe_history.append(healthy.outcome)
            if healthy.result is not None:
                healthy_result = self._with_probe_metadata(
                    healthy.result,
                    healthy.outcome,
                    evidence_store,
                    budget,
                )
                healthy_report = self._validator.validate(healthy_result, topology=topology)
                healthy_contract = self._contracts.evaluate(
                    healthy_result,
                    evidence_store.all(),
                    require_location=False,
                )
                if healthy_report.valid and healthy_contract.satisfied:
                    healthy_decision = replace(
                        decision,
                        family="healthy_verification",
                        reasons=(*decision.reasons, "complete_active_path_coverage"),
                    )
                    return self._write_case_trace(
                        context,
                        healthy_result,
                        initial=initial,
                        normalized=candidate,
                        decision=healthy_decision,
                        evidence_store=evidence_store,
                        probe_history=probe_history,
                        ranked_interfaces=ranked_interfaces,
                        budget=budget,
                        cache=cache,
                    )

        hypotheses = {}
        gate_decision = None
        if self.config.diagnosability_gate.enabled:
            interface_candidate = ranked_interfaces[0] if ranked_interfaces else None
            location = self._location(candidate)
            reduction = self._reducer.reduce(
                evidence_store.all(),
                ranked_interfaces,
                conflicts=evidence_store.conflicts,
                initial_device=location.get("device"),
                initial_interface=location.get("interface"),
            )
            hypotheses = reduction.hypotheses
            gate_decision = reduction.gate_decision
            secondary_faults = reduction.secondary_faults
            candidate = self._with_analysis_metadata(
                candidate,
                evidence_store=evidence_store,
                budget=budget,
                cache=cache,
                probe_history=probe_history,
                planned_actions=planned_actions,
                ranked_interfaces=ranked_interfaces,
                hypotheses=hypotheses,
                gate_decision=gate_decision,
                topology_error=topology_error,
                peer_collection=peer_collection,
                integrity_link_collection=integrity_link_collection,
                adaptive_escalation=adaptive_escalation,
            )
            if gate_decision.can_submit and interface_candidate is not None:
                top = max(hypotheses.values(), key=lambda item: item.probability)
                diagnosed = self._build_diagnosed(candidate, top, interface_candidate)
                diagnosed = self._with_secondary_faults(diagnosed, secondary_faults)
                diagnosed_contract = self._contracts.evaluate(
                    diagnosed,
                    evidence_store.all(),
                    candidates=ranked_interfaces,
                    require_location=True,
                )
                stop = self._stop_verifier.verify(diagnosed, decision=gate_decision, topology=topology)
                if stop.can_submit and diagnosed_contract.satisfied:
                    return self._write_case_trace(
                        context,
                        diagnosed,
                        initial=initial,
                        normalized=candidate,
                        decision=decision,
                        evidence_store=evidence_store,
                        probe_history=probe_history,
                        ranked_interfaces=ranked_interfaces,
                        hypotheses=hypotheses,
                        gate_decision=gate_decision,
                        budget=budget,
                        cache=cache,
                    )
                if not diagnosed_contract.satisfied:
                    gate_decision = replace(
                        gate_decision,
                        can_submit=False,
                        reason="Evidence contract is incomplete.",
                        missing_requirements=tuple(
                            dict.fromkeys(
                                (*gate_decision.missing_requirements, *diagnosed_contract.missing_requirements)
                            )
                        ),
                    )
                    candidate = self._with_analysis_metadata(
                        candidate,
                        evidence_store=evidence_store,
                        budget=budget,
                        cache=cache,
                        probe_history=probe_history,
                        planned_actions=planned_actions,
                        ranked_interfaces=ranked_interfaces,
                        hypotheses=hypotheses,
                        gate_decision=gate_decision,
                        topology_error=topology_error,
                        peer_collection=peer_collection,
                        integrity_link_collection=integrity_link_collection,
                        adaptive_escalation=adaptive_escalation,
                    )
            inconclusive = self._build_gate_inconclusive(candidate, gate_decision)
            return self._write_case_trace(
                context,
                inconclusive,
                initial=initial,
                normalized=candidate,
                decision=decision,
                evidence_store=evidence_store,
                probe_history=probe_history,
                ranked_interfaces=ranked_interfaces,
                hypotheses=hypotheses,
                gate_decision=gate_decision,
                budget=budget,
                cache=cache,
            )
        final_contract = self._contracts.evaluate(candidate, evidence_store.all(), require_location=True)
        if not final_contract.satisfied:
            return self._with_contract_inconclusive(candidate, final_contract.missing_requirements)
        if report.valid:
            status = "probed_deferred_until_hypothesis_discriminator" if outcome else "deferred_until_impairment_probes"
            return self._with_deferred_status(candidate, status=status)
        status = "probed_but_not_diagnosable" if outcome else "blocked_until_impairment_probes"
        return self._build_inconclusive(candidate, report, status=status)

    async def _run_impairment_probe(
        self,
        context: Any,
        *,
        family: str,
        result: DiagnosisResult,
        budget: ProbeBudget,
        evidence: list[Evidence] | tuple[Evidence, ...] = (),
        skip_anomaly_pair: bool = False,
    ) -> ProbeOutcome:
        try:
            if family == "packet_loss":
                matrix_enabled = self.config.packet_loss_probe.link_isolation_enabled
                packets_per_round = (
                    self.config.packet_loss_probe.matrix_packets_per_round
                    if matrix_enabled
                    else self.config.packet_loss_probe.packets_per_pair
                )
                repeat_rounds = (
                    self.config.packet_loss_probe.matrix_repeat_rounds
                    if matrix_enabled
                    else self.config.packet_loss_probe.repeat_rounds
                )
                calls_per_pair = math.ceil(packets_per_round / 20) * repeat_rounds
                max_pairs = min(
                    self.config.packet_loss_probe.max_pairs,
                    self.config.budget.max_active_probes_per_case,
                    max(
                        1,
                        self.config.budget.max_extra_tool_calls_per_hard_case // max(1, calls_per_pair),
                    ),
                )
                pairs = select_probe_pairs(
                    context,
                    family=family,
                    max_pairs=max_pairs,
                    evidence=evidence,
                    anomaly_pairs=self.config.packet_loss_probe.anomaly_pairs,
                    control_pairs=self.config.packet_loss_probe.control_pairs,
                    link_isolation=matrix_enabled,
                    packets_per_round=packets_per_round,
                    repeat_rounds=repeat_rounds,
                    control_repeat_rounds=self.config.packet_loss_probe.control_repeat_rounds,
                    link_breadth_packets_per_round=(self.config.packet_loss_probe.link_breadth_packets_per_pair),
                    link_breadth_repeat_rounds=self.config.packet_loss_probe.link_breadth_rounds,
                )
                if skip_anomaly_pair:
                    contrast_pairs = [pair for pair in pairs if pair.metadata.get("selection") != "anomaly"]
                    if contrast_pairs:
                        pairs = contrast_pairs
                if not pairs:
                    return self._missing_pair_outcome(family)
                return await RepeatedPacketLossProbe(self.config.packet_loss_probe).run(
                    context,
                    pairs=pairs,
                    budget=budget,
                )
            if family == "mtu":
                pairs = select_probe_pairs(
                    context,
                    family=family,
                    max_pairs=self.config.mtu_probe.max_pairs,
                    evidence=evidence,
                )
                if not pairs:
                    return self._missing_pair_outcome(family)
                return await MTUPacketSizeSweepProbe(self.config.mtu_probe).run(
                    context,
                    pairs=pairs,
                    budget=budget,
                )
            if family == "high_latency":
                max_pairs = min(
                    self.config.latency_probe.max_pairs,
                    self.config.budget.max_active_probes_per_case,
                    self.config.budget.max_extra_tool_calls_per_hard_case,
                )
                pairs = select_probe_pairs(
                    context,
                    family=family,
                    max_pairs=max_pairs,
                    evidence=evidence,
                    anomaly_pairs=self.config.latency_probe.anomaly_pairs,
                    control_pairs=self.config.latency_probe.control_pairs,
                    link_isolation=self.config.latency_probe.link_isolation_enabled,
                )
                if not pairs:
                    return self._missing_pair_outcome(family)
                return await RTTMatrixProbe(self.config.latency_probe).run(
                    context,
                    pairs=pairs,
                    budget=budget,
                )
            pairs = select_probe_pairs(
                context,
                family=family,
                # Preserve the second half of the active-probe allowance for
                # physical-link localization after an end-to-end checksum
                # mismatch.  This is a staged allocation inside the existing
                # case budget, not extra probing.
                max_pairs=max(1, self.config.budget.max_active_probes_per_case // 2),
                evidence=evidence,
                anomaly_pairs=max(1, self.config.budget.max_active_probes_per_case // 2),
                diverse_endpoint_pairs=True,
            )
            location = result.findings.get("location") if isinstance(result.findings, dict) else {}
            location = location if isinstance(location, dict) else {}
            return await PayloadIntegrityProbe(self.config.corruption_probe).run(
                context,
                pairs=pairs,
                budget=budget,
                target_device=location.get("device"),
                target_interface=location.get("interface"),
            )
        except Exception as exc:  # noqa: BLE001 - one failed probe must not fail the agent
            message = f"probe execution failed: {type(exc).__name__}: {exc}"
            return ProbeOutcome(
                probe_id=f"{family}-probe",
                status="failed",
                evidence=(
                    Evidence(
                        evidence_id=f"{family}-probe-unhandled-error",
                        entity_type="probe",
                        entity_id=family,
                        category="tool_error",
                        value={"error": message},
                        source="diagnostic_harness",
                        timestamp=datetime.now(UTC),
                        reliability=0.0,
                        origin=EvidenceOrigin.UNKNOWN,
                        supports_submission=False,
                    ),
                ),
                error=message,
            )

    def _grant_complete_coverage_healthy_budget(
        self,
        budget: ProbeBudget,
        topology: TopologyIndex,
    ) -> dict[str, int]:
        """Protect final health verification after a symptom is disproved.

        The grant is unavailable until complete exact-link coverage exists at
        the call site.  It expands only unused resource dimensions up to the
        existing adaptive ceilings and does not relax evidence contracts.  A
        real fault therefore still wins immediately if any final check is
        abnormal.
        """
        required = self._operational_closure.healthy_verification_budget(topology)
        return budget.grant_escalation(
            reason="complete_active_path_coverage_healthy_verification",
            tool_calls=max(0, required["tool_calls"] - budget.remaining_tool_calls),
            active_probes=max(0, required["active_probes"] - budget.remaining_active_probes),
            probe_packets=max(0, required["probe_packets"] - budget.remaining_probe_packets),
            stage="final_verification",
        )

    def _new_budget(self, family: str | None, *, evidence_store: EvidenceStore) -> ProbeBudget:
        budget = ProbeBudget(self.config.budget, adaptive_config=self.config.adaptive_budget)
        self._configure_budget(budget, family, evidence_store=evidence_store)
        return budget

    def _configure_budget(
        self,
        budget: ProbeBudget,
        family: str | None,
        *,
        evidence_store: EvidenceStore,
    ) -> None:
        total = self.config.budget.max_extra_tool_calls_per_hard_case
        family_reserve = 0
        evidence_reserve = 0
        triage_cap = None
        semantic_cap = self.config.branch_budget.semantic_closure_max_tool_calls

        if family == "mtu":
            family_reserve = min(
                total,
                len(self.config.mtu_probe.payload_sizes) * self.config.mtu_probe.max_pairs,
            )
            # Peer inventory is the remaining part of the proven 12-call MTU
            # closure. This does not increase the total hard-case budget.
            evidence_reserve = max(0, total - family_reserve)
            semantic_cap = 0
        elif family == "packet_loss":
            family_reserve = min(11, total)
            evidence_reserve = max(0, min(1, total - family_reserve))
        elif family == "static_route":
            # Reserve route/config collection before any generic fallback;
            # this remains inside the existing 12-call hard-case cap.
            evidence_reserve = min(
                total,
                self.config.branch_budget.static_route_required_evidence_reserve_tool_calls,
            )
        elif family == "healthy_verification":
            triage_cap = min(self.config.branch_budget.healthy_verification_max_tool_calls, total)
        elif family == "temporal_verification":
            triage_cap = min(self.config.branch_budget.temporal_triage_max_tool_calls, total)
        elif family == "link_state_verification":
            triage_cap = min(self.config.branch_budget.link_state_triage_max_tool_calls, total)
        elif family == "runtime_semantic" and self._router.has_partial_packet_loss(evidence_store):
            family_reserve = min(10, total)
            semantic_cap = max(0, total - family_reserve)

        budget.configure_stages(
            family=family,
            family_probe_reserve=family_reserve,
            evidence_collection_reserve=evidence_reserve,
            final_verification_reserve=self.config.branch_budget.final_verification_reserve_tool_calls,
            triage_cap=triage_cap,
            semantic_closure_cap=semantic_cap,
        )

    @staticmethod
    def _missing_pair_outcome(family: str) -> ProbeOutcome:
        message = "No safe source/destination pair is available from public observations or topology inventory."
        return ProbeOutcome(
            probe_id=f"{family}-probe",
            status="failed",
            evidence=(
                Evidence(
                    evidence_id=f"{family}-probe-missing-pair",
                    entity_type="probe",
                    entity_id=family,
                    category="missing_observation",
                    value={"reason": message},
                    source="diagnostic_harness",
                    timestamp=datetime.now(UTC),
                    reliability=0.0,
                    origin=EvidenceOrigin.UNKNOWN,
                    supports_submission=False,
                ),
            ),
            error=message,
        )

    @staticmethod
    def _with_route_metadata(
        result: DiagnosisResult,
        decision: Any,
        *,
        base_assessment: BaseAgentAssessment | None = None,
        semantic_hints: Any = (),
    ) -> DiagnosisResult:
        metadata = dict(result.metadata or {})
        harness = dict(metadata.get("diagnostic_harness") or {})
        harness["route_decision"] = {
            "fast_path": decision.fast_path,
            "family": decision.family,
            "reasons": list(decision.reasons),
        }
        harness["semantic_family_hints"] = [asdict(item) for item in semantic_hints]
        if base_assessment is not None:
            harness["base_agent_reliability"] = {
                "status": base_assessment.status.value,
                "reasons": list(base_assessment.reasons),
                "direct_evidence": base_assessment.direct_evidence,
                "semantic_conflict": base_assessment.semantic_conflict,
            }
        metadata["diagnostic_harness"] = harness
        agent_name = str(result.agent_name or "agent")
        if not agent_name.startswith("diagnostic-harness["):
            agent_name = f"diagnostic-harness[{agent_name}]"
        return replace(result, agent_name=agent_name, metadata=metadata)

    @staticmethod
    def _verification_family(result: DiagnosisResult, current: str | None) -> str | None:
        """Map an unverified claim to the least expensive live closure."""
        findings = result.findings if isinstance(result.findings, dict) else {}
        fault_type = findings.get("fault_type")
        family_map = {
            "device_down": "link_state_verification",
            "link_down": "link_state_verification",
            "link_flapping": "temporal_verification",
            "bgp_neighbor_misconfig": "bgp_verification",
            "acl_misconfig": "acl",
            "route_policy_misconfig": "route_policy",
            "static_route_misconfig": "static_route",
            "blackhole_route": "static_route",
        }
        mapped: str | None = None
        if fault_type in family_map:
            mapped = family_map[str(fault_type)]
        elif result.verdict == "network_healthy":
            mapped = "healthy_verification"

        # The router may already have selected a more specific family from
        # public observations (for example packet loss contradicting a healthy
        # base answer, or direct link state contradicting a policy label).
        # A contract veto concerns submission reliability; it must not erase
        # that independently-derived routing decision.
        if current in family_map:
            return family_map[current]
        if current and current not in {fault_type, "generic_verification"}:
            return current
        return mapped or current

    def _resolve_route_policy(self, result: DiagnosisResult, decision: Any) -> DiagnosisResult | None:
        """Correct a high-confidence BGP label contradicted by direct policy evidence."""
        if decision.family != "route_policy" or result.verdict != "fault_detected":
            return None
        findings = dict(result.findings or {})
        location = findings.get("location") if isinstance(findings.get("location"), dict) else {}
        evidence = findings.get("evidence")
        source_fault_type = findings.get("fault_type")
        # This adapter exists for one narrow normalization case: a base agent
        # calls a missing-prefix advertisement a BGP-neighbor fault even though
        # all sessions are established.  It must never turn an operational or
        # impairment diagnosis into route-policy merely because downstream
        # route prose contains policy vocabulary.
        route_reasons = set(decision.reasons)
        if (
            source_fault_type != "bgp_neighbor_misconfig"
            or "bgp_label_conflicts_with_route_policy_evidence" not in route_reasons
            or result.confidence < self.config.diagnosability.submit_confidence
            or not location.get("device")
            or not isinstance(evidence, list)
            or not evidence
        ):
            return None

        findings["fault_type"] = "route_policy_misconfig"
        metadata = dict(result.metadata or {})
        harness = dict(metadata.get("diagnostic_harness") or {})
        harness["hard_path_status"] = "route_policy_semantic_resolution"
        harness["route_policy_resolution"] = {
            "normalized_from": source_fault_type,
            "confidence": result.confidence,
            "minimum_confidence": self.config.diagnosability.submit_confidence,
            "reason": "Direct policy/origination evidence conflicts with a neighbor-session label.",
        }
        metadata["diagnostic_harness"] = harness
        return replace(result, findings=findings, metadata=metadata)

    @staticmethod
    def _with_probe_metadata(
        result: DiagnosisResult,
        outcome: ProbeOutcome,
        store: EvidenceStore,
        budget: ProbeBudget,
    ) -> DiagnosisResult:
        metadata = dict(result.metadata)
        harness = dict(metadata.get("diagnostic_harness") or {})
        harness["probe_outcome"] = {
            "probe_id": outcome.probe_id,
            "status": outcome.status,
            "tool_calls": outcome.tool_calls,
            "probe_packets": outcome.probe_packets,
            "error": outcome.error,
            "metadata": outcome.metadata,
        }
        harness["evidence"] = store.summarize_for_llm(max_items=40)
        harness["cost"] = budget.snapshot()
        harness["budget_allocation"] = budget.allocation_snapshot()
        metadata["diagnostic_harness"] = harness
        return replace(result, metadata=metadata)

    @staticmethod
    def _location(result: DiagnosisResult) -> dict[str, Any]:
        return dict(DiagnosisView.from_result(result).location)

    @staticmethod
    def _with_analysis_metadata(
        result: DiagnosisResult,
        *,
        evidence_store: EvidenceStore,
        budget: ProbeBudget | None,
        cache: TTLToolCache,
        probe_history: list[ProbeOutcome],
        planned_actions: list[Any],
        ranked_interfaces: list[Any],
        hypotheses: dict[str, Any],
        gate_decision: Any,
        topology_error: str | None,
        peer_collection: dict[str, Any],
        integrity_link_collection: dict[str, Any],
        adaptive_escalation: dict[str, Any],
    ) -> DiagnosisResult:
        metadata = dict(result.metadata or {})
        harness = dict(metadata.get("diagnostic_harness") or {})
        harness["planned_actions"] = [asdict(action) for action in planned_actions]
        harness["probe_history"] = [
            {
                "probe_id": item.probe_id,
                "status": item.status,
                "tool_calls": item.tool_calls,
                "probe_packets": item.probe_packets,
                "error": item.error,
                "metadata": item.metadata,
            }
            for item in probe_history
        ]
        harness["evidence"] = evidence_store.summarize_for_llm(max_items=60)
        harness["interface_candidates"] = [asdict(item) for item in ranked_interfaces[:10]]
        harness["hypotheses"] = [asdict(item) for item in sorted(hypotheses.values(), key=lambda h: -h.probability)]
        harness["diagnosability"] = asdict(gate_decision) if gate_decision is not None else None
        harness["cost"] = (
            budget.snapshot()
            if budget is not None
            else {
                "tool_calls": 0,
                "active_probes": 0,
                "probe_packets": 0,
            }
        )
        harness["budget_allocation"] = budget.allocation_snapshot() if budget is not None else None
        harness["cache"] = {
            **cache.stats,
            "active_probe_ttl_seconds": 0.0,
            "active_probe_reuse": False,
        }
        harness["peer_collection"] = peer_collection
        harness["integrity_link_collection"] = integrity_link_collection
        harness["adaptive_escalation"] = adaptive_escalation
        if topology_error:
            harness["topology_error"] = topology_error
        metadata["diagnostic_harness"] = harness
        return replace(result, metadata=metadata)

    @staticmethod
    def _build_diagnosed(result: DiagnosisResult, hypothesis: Any, interface_candidate: Any) -> DiagnosisResult:
        findings = dict(result.findings or {})
        findings["fault_type"] = hypothesis.fault_type
        findings["location"] = {
            "device": interface_candidate.primary_device,
            "interface": interface_candidate.primary_interface,
        }
        metadata = dict(result.metadata or {})
        harness = dict(metadata.get("diagnostic_harness") or {})
        harness["hard_path_status"] = "diagnosable_submitted"
        metadata["diagnostic_harness"] = harness
        return replace(
            result,
            verdict="fault_detected",
            findings=findings,
            confidence=hypothesis.probability,
            reasoning=(
                f"Harness discriminated {hypothesis.fault_type} on "
                f"{interface_candidate.primary_device}:{interface_candidate.primary_interface}; "
                f"supporting evidence: {', '.join(hypothesis.supporting_evidence[:6])}."
            ),
            metadata=metadata,
        )

    @staticmethod
    def _with_secondary_faults(result: DiagnosisResult, faults: tuple[Any, ...]) -> DiagnosisResult:
        metadata = dict(result.metadata or {})
        harness = dict(metadata.get("diagnostic_harness") or {})
        harness["independent_secondary_faults"] = [item.as_finding() for item in faults]
        metadata["diagnostic_harness"] = harness
        if not faults:
            return replace(result, metadata=metadata)
        findings = dict(result.findings or {})
        findings["additional_faults"] = [item.as_finding() for item in faults]
        return replace(result, findings=findings, metadata=metadata)

    @staticmethod
    def _build_gate_inconclusive(result: DiagnosisResult, decision: Any) -> DiagnosisResult:
        missing = ", ".join(decision.missing_requirements) if decision is not None else "unknown"
        return DiagnosticHarness._with_inconclusive_status(
            result,
            status="inconclusive_by_diagnosability_gate",
            reasoning=f"Harness diagnosability gate withheld submission; missing: {missing}.",
        )

    def _write_case_trace(
        self,
        context: Any,
        result: DiagnosisResult,
        *,
        initial: DiagnosisResult,
        normalized: DiagnosisResult,
        decision: Any,
        evidence_store: EvidenceStore,
        probe_history: list[ProbeOutcome] | None = None,
        ranked_interfaces: list[Any] | None = None,
        hypotheses: dict[str, Any] | None = None,
        gate_decision: Any = None,
        budget: ProbeBudget | None = None,
        cache: TTLToolCache | None = None,
    ) -> DiagnosisResult:
        if not self.config.telemetry.enabled:
            return result
        payload = {
            "initial_result": initial,
            "normalized_result": normalized,
            "route_decision": asdict(decision),
            "evidence": [asdict(item) for item in evidence_store.all()],
            "probes": [asdict(item) for item in (probe_history or [])],
            "hypothesis_history": [
                [asdict(item) for item in sorted((hypotheses or {}).values(), key=lambda h: -h.probability)]
            ],
            "interface_rank_history": [[asdict(item) for item in (ranked_interfaces or [])]],
            "diagnosability": asdict(gate_decision) if gate_decision is not None else None,
            "final_result": result,
            "cost": budget.snapshot() if budget is not None else {},
            "budget_allocation": budget.allocation_snapshot() if budget is not None else None,
            "cache": cache.stats if cache is not None else {},
        }
        try:
            path = CaseTraceWriter(self.config.telemetry.output_directory).write(context=context, payload=payload)
        except OSError as exc:
            metadata = dict(result.metadata or {})
            harness = dict(metadata.get("diagnostic_harness") or {})
            harness["telemetry_error"] = f"{type(exc).__name__}: {exc}"
            metadata["diagnostic_harness"] = harness
            return replace(result, metadata=metadata)
        metadata = dict(result.metadata or {})
        harness = dict(metadata.get("diagnostic_harness") or {})
        harness["case_trace"] = str(path)
        metadata["diagnostic_harness"] = harness
        return replace(result, metadata=metadata)

    @staticmethod
    def _with_deferred_status(result: DiagnosisResult, *, status: str) -> DiagnosisResult:
        metadata = dict(result.metadata)
        harness = dict(metadata.get("diagnostic_harness") or {})
        harness["hard_path_status"] = status
        metadata["diagnostic_harness"] = harness
        return replace(result, metadata=metadata)

    @staticmethod
    def _with_contract_inconclusive(result: DiagnosisResult, missing: tuple[str, ...]) -> DiagnosisResult:
        return DiagnosticHarness._with_inconclusive_status(
            result,
            status="inconclusive_by_evidence_contract",
            reasoning="Harness withheld an unverified claim; missing: " + ", ".join(missing),
            evidence_contract_missing=list(missing),
        )

    @staticmethod
    def _build_inconclusive(result: DiagnosisResult, report: Any, *, status: str) -> DiagnosisResult:
        issues = [{"code": issue.code, "field": issue.field, "message": issue.message} for issue in report.issues]
        return DiagnosticHarness._with_inconclusive_status(
            result,
            status=status,
            reasoning="Harness cannot safely submit the initial result: "
            + "; ".join(issue.message for issue in report.issues),
            validation_issues=issues,
        )

    @staticmethod
    def _with_inconclusive_status(
        result: DiagnosisResult,
        *,
        status: str,
        reasoning: str,
        **harness_fields: Any,
    ) -> DiagnosisResult:
        findings = dict(result.findings or {})
        findings["fault_type"] = None
        findings["location"] = {"device": None, "interface": None}
        metadata = dict(result.metadata or {})
        harness = dict(metadata.get("diagnostic_harness") or {})
        harness["hard_path_status"] = status
        harness.update(harness_fields)
        metadata["diagnostic_harness"] = harness
        return replace(
            result,
            verdict="inconclusive",
            findings=findings,
            confidence=0.0,
            reasoning=reasoning,
            metadata=metadata,
        )

    async def aclose(self) -> None:
        close = getattr(self.base_agent, "aclose", None)
        if close is None:
            return
        result = close()
        if inspect.isawaitable(result):
            await result

    def get_capabilities(self) -> list[str]:
        capabilities = list(getattr(self.base_agent, "get_capabilities", lambda: [])())
        return list(dict.fromkeys((*capabilities, "deterministic_diagnostic_harness")))


__all__ = ["DiagnosticHarness"]
