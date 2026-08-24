import asyncio
import json
from datetime import UTC, datetime

import pytest

from examples.agents.diagnostic_harness import DiagnosticHarness, HarnessConfig
from examples.agents.diagnostic_harness.config import (
    CorruptionProbeConfig,
    FeatureConfig,
    MTUProbeConfig,
    PacketLossProbeConfig,
    TelemetryConfig,
)
from examples.agents.diagnostic_harness.evidence import EvidenceStore
from examples.agents.diagnostic_harness.models import Evidence, EvidenceOrigin, RankedInterfaceCandidate
from examples.agents.diagnostic_harness.normalization.interface import TopologyIndex
from examples.agents.diagnostic_harness.orchestrator import (
    _blackhole_semantic_closure_is_admissible,
    _cross_validated_link_loss,
    _has_directional_latency_for_candidate,
    _has_nonbase_latency_observation,
    _has_reliable_abnormal_path_evidence,
    _has_weak_loss_symptom,
    _loss_isolation_conflicts_with_episode,
    _select_latency_directional_candidate,
)
from netopsbench.agents.base import DiagnosticContext
from netopsbench.agents.tracing import AgentTraceRecorder
from netopsbench.platform.toolkit._core.common import ToolResult

from .diagnostic_harness_helpers import BaseAgent, diagnosis_result, write_two_leaf_manifest


def test_route_metadata_agent_name_is_idempotent():
    result = diagnosis_result(
        {
            "verdict": "inconclusive",
            "fault_type": None,
            "device": None,
            "interface": None,
            "confidence": 0.0,
        }
    )
    decision = type("Decision", (), {"fast_path": False, "family": "packet_loss", "reasons": ()})()

    once = DiagnosticHarness._with_route_metadata(result, decision)
    twice = DiagnosticHarness._with_route_metadata(once, decision)

    assert twice.agent_name == once.agent_name


def test_complete_coverage_health_grant_restores_only_missing_budget_dimensions():
    harness = DiagnosticHarness(object())
    budget = harness._new_budget("packet_loss", evidence_store=EvidenceStore())
    with budget.use_stage("family_probe"):
        for _ in range(10):
            budget.reserve_invocation()
        budget.reserve_invocation(packets=480)
    for _ in range(6):
        budget.reserve_probe()

    topology = TopologyIndex(
        devices={f"leaf{index}": "leaf" for index in range(1, 5)},
        source="small-four-attachment-budget-test",
    )
    grant = harness._grant_complete_coverage_healthy_budget(budget, topology)

    assert grant == {"tool_calls": 5, "active_probes": 2, "probe_packets": 20}
    allocation = budget.allocation_snapshot()
    assert allocation["adaptive_escalation"][-1]["reason"] == (
        "complete_active_path_coverage_healthy_verification"
    )


def test_directional_latency_evidence_is_recollected_when_rerank_changes_top_link():
    first = RankedInterfaceCandidate("L1", "leaf1", "Ethernet0", "spine1", "Ethernet0", 0.8)
    reranked = RankedInterfaceCandidate("L2", "leaf1", "Ethernet4", "spine2", "Ethernet0", 0.9)
    evidence = [
        Evidence(
            "E-directional-L1",
            "path",
            "leaf1--spine1",
            "latency_median",
            80.0,
            "latency_link_test",
            datetime.now(UTC),
            covered_links=("L1",),
            observed_path=("L1",),
            path_observation_confidence=1.0,
            metadata={
                "interface_direct_anomaly": True,
                "fault_endpoint_device": "leaf1",
                "fault_endpoint_interface": "Ethernet0",
            },
        )
    ]

    assert _has_directional_latency_for_candidate(evidence, first)
    assert not _has_directional_latency_for_candidate(evidence, reranked)


def test_base_tool_directional_latency_uses_the_same_source_contract():
    candidate = RankedInterfaceCandidate("L1", "leaf1", "Ethernet0", "spine1", "Ethernet0", 0.8)
    evidence = [
        Evidence(
            "E-base-directional",
            "path",
            "leaf1--spine1",
            "latency_median",
            120.0,
            "base_tool:latency_link_test",
            datetime.now(UTC),
            covered_links=("L1",),
            observed_path=("L1",),
            path_observation_confidence=1.0,
            metadata={
                "interface_direct_anomaly": True,
                "fault_endpoint_device": "leaf1",
                "fault_endpoint_interface": "Ethernet0",
            },
        )
    ]

    assert _has_directional_latency_for_candidate(evidence, candidate)


def test_healthy_exact_latency_sample_does_not_satisfy_endpoint_evidence():
    candidate = RankedInterfaceCandidate("L1", "leaf1", "Ethernet0", "spine1", "Ethernet4", 0.8)
    evidence = [
        Evidence(
            "E-healthy-directional",
            "path",
            "leaf1--spine1",
            "latency_median",
            0.4,
            "latency_link_test",
            datetime.now(UTC),
            covered_links=("L1",),
            observed_path=("L1",),
            path_observation_confidence=1.0,
            metadata={"interface_direct_anomaly": False},
        )
    ]

    assert not _has_directional_latency_for_candidate(evidence, candidate)


def test_latency_directional_target_prefers_directly_abnormal_one_hop_link():
    access = RankedInterfaceCandidate(
        "L-access", "leaf2", "Ethernet12", "client4", "eth1", 0.6, endpoint_confidence=0.2
    )
    fabric = RankedInterfaceCandidate(
        "L-fabric", "spine1", "Ethernet4", "leaf2", "Ethernet0", 0.5, endpoint_confidence=0.4
    )
    evidence = [
        Evidence(
            "E-exact-abnormal-rtt",
            "path",
            "spine1--leaf2",
            "latency_median",
            120.0,
            "ping_test_rtt_matrix",
            datetime.now(UTC),
            origin=EvidenceOrigin.ACTIVE_PROBE,
            covered_links=("L-fabric",),
            observed_path=("L-fabric",),
            path_observation_confidence=1.0,
            metadata={
                "link_probe": True,
                "category_anomaly": True,
                "selection": "adaptive_ecmp_link_isolation",
            },
        )
    ]

    assert _select_latency_directional_candidate(evidence, [access, fabric]) is fabric


def test_latency_directional_target_preserves_ranking_without_direct_anomaly():
    first = RankedInterfaceCandidate("L1", "leaf1", "Ethernet0", "spine1", "Ethernet4", 0.8)
    second = RankedInterfaceCandidate("L2", "leaf1", "Ethernet4", "spine2", "Ethernet4", 0.7)

    assert _select_latency_directional_candidate([], [first, second]) is first


def test_weak_loss_can_select_checksum_discriminator_without_becoming_fault_proof():
    store = EvidenceStore(
        [
            Evidence(
                "weak-loss",
                "path",
                "client1--client2",
                "packet_loss_rate",
                0.03,
                "pingmesh_episode",
                datetime.now(UTC),
            )
        ]
    )

    assert _has_weak_loss_symptom(store)
    assert not _has_reliable_abnormal_path_evidence(store, warning_threshold=0.10)


def test_weak_exact_payload_loss_is_promoted_only_after_independent_link_confirmation():
    now = datetime.now(UTC)
    link_id = "spine3:Ethernet20--leaf6:Ethernet8"
    planning = Evidence(
        "payload-missing",
        "path",
        "spine3--leaf6",
        "packet_loss_rate",
        0.075,
        "payload_integrity_link_test",
        now,
        probe_id="payload-integrity-links",
        origin=EvidenceOrigin.ACTIVE_PROBE,
        independence_key="probe:payload",
        supports_submission=False,
        metadata={
            "sent": 80,
            "missing_packets": 6,
            "fault_endpoint_device": "spine3",
            "fault_endpoint_interface": "Ethernet20",
        },
        observed_path=(link_id,),
        possible_paths=((link_id,),),
        covered_links=(link_id,),
        path_observation_confidence=1.0,
    )
    store = EvidenceStore([planning])
    assert not _cross_validated_link_loss(store, warning_threshold=0.10)
    store.add(
        Evidence(
            "repeat-loss",
            "path",
            "spine3--leaf6",
            "packet_loss_rate",
            0.25,
            "ping_test_repeated",
            now,
            probe_id="repeated-packet-loss",
            origin=EvidenceOrigin.ACTIVE_PROBE,
            independence_key="probe:ping",
            metadata={"sent": 20, "rounds": 1},
            observed_path=(link_id,),
            possible_paths=((link_id,),),
            covered_links=(link_id,),
            path_observation_confidence=1.0,
        )
    )

    promoted = _cross_validated_link_loss(store, warning_threshold=0.10)

    assert len(promoted) == 1
    assert promoted[0].supports_submission
    assert promoted[0].metadata["cross_validated"] is True
    assert promoted[0].metadata["fault_endpoint_device"] == "spine3"


def test_two_independent_exact_link_integrity_batches_promote_persistent_access_loss():
    now = datetime.now(UTC)
    link_id = "leaf1:Ethernet28--client4:eth1"
    store = EvidenceStore(
        [
            Evidence(
                f"payload-missing-{index}",
                "path",
                "leaf1--client4",
                "packet_loss_rate",
                rate,
                "payload_integrity_link_test",
                now,
                probe_id=f"payload-integrity-{index}",
                origin=EvidenceOrigin.ACTIVE_PROBE,
                independence_key=f"probe:payload:{index}",
                supports_submission=False,
                metadata={"sent": 80, "missing_packets": missing, "planning_only": True},
                observed_path=(link_id,),
                possible_paths=((link_id,),),
                covered_links=(link_id,),
                path_observation_confidence=1.0,
            )
            for index, (rate, missing) in enumerate(((0.15, 12), (0.125, 10)), start=1)
        ]
    )

    promoted = _cross_validated_link_loss(store, warning_threshold=0.10)

    assert len(promoted) == 1
    assert promoted[0].supports_submission is True
    assert promoted[0].metadata["validation_mode"] == "repeated_payload_batches"


def test_healthy_link_isolation_triggers_integrity_discriminator_without_becoming_fault_evidence():
    now = datetime.now(UTC)
    store = EvidenceStore(
        [
            Evidence(
                "episode-loss",
                "path",
                "client1--client9",
                "packet_loss_rate",
                0.8,
                "pingmesh_episode",
                now,
                reliability=0.9,
            ),
            Evidence(
                "link-1-healthy",
                "path",
                "spine1--leaf5",
                "packet_loss_rate",
                0.0,
                "ping_test_repeated",
                now,
                reliability=1.0,
                metadata={"selection": "link_isolation"},
                observed_path=("spine1--leaf5",),
                possible_paths=(("spine1--leaf5",),),
                covered_links=("spine1--leaf5",),
                path_observation_confidence=1.0,
            ),
            Evidence(
                "link-2-healthy",
                "path",
                "spine2--leaf5",
                "packet_loss_rate",
                0.0,
                "ping_test_repeated",
                now,
                reliability=1.0,
                metadata={"selection": "link_isolation"},
                observed_path=("spine2--leaf5",),
                possible_paths=(("spine2--leaf5",),),
                covered_links=("spine2--leaf5",),
                path_observation_confidence=1.0,
            ),
        ]
    )

    assert _loss_isolation_conflicts_with_episode(store, warning_threshold=0.10)
    assert all(item.category == "packet_loss_rate" for item in store.all())


def test_adaptive_budget_requires_reliable_abnormal_evidence():
    now = datetime.now(UTC)
    errors_only = EvidenceStore(
        [
            Evidence(
                "timeout",
                "tool",
                "ping",
                "tool_error",
                {"error": "timeout"},
                "ping_test",
                now,
                reliability=0.0,
            )
        ]
    )
    abnormal = EvidenceStore(
        [
            Evidence(
                "loss",
                "path",
                "client1--client9",
                "packet_loss_rate",
                0.2,
                "pingmesh_episode",
                now,
                reliability=0.9,
            )
        ]
    )

    assert not _has_reliable_abnormal_path_evidence(errors_only, warning_threshold=0.10)
    assert _has_reliable_abnormal_path_evidence(abnormal, warning_threshold=0.10)


def test_real_latency_observation_survives_a_healthy_first_ecmp_sample():
    store = EvidenceStore(
        [
            Evidence(
                "episode-latency",
                "path",
                "client1--client9",
                "latency_p95",
                18.0,
                "pingmesh_episode",
                datetime.now(UTC),
                reliability=0.9,
                origin=EvidenceOrigin.PUBLIC_OBSERVATION,
                metadata={"category_anomaly": True},
            ),
            Evidence(
                "healthy-first-member",
                "path",
                "spine1--leaf9",
                "latency_median",
                0.5,
                "ping_link_test",
                datetime.now(UTC),
                origin=EvidenceOrigin.ACTIVE_PROBE,
            ),
        ]
    )

    assert _has_nonbase_latency_observation(store)

    prose_only = EvidenceStore(
        [
            Evidence(
                "base-latency",
                "path",
                "client1--client9",
                "latency_p95",
                80.0,
                "base_diagnosis",
                datetime.now(UTC),
                origin=EvidenceOrigin.BASE_CLAIM,
            )
        ]
    )
    assert not _has_nonbase_latency_observation(prose_only)


def test_blackhole_semantic_submit_requires_selected_route_bound_to_loss_destination():
    now = datetime.now(UTC)
    result = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "blackhole_route",
            "device": "leaf5",
            "interface": None,
            "confidence": 0.95,
        }
    )
    route = Evidence(
        "route",
        "device",
        "leaf5",
        "route_presence",
        {"destination": "192.0.2.9", "selected": True, "is_discard": True},
        "get_route_table",
        now,
        reliability=1.0,
        metadata={"destination": "192.0.2.9"},
    )
    matching_loss = Evidence(
        "loss",
        "path",
        "client1--client9",
        "packet_loss_rate",
        1.0,
        "pingmesh_episode",
        now,
        reliability=1.0,
        metadata={"src_ip": "192.0.2.1", "dst_ip": "192.0.2.9"},
    )
    unrelated_loss = Evidence(
        "other-loss",
        "path",
        "client2--client3",
        "packet_loss_rate",
        1.0,
        "pingmesh_episode",
        now,
        reliability=1.0,
        metadata={"src_ip": "192.0.2.2", "dst_ip": "192.0.2.3"},
    )

    assert _blackhole_semantic_closure_is_admissible(
        result,
        EvidenceStore([route, matching_loss]),
        minimum_confidence=0.75,
    )
    assert not _blackhole_semantic_closure_is_admissible(
        result,
        EvidenceStore([route, unrelated_loss]),
        minimum_confidence=0.75,
    )


def test_blackhole_semantic_submit_accepts_independent_live_config_and_selected_rib():
    now = datetime.now(UTC)
    result = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "blackhole_route",
            "device": "leaf5",
            "interface": None,
            "confidence": 0.95,
        }
    )
    route = Evidence(
        "route-live",
        "device",
        "leaf5",
        "route_presence",
        {"prefix": "198.51.100.0/24", "selected": True, "is_discard": True},
        "get_route_table",
        now,
        independence_key="tool:get_route_table:leaf5:198.51.100.0/24",
    )
    config = Evidence(
        "config-live",
        "device",
        "leaf5",
        "configured_static_route",
        {"prefix": "198.51.100.0/24", "next_hop": "Null0"},
        "get_device_config",
        now,
        independence_key="tool:get_device_config:leaf5",
    )

    assert _blackhole_semantic_closure_is_admissible(
        result,
        EvidenceStore([route, config]),
        minimum_confidence=0.75,
    )


def test_blackhole_semantic_submit_accepts_selected_rib_with_multisource_partial_loss():
    now = datetime.now(UTC)
    result = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "blackhole_route",
            "device": "spine3",
            "interface": None,
            "confidence": 0.95,
        }
    )
    route = Evidence(
        "route-partial",
        "device",
        "spine3",
        "route_presence",
        {"prefix": "198.51.100.0/30", "selected": True, "is_discard": True},
        "get_route_table",
        now,
    )
    losses = [
        Evidence(
            f"loss-{index}",
            "path",
            f"client{index}--target",
            "packet_loss_rate",
            0.25,
            "pingmesh_episode",
            now,
            metadata={"src_leaf": f"leaf{index}", "dst_ip": "198.51.100.2"},
        )
        for index in range(1, 4)
    ]

    assert _blackhole_semantic_closure_is_admissible(
        result,
        EvidenceStore([route, *losses]),
        minimum_confidence=0.75,
    )
    assert not _blackhole_semantic_closure_is_admissible(
        result,
        EvidenceStore([route, *losses[:2]]),
        minimum_confidence=0.75,
    )


def test_contract_veto_preserves_observation_derived_runtime_semantic_family():
    result = diagnosis_result(
        {
            "verdict": "network_healthy",
            "fault_type": None,
            "device": None,
            "interface": None,
            "confidence": 0.85,
        }
    )

    assert DiagnosticHarness._verification_family(result, "runtime_semantic") == "runtime_semantic"


def _context():
    return DiagnosticContext(
        scenario_id="opaque-public-id",
        topology={
            "devices": {
                "leafs": [{"name": "leaf1"}, {"name": "leaf5"}],
                "spines": [{"name": "spine3"}],
                "clients": [],
            },
            "links": [],
        },
        symptoms={"observations": {}},
    )


def test_disabled_harness_returns_exact_original_result():
    initial = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "link_flap",
            "device": "leaf5",
            "interface": "eth2",
            "confidence": 0.95,
            "evidence": ["Flapping observed."],
        }
    )
    base = BaseAgent(initial)

    result = asyncio.run(DiagnosticHarness(base, config=HarnessConfig(enabled=False)).diagnose(_context()))

    assert result is initial
    assert base.calls == 1


def test_base_prose_alone_cannot_fast_path_device_down():
    initial = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "device_down",
            "device": "leaf1",
            "interface": None,
            "confidence": 0.95,
            "evidence": ["Only leaf1 is unreachable."],
        }
    )
    base = BaseAgent(initial)

    result = asyncio.run(DiagnosticHarness(base).diagnose(_context()))

    assert result.verdict == "inconclusive"
    assert result.findings["fault_type"] is None
    assert not result.metadata["diagnostic_harness"]["route_decision"]["fast_path"]
    assert result.metadata["diagnostic_harness"]["hard_path_status"] == "inconclusive_by_evidence_contract"
    assert base.calls == 1


def test_wrapper_contains_provider_exception_without_copying_sensitive_error_text():
    class RaisingAgent:
        name = "third-party-agent"

        async def diagnose(self, _context):
            raise RuntimeError("provider error api_key=must-not-appear")

    result = asyncio.run(DiagnosticHarness(RaisingAgent()).diagnose(_context()))

    assert result.verdict == "inconclusive"
    assert result.success is False
    assert "must-not-appear" not in repr(result)
    harness = result.metadata["diagnostic_harness"]
    assert harness["base_agent_reliability"]["status"] == "tool_loop_failed"
    assert "base_status_tool_loop_failed" in harness["route_decision"]["reasons"]


def test_wrapper_contains_non_diagnosis_result_as_schema_failure():
    class MappingAgent:
        name = "mapping-agent"

        async def diagnose(self, _context):
            return {"verdict": "fault_detected", "fault_type": "packet_loss"}

    result = asyncio.run(DiagnosticHarness(MappingAgent()).diagnose(_context()))

    assert result.verdict == "inconclusive"
    assert result.success is False
    harness = result.metadata["diagnostic_harness"]
    assert harness["base_agent_reliability"]["status"] == "schema_failed"
    assert "base_status_schema_failed" in harness["route_decision"]["reasons"]


def test_orchestrator_reuses_wrapped_agent_live_tool_trace_independent_of_base_label(tmp_path):
    class TraceRecordingAgent:
        name = "provider-neutral-base"

        async def diagnose(self, context):
            context.trace.record_tool_start(
                name="get_device_interfaces",
                args={"device": "leaf2"},
                run_id="base-interface-call",
            )
            context.trace.record_tool_end(
                output={
                    "device": "leaf2",
                    "interfaces": [{"name": "Ethernet0", "admin": "up", "oper": "down"}],
                },
                run_id="base-interface-call",
            )
            return diagnosis_result(
                {
                    "verdict": "fault_detected",
                    "fault_type": "route_policy_misconfig",
                    "device": "leaf1",
                    "confidence": 0.95,
                    "evidence": ["The provider emitted an unrelated label."],
                }
            )

    class Tools:
        def __init__(self):
            self.calls = []

        def get_device_interfaces(self, *, device):
            self.calls.append(device)
            assert device in {"leaf1", "leaf2", "spine1"}
            interface = "Ethernet4" if device == "spine1" else "Ethernet0"
            oper = "up" if device == "leaf1" else "down"
            return ToolResult(
                success=True,
                data={
                    "device": device,
                    "interfaces": [{"name": interface, "admin": "up", "oper": oper}],
                },
            )

    context = DiagnosticContext(
        scenario_id="opaque-provider-contract",
        topology={},
        symptoms={"observations": {}},
        metadata={"worker_env": {"NETOPSBENCH_TOPOLOGY_DIR": str(_write_manifest(tmp_path).parent)}},
        trace=AgentTraceRecorder(),
    )
    tools = Tools()
    context.tools = tools

    result = asyncio.run(DiagnosticHarness(TraceRecordingAgent()).diagnose(context))

    assert result.verdict == "fault_detected", result
    assert result.findings["fault_type"] == "link_down"
    assert result.findings["location"] == {"device": "leaf2", "interface": "Ethernet0"}
    assert tools.calls[0] == "leaf2"
    assert {"leaf2", "spine1"} <= set(tools.calls)
    harness = result.metadata["diagnostic_harness"]
    assert harness["route_decision"]["family"] == "link_state_verification"
    assert any(item["source"] == "base_tool:get_device_interfaces" for item in harness["evidence"])


def test_link_down_prose_trace_is_a_claim_not_live_interface_evidence(tmp_path):
    initial = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "link_down",
            "device": "leaf1",
            "interface": "Ethernet0",
            "confidence": 0.95,
            "evidence": [
                "leaf1 get_device_interfaces shows Ethernet0 oper:down admin:down",
                "spine1:Ethernet0 oper=down admin=down on the physical peer",
            ],
        }
    )
    context = DiagnosticContext(
        scenario_id="opaque-link-down-replay",
        topology={},
        symptoms={"observations": {}},
        metadata={"worker_env": {"NETOPSBENCH_TOPOLOGY_DIR": str(_write_manifest(tmp_path).parent)}},
    )
    base = BaseAgent(initial)

    result = asyncio.run(DiagnosticHarness(base).diagnose(context))

    assert result.verdict == "inconclusive"
    assert result.findings["fault_type"] is None
    assert result.metadata["diagnostic_harness"]["base_agent_reliability"] == {
        "status": "valid",
        "reasons": [],
        "direct_evidence": True,
        "semantic_conflict": False,
    }
    assert not result.metadata["diagnostic_harness"]["route_decision"]["fast_path"]
    assert base.calls == 1


def test_route_policy_semantic_conflict_requires_live_config_confirmation():
    initial = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "bgp_neighbor_misconfig",
            "device": "leaf5",
            "interface": None,
            "confidence": 0.95,
            "evidence": [
                "All BGP sessions are Established; the route-map network statement for the affected prefix is missing."
            ],
        }
    )

    result = asyncio.run(DiagnosticHarness(BaseAgent(initial)).diagnose(_context()))

    assert result.verdict == "inconclusive"
    assert result.findings["fault_type"] is None
    harness = result.metadata["diagnostic_harness"]
    assert harness["hard_path_status"] == "inconclusive_by_evidence_contract"
    assert "diagnosability" not in harness
    assert "payload_integrity_valid" not in harness["evidence_contract_missing"]


def test_route_policy_adapter_cannot_overwrite_canonical_link_flapping(tmp_path):
    initial = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "link_flap",
            "device": "leaf2",
            "interface": "Ethernet0",
            "confidence": 0.95,
            "evidence": [
                "BGP session flap ESTABLISHED→ACTIVE→ESTABLISHED was observed on leaf2.",
                "leaf2 device logs show Ethernet0 went down twice during the episode.",
                "BGP config is clean with no static routes or route-map filters.",
            ],
        }
    )
    context = DiagnosticContext(
        scenario_id="opaque-link-flap-negative-policy-replay",
        topology={},
        symptoms={"observations": {}},
        metadata={"worker_env": {"NETOPSBENCH_TOPOLOGY_DIR": str(_write_manifest(tmp_path).parent)}},
    )

    result = asyncio.run(DiagnosticHarness(BaseAgent(initial)).diagnose(context))

    assert result.verdict == "inconclusive"
    assert result.findings["fault_type"] is None
    harness = result.metadata["diagnostic_harness"]
    assert not harness["route_decision"]["fast_path"]
    assert harness["route_decision"]["family"] == "temporal_verification"
    assert "route_policy_resolution" not in harness


def test_missing_connected_interface_policy_label_is_verified_as_link_down(tmp_path):
    class Tools:
        def get_device_interfaces(self, *, device):
            assert device == "leaf2"
            return ToolResult(
                success=True,
                data={"interfaces": [{"name": "Ethernet0", "admin": "down", "oper": "down"}]},
            )

    initial = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "route_policy_misconfig",
            "device": "leaf2",
            "interface": None,
            "confidence": 0.95,
            "evidence": [
                "192.168.102.8/30 is the subnet for which leaf2 has no connected interface.",
                "The BGP network statement cannot activate without a matching connected route.",
            ],
        }
    )
    context = DiagnosticContext(
        scenario_id="opaque-connected-interface-replay",
        topology={},
        symptoms={"observations": {}},
        metadata={"worker_env": {"NETOPSBENCH_TOPOLOGY_DIR": str(_write_manifest(tmp_path).parent)}},
    )
    context.tools = Tools()

    result = asyncio.run(DiagnosticHarness(BaseAgent(initial)).diagnose(context))

    assert result.verdict == "fault_detected"
    assert result.findings["fault_type"] == "link_down"
    assert result.findings["location"] == {"device": "leaf2", "interface": "Ethernet0"}
    assert result.metadata["diagnostic_harness"]["route_decision"]["family"] == "link_state_verification"


def test_invalid_hard_path_returns_inconclusive_until_probes_exist():
    initial = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "mtu_mismatch",
            "device": "leaf5",
            "interface": None,
            "confidence": 0.8,
            "evidence": ["Large packets fail."],
        }
    )

    result = asyncio.run(DiagnosticHarness(BaseAgent(initial)).diagnose(_context()))

    assert result.verdict == "inconclusive"
    assert result.metadata["diagnostic_harness"]["hard_path_status"] == "inconclusive_by_evidence_contract"


def test_enabled_hard_path_executes_real_ping_contract_with_shared_budget():
    class Tools:
        def __init__(self):
            self.calls = []

        def ping_test(self, **arguments):
            self.calls.append(arguments)
            count = arguments["count"]
            return ToolResult(
                success=True,
                data={
                    "source": arguments["src"],
                    "destination": arguments["dst_ip"],
                    "count": count,
                    "payload_size": arguments["payload_size"],
                    "dont_fragment": arguments["dont_fragment"],
                    "output": f"{count} packets transmitted, {count - 1} received, 50% packet loss\n",
                    "return_code": 1,
                },
            )

    initial = diagnosis_result(
        {
            "verdict": "network_healthy",
            "fault_type": None,
            "device": None,
            "interface": None,
            "confidence": 0.9,
            "evidence": ["Routes are present despite sparse packet loss."],
        }
    )
    context = _context()
    context.tools = Tools()
    context.symptoms = {
        "observations": {
            "pingmesh_metrics": {
                "query_status": {"ok": True},
                "anomalies": [
                    {
                        "type": "packet_loss",
                        "src_name": "client1",
                        "dst_ip": "192.0.2.2",
                        "dst_name": "client9",
                        "value": 10.0,
                        "sample_count": 30,
                        "persistence": "persistent",
                    }
                ],
            }
        }
    }
    config = HarnessConfig(
        impairment_probes=FeatureConfig(enabled=True),
        packet_loss_probe=PacketLossProbeConfig(packets_per_pair=2, repeat_rounds=1),
    )

    result = asyncio.run(DiagnosticHarness(BaseAgent(initial), config=config).diagnose(context))

    harness = result.metadata["diagnostic_harness"]
    assert len(context.tools.calls) == 1
    assert harness["probe_outcome"]["probe_id"] == "repeated-packet-loss"
    assert harness["probe_outcome"]["status"] == "completed"
    assert harness["cost"] == {"tool_calls": 1, "active_probes": 1, "probe_packets": 2}
    assert any(item["category"] == "packet_loss_rate" for item in harness["evidence"])
    assert harness["hard_path_status"] == "inconclusive_by_evidence_contract"


def _write_manifest(tmp_path):
    return write_two_leaf_manifest(tmp_path)


def _write_ecmp_manifest(tmp_path):
    path = _write_manifest(tmp_path)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["facts"]["num_spines"] = 2
    manifest["facts"]["total_switches"] = 4
    manifest["devices"].insert(1, {"name": "spine2", "role": "spine"})
    manifest["links"] = [
        {
            "kind": "spine-leaf",
            "endpoints": [{"device": "spine1", "interface": "eth1"}, {"device": "leaf1", "interface": "eth1"}],
        },
        {
            "kind": "spine-leaf",
            "endpoints": [{"device": "spine2", "interface": "eth1"}, {"device": "leaf1", "interface": "eth2"}],
        },
        {
            "kind": "spine-leaf",
            "endpoints": [{"device": "spine1", "interface": "eth2"}, {"device": "leaf2", "interface": "eth1"}],
        },
        {
            "kind": "spine-leaf",
            "endpoints": [{"device": "spine2", "interface": "eth2"}, {"device": "leaf2", "interface": "eth2"}],
        },
        *[link for link in manifest["links"] if link["kind"] == "client-leaf"],
    ]
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def test_phase5_mtu_path_uses_peer_difference_and_writes_case_trace(tmp_path):
    class Tools:
        def ping_test(self, **arguments):
            count = arguments["count"]
            received = count if arguments["payload_size"] == 64 else 0
            return ToolResult(
                success=True,
                data={
                    "source": arguments["src"],
                    "destination": arguments["dst_ip"],
                    "count": count,
                    "payload_size": arguments["payload_size"],
                    "dont_fragment": arguments["dont_fragment"],
                    "output": f"{count} packets transmitted, {received} received, 0% packet loss\n",
                    "return_code": 0 if received else 1,
                },
            )

        def get_device_interfaces(self, *, device):
            inventories = {
                "spine1": [
                    {"name": "Ethernet0", "admin": "up", "oper": "up", "mtu": 1400},
                    {"name": "Ethernet4", "admin": "up", "oper": "up", "mtu": 9100},
                ],
                "leaf1": [{"name": "Ethernet0", "admin": "up", "oper": "up", "mtu": 9100}],
                "leaf2": [{"name": "Ethernet0", "admin": "up", "oper": "up", "mtu": 9100}],
            }
            return ToolResult(success=True, data={"interfaces": inventories[device]})

    initial = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "mtu_mismatch",
            "device": "leaf1",
            "interface": None,
            "confidence": 0.7,
            "evidence": ["Large packets fail."],
        }
    )
    context = DiagnosticContext(
        scenario_id="opaque-mtu-case",
        topology={},
        symptoms={
            "observations": {
                "pingmesh_metrics": {
                    "anomalies": [
                        {
                            "type": "mtu_or_fragmentation_suspect",
                            "src_name": "client1",
                            "dst_name": "client2",
                            "dst_ip": "192.0.2.2",
                            "src_leaf": "leaf1",
                            "dst_leaf": "leaf2",
                            "value": 100.0,
                            "sample_count": 30,
                            "persistence": "persistent",
                        }
                    ]
                }
            }
        },
        metadata={
            "runtime_id": "test-runtime",
            "worker_env": {"NETOPSBENCH_TOPOLOGY_DIR": str(_write_manifest(tmp_path).parent)},
        },
    )
    context.tools = Tools()
    config = HarnessConfig(
        impairment_probes=FeatureConfig(enabled=True),
        topology_ranker=FeatureConfig(enabled=True),
        diagnosability_gate=FeatureConfig(enabled=True),
        mtu_probe=MTUProbeConfig(payload_sizes=(64, 1472), packets_per_size=2),
        telemetry=TelemetryConfig(enabled=True, output_directory=str(tmp_path / "traces")),
    )

    result = asyncio.run(DiagnosticHarness(BaseAgent(initial), config=config).diagnose(context))

    assert result.verdict == "fault_detected"
    assert result.findings["fault_type"] == "mtu_mismatch"
    assert result.findings["location"] == {"device": "spine1", "interface": "Ethernet0"}
    harness = result.metadata["diagnostic_harness"]
    assert harness["diagnosability"]["can_submit"]
    assert harness["cache"] == {
        "hits": 0,
        "misses": 3,
        "size": 3,
        "active_probe_ttl_seconds": 0.0,
        "active_probe_reuse": False,
    }
    traces = list((tmp_path / "traces" / "test-runtime").glob("trace-*.json"))
    assert len(traces) == 1
    trace = traces[0]
    assert "opaque-mtu-case" not in trace.read_text(encoding="utf-8")
    payload = json.loads(trace.read_text(encoding="utf-8"))
    assert payload["final_result"]["findings"]["fault_type"] == "mtu_mismatch"
    assert any(item["category"] == "configuration_difference" for item in payload["evidence"])


def test_unknown_ecmp_healthy_sweep_collects_peers_but_does_not_submit_from_passive_mtu_suspect(tmp_path):
    class Tools:
        def __init__(self):
            self.interface_calls = []

        def ping_test(self, **arguments):
            count = arguments["count"]
            return ToolResult(
                success=True,
                data={
                    "source": arguments["src"],
                    "destination": arguments["dst_ip"],
                    "count": count,
                    "payload_size": arguments["payload_size"],
                    "dont_fragment": arguments["dont_fragment"],
                    "output": f"{count} packets transmitted, {count} received, 0% packet loss\n",
                    "return_code": 0,
                },
            )

        def get_device_interfaces(self, *, device):
            self.interface_calls.append(device)
            inventories = {
                "spine1": [
                    {"name": "Ethernet0", "admin": "up", "oper": "up", "mtu": 9100},
                    {"name": "Ethernet4", "admin": "up", "oper": "up", "mtu": 9100},
                ],
                "spine2": [
                    {"name": "Ethernet0", "admin": "up", "oper": "up", "mtu": 1400},
                    {"name": "Ethernet4", "admin": "up", "oper": "up", "mtu": 9100},
                ],
                "leaf1": [
                    {"name": "Ethernet0", "admin": "up", "oper": "up", "mtu": 9100},
                    {"name": "Ethernet4", "admin": "up", "oper": "up", "mtu": 9100},
                ],
                "leaf2": [
                    {"name": "Ethernet0", "admin": "up", "oper": "up", "mtu": 9100},
                    {"name": "Ethernet4", "admin": "up", "oper": "up", "mtu": 9100},
                ],
            }
            return ToolResult(success=True, data={"interfaces": inventories[device]})

    initial = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "mtu_mismatch",
            "device": None,
            "interface": None,
            "confidence": 0.7,
            "evidence": ["Pingmesh suggests a size-related impairment."],
        }
    )
    context = DiagnosticContext(
        scenario_id="opaque-ecmp-mtu-case",
        topology={},
        symptoms={
            "observations": {
                "pingmesh_metrics": {
                    "anomalies": [
                        {
                            "type": "mtu_or_fragmentation_suspect",
                            "src_name": "client1",
                            "dst_name": "client2",
                            "dst_ip": "192.0.2.2",
                            "src_leaf": "leaf1",
                            "dst_leaf": "leaf2",
                            "value": 100.0,
                            "sample_count": 30,
                            "persistence": "persistent",
                        }
                    ]
                }
            }
        },
        metadata={
            "runtime_id": "test-ecmp-runtime",
            "worker_env": {"NETOPSBENCH_TOPOLOGY_DIR": str(_write_ecmp_manifest(tmp_path).parent)},
        },
    )
    tools = Tools()
    context.tools = tools
    config = HarnessConfig(
        impairment_probes=FeatureConfig(enabled=True),
        topology_ranker=FeatureConfig(enabled=True),
        diagnosability_gate=FeatureConfig(enabled=True),
        mtu_probe=MTUProbeConfig(payload_sizes=(64, 1472), packets_per_size=2),
    )

    result = asyncio.run(DiagnosticHarness(BaseAgent(initial), config=config).diagnose(context))

    harness = result.metadata["diagnostic_harness"]
    assert harness["peer_collection"] == {
        "attempted": True,
        "trigger": "unresolved_ecmp_ambiguity",
        "ecmp_ambiguity": True,
        "candidate_count": 6,
    }
    assert set(tools.interface_calls) == {"client1", "client2", "leaf1", "leaf2", "spine1", "spine2"}
    assert any(item["category"] == "configuration_difference" for item in harness["evidence"])
    assert result.verdict == "inconclusive"
    assert "size_dependent_failure" in result.metadata["diagnostic_harness"]["diagnosability"]["missing_requirements"]


def test_unknown_ecmp_healthy_sweep_closes_after_exact_peer_link_df_confirmation(tmp_path):
    class Tools:
        def ping_test(self, **arguments):
            count = arguments["count"]
            return ToolResult(
                success=True,
                data={
                    "source": arguments["src"],
                    "destination": arguments["dst_ip"],
                    "count": count,
                    "payload_size": arguments["payload_size"],
                    "dont_fragment": arguments["dont_fragment"],
                    "output": f"{count} packets transmitted, {count} received, 0% packet loss\n",
                    "return_code": 0,
                },
            )

        def ping_link_test(self, **arguments):
            count = arguments["count"]
            received = count if arguments["payload_size"] + 28 <= 1400 else 0
            return ToolResult(
                success=True,
                data={
                    "source": arguments["src"],
                    "destination": arguments["target_device"],
                    "count": count,
                    "payload_size": arguments["payload_size"],
                    "dont_fragment": arguments["dont_fragment"],
                    "output": f"{count} packets transmitted, {received} received, 0% packet loss\n",
                    "return_code": 0 if received else 1,
                },
            )

        def get_device_interfaces(self, *, device):
            inventories = {
                "spine1": [
                    {"name": "Ethernet0", "admin": "up", "oper": "up", "mtu": 9100},
                    {"name": "Ethernet4", "admin": "up", "oper": "up", "mtu": 9100},
                ],
                "spine2": [
                    {"name": "Ethernet0", "admin": "up", "oper": "up", "mtu": 1400},
                    {"name": "Ethernet4", "admin": "up", "oper": "up", "mtu": 9100},
                ],
                "leaf1": [
                    {"name": "Ethernet0", "admin": "up", "oper": "up", "mtu": 9100},
                    {"name": "Ethernet4", "admin": "up", "oper": "up", "mtu": 9100},
                ],
                "leaf2": [
                    {"name": "Ethernet0", "admin": "up", "oper": "up", "mtu": 9100},
                    {"name": "Ethernet4", "admin": "up", "oper": "up", "mtu": 9100},
                ],
            }
            return ToolResult(success=True, data={"interfaces": inventories[device]})

    initial = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "mtu_mismatch",
            "device": None,
            "interface": None,
            "confidence": 0.7,
            "evidence": ["A public observation suggests a size-related impairment."],
        }
    )
    context = DiagnosticContext(
        scenario_id="opaque-ecmp-mtu-link-confirmation",
        topology={},
        symptoms={
            "observations": {
                "pingmesh_metrics": {
                    "anomalies": [
                        {
                            "type": "mtu_or_fragmentation_suspect",
                            "src_name": "client1",
                            "dst_name": "client2",
                            "dst_ip": "192.0.2.2",
                            "src_leaf": "leaf1",
                            "dst_leaf": "leaf2",
                            "value": 100.0,
                            "sample_count": 30,
                            "persistence": "persistent",
                        }
                    ]
                }
            }
        },
        metadata={
            "runtime_id": "test-ecmp-mtu-link-confirmation",
            "worker_env": {"NETOPSBENCH_TOPOLOGY_DIR": str(_write_ecmp_manifest(tmp_path).parent)},
        },
    )
    context.tools = Tools()
    config = HarnessConfig(
        impairment_probes=FeatureConfig(enabled=True),
        topology_ranker=FeatureConfig(enabled=True),
        diagnosability_gate=FeatureConfig(enabled=True),
        mtu_probe=MTUProbeConfig(payload_sizes=(64, 1472), packets_per_size=2),
    )

    result = asyncio.run(DiagnosticHarness(BaseAgent(initial), config=config).diagnose(context))

    assert result.verdict == "fault_detected"
    assert result.findings["fault_type"] == "mtu_mismatch"
    assert result.findings["location"] == {"device": "spine2", "interface": "Ethernet0"}
    history = result.metadata["diagnostic_harness"]["probe_history"]
    assert [item["probe_id"] for item in history] == ["mtu-packet-size-sweep", "mtu-physical-link-sweep"]
    assert result.metadata["diagnostic_harness"]["diagnosability"]["can_submit"] is True


def test_recursion_mtu_branch_preserves_sweep_and_peer_collection_budget(tmp_path):
    class Tools:
        def __init__(self):
            self.ping_sizes = []
            self.interface_calls = []

        def ping_test(self, **arguments):
            self.ping_sizes.append(arguments["payload_size"])
            count = arguments["count"]
            received = count if arguments["payload_size"] <= 1372 else 0
            return ToolResult(
                success=True,
                data={
                    "source": arguments["src"],
                    "destination": arguments["dst_ip"],
                    "count": count,
                    "payload_size": arguments["payload_size"],
                    "dont_fragment": arguments["dont_fragment"],
                    "output": f"{count} packets transmitted, {received} received, 0% packet loss\n",
                    "return_code": 0 if received else 1,
                },
            )

        def get_device_interfaces(self, *, device):
            self.interface_calls.append(device)
            inventories = {
                "spine1": [
                    {"name": "Ethernet0", "admin": "up", "oper": "up", "mtu": 9100},
                    {"name": "Ethernet4", "admin": "up", "oper": "up", "mtu": 1400},
                ],
                "leaf1": [{"name": "Ethernet0", "admin": "up", "oper": "up", "mtu": 9100}],
                "leaf2": [{"name": "Ethernet0", "admin": "up", "oper": "up", "mtu": 9100}],
            }
            return ToolResult(success=True, data={"interfaces": inventories[device]})

    initial = diagnosis_result(
        {
            "verdict": "inconclusive",
            "fault_type": None,
            "device": None,
            "interface": None,
            "confidence": 0.0,
            "evidence": [],
            "reasoning": "Graph recursion limit reached before a diagnosis was emitted.",
        }
    )
    context = DiagnosticContext(
        scenario_id="opaque-recursion-mtu",
        topology={},
        symptoms={
            "observations": {
                "pingmesh_metrics": {
                    "anomalies": [
                        {
                            "type": "mtu_or_fragmentation_suspect",
                            "src_name": "client1",
                            "dst_name": "client2",
                            "dst_ip": "192.0.2.2",
                            "src_leaf": "leaf1",
                            "dst_leaf": "leaf2",
                            "value": 100.0,
                            "sample_count": 30,
                            "persistence": "persistent",
                        }
                    ]
                }
            }
        },
        metadata={
            "runtime_id": "test-recursion-mtu",
            "worker_env": {"NETOPSBENCH_TOPOLOGY_DIR": str(_write_manifest(tmp_path).parent)},
        },
    )
    tools = Tools()
    context.tools = tools
    config = HarnessConfig(
        impairment_probes=FeatureConfig(enabled=True),
        topology_ranker=FeatureConfig(enabled=True),
        diagnosability_gate=FeatureConfig(enabled=True),
    )

    result = asyncio.run(DiagnosticHarness(BaseAgent(initial), config=config).diagnose(context))

    harness = result.metadata["diagnostic_harness"]
    assert result.verdict == "fault_detected"
    assert result.findings["fault_type"] == "mtu_mismatch"
    assert tools.ping_sizes == [64, 512, 1200, 1372, 1400, 1472, 8972]
    assert set(tools.interface_calls) == {"leaf1", "leaf2", "spine1"}
    assert [item["probe_id"] for item in harness["probe_history"]] == ["mtu-packet-size-sweep"]
    allocation = harness["budget_allocation"]
    assert allocation["budget_reserved_by_family"] == {
        "family_probe": 7,
        "evidence_collection": 5,
        "final_verification": 0,
    }
    assert allocation["budget_spent_semantic_closure"] == 0
    assert allocation["budget_spent_family_probe"] == 7
    assert allocation["budget_spent_evidence_collection"] == 3


def test_recursion_healthy_case_uses_integrity_backed_verified_closure(tmp_path):
    class Tools:
        def query_bgp_events(self, **_arguments):
            return ToolResult(success=True, data={"events": []})

        def get_device_interfaces(self, *, device):
            return ToolResult(
                success=True,
                data={"interfaces": [{"name": "Ethernet0", "admin": "up", "oper": "up"}]},
            )

        def get_bgp_neighbors(self, *, device):
            return ToolResult(success=True, data={"neighbors": [{"state": "ESTABLISHED"}]})

        def get_route_table(self, *, device, **_arguments):
            return ToolResult(success=True, data={"routes": [{"prefix": "0.0.0.0/0"}]})

        def payload_integrity_test(self, **arguments):
            count = arguments["count"]
            return ToolResult(
                success=True,
                data={
                    "received": True,
                    "checksum_valid": True,
                    "sequence": 1,
                    "packets_sent": count,
                    "packets_observed": count,
                    "missing_packets": 0,
                    "integrity_complete": True,
                },
            )

    initial = diagnosis_result(
        {
            "verdict": "inconclusive",
            "fault_type": None,
            "device": None,
            "interface": None,
            "confidence": 0.0,
            "evidence": [],
            "reasoning": "Graph recursion limit reached without a stop condition.",
        }
    )
    context = _context()
    context.metadata = {"worker_env": {"NETOPSBENCH_TOPOLOGY_DIR": str(_write_manifest(tmp_path).parent)}}
    context.symptoms = {
        "observations": {
            "data_source_status": "ok",
            "coverage_status": "complete",
            "anomalies_detected": False,
            "start_time": "2026-08-03T00:00:00Z",
            "end_time": "2026-08-03T00:00:30Z",
            "pingmesh_metrics": {"query_status": {"ok": True}, "anomalies": []},
        }
    }
    context.tools = Tools()

    result = asyncio.run(DiagnosticHarness(BaseAgent(initial)).diagnose(context))

    harness = result.metadata["diagnostic_harness"]
    assert result.verdict == "network_healthy"
    assert harness["base_agent_reliability"]["status"] == "recursion_failed"
    assert harness["hard_path_status"] == "verified_healthy_closure"
    assert harness["cost"] == {"tool_calls": 5, "active_probes": 1, "probe_packets": 20}
    assert harness["budget_allocation"]["budget_spent_triage"] == 5


def test_low_evidence_route_policy_is_vetoed_and_direct_link_down_wins(tmp_path):
    class Tools:
        def get_device_interfaces(self, *, device):
            assert device == "leaf2"
            return ToolResult(
                success=True,
                data={"interfaces": [{"name": "Ethernet0", "admin": "down", "oper": "down"}]},
            )

    initial = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "route_policy_misconfig",
            "device": "leaf2",
            "interface": "Ethernet16",
            "confidence": 0.95,
            "evidence": [
                "The configured network statement has no matching connected interface, and the destination is unreachable."
            ],
        }
    )
    context = DiagnosticContext(
        scenario_id="opaque-link-state-replay",
        topology={},
        symptoms={
            "observations": {
                "pingmesh_metrics": {
                    "anomalies": [
                        {
                            "type": "path_unreachable",
                            "src_name": "client1",
                            "dst_name": "client2",
                            "src_leaf": "leaf1",
                            "dst_leaf": "leaf2",
                            "value": 100.0,
                            "sample_count": 30,
                            "persistence": "persistent",
                        }
                    ]
                }
            }
        },
        metadata={"worker_env": {"NETOPSBENCH_TOPOLOGY_DIR": str(_write_manifest(tmp_path).parent)}},
    )
    context.tools = Tools()

    result = asyncio.run(DiagnosticHarness(BaseAgent(initial)).diagnose(context))

    harness = result.metadata["diagnostic_harness"]
    assert result.verdict == "fault_detected"
    assert result.findings["fault_type"] == "link_down"
    assert result.findings["location"] == {"device": "leaf2", "interface": "Ethernet0"}
    assert not harness["route_decision"]["fast_path"]
    assert harness["base_agent_reliability"]["status"] == "low_evidence"
    assert harness["cost"]["tool_calls"] == 1


def test_recursion_low_loss_temporal_evidence_closes_link_flapping(tmp_path):
    class Tools:
        def query_bgp_events(self, **_arguments):
            return ToolResult(
                success=True,
                data={
                    "events": [
                        {
                            "device": "leaf2",
                            "event_type": "session_flap",
                            "peer": "10.0.0.1",
                            "states_observed": ["ESTABLISHED", "ACTIVE"],
                        },
                        {
                            "device": "spine1",
                            "event_type": "session_flap",
                            "peer": "10.0.0.2",
                            "states_observed": ["ACTIVE", "ESTABLISHED"],
                        },
                    ]
                },
            )

        def get_device_logs(self, *, device, **_arguments):
            logs = (
                [{"message": "Port Ethernet0 oper error event: no_rx_reachability occurred"}]
                if device == "leaf2"
                else []
            )
            return ToolResult(success=True, data={"logs": logs})

    initial = diagnosis_result(
        {
            "verdict": "inconclusive",
            "fault_type": None,
            "device": None,
            "interface": None,
            "confidence": 0.0,
            "evidence": [],
            "reasoning": "Graph recursion limit reached without a stop condition.",
        }
    )
    context = DiagnosticContext(
        scenario_id="opaque-temporal-replay",
        topology={},
        symptoms={
            "observations": {
                "start_time": "2026-08-03T00:00:00Z",
                "end_time": "2026-08-03T00:00:30Z",
                "pingmesh_metrics": {
                    "anomalies": [
                        {
                            "type": "packet_loss",
                            "src_name": "client1",
                            "dst_name": "client2",
                            "src_leaf": "leaf1",
                            "dst_leaf": "leaf2",
                            "value": 6.0,
                            "sample_count": 30,
                            "persistence": "persistent",
                        }
                    ]
                },
            }
        },
        metadata={"worker_env": {"NETOPSBENCH_TOPOLOGY_DIR": str(_write_manifest(tmp_path).parent)}},
    )
    context.tools = Tools()

    result = asyncio.run(DiagnosticHarness(BaseAgent(initial)).diagnose(context))

    harness = result.metadata["diagnostic_harness"]
    assert result.verdict == "fault_detected"
    assert result.findings["fault_type"] == "link_flapping"
    assert result.findings["location"] == {"device": "leaf2", "interface": "Ethernet0"}
    assert harness["route_decision"]["family"] == "temporal_verification"
    # The first topology-bound log already corroborates the event, so the
    # closure must not spend a third call on an unrelated device.
    assert harness["cost"] == {"tool_calls": 2, "active_probes": 0, "probe_packets": 0}


def test_base_temporal_claim_without_live_transition_is_inconclusive(tmp_path):
    class Tools:
        def query_bgp_events(self, **_arguments):
            return ToolResult(success=True, data={"events": []})

        def get_device_logs(self, **_arguments):
            return ToolResult(success=True, data={"logs": []})

    initial = diagnosis_result(
        {
            "verdict": "network_healthy",
            "fault_type": "link_flap",
            "device": "leaf2",
            "interface": "Ethernet0",
            "confidence": 0.90,
            "evidence": [
                "BGP session flap ESTABLISHED→ACTIVE→ESTABLISHED was observed on leaf2.",
                "leaf2 device logs show Ethernet0 went down twice during the episode window.",
                "All interfaces and BGP sessions have since recovered.",
            ],
        }
    )
    context = DiagnosticContext(
        scenario_id="opaque-recovered-temporal-replay",
        topology={},
        symptoms={"observations": {}},
        metadata={"worker_env": {"NETOPSBENCH_TOPOLOGY_DIR": str(_write_manifest(tmp_path).parent)}},
    )
    context.tools = Tools()

    result = asyncio.run(DiagnosticHarness(BaseAgent(initial)).diagnose(context))

    assert result.verdict == "inconclusive"
    assert result.findings["fault_type"] is None
    harness = result.metadata["diagnostic_harness"]
    assert harness["route_decision"]["family"] == "temporal_verification"
    assert harness["hard_path_status"] == "inconclusive_by_evidence_contract"


def test_corruption_checksum_failure_closes_physical_link_loop(tmp_path):
    class Tools:
        def ping_link_test(self, **arguments):
            count = arguments["count"]
            return ToolResult(
                success=True,
                data={
                    "source": arguments["src"],
                    "destination": arguments["target_device"],
                    "count": count,
                    "output": f"{count} packets transmitted, {count // 2} received, 50% packet loss\n",
                    "return_code": 1,
                },
            )

        def payload_integrity_link_test(self, **arguments):
            failed = "leaf2" in {arguments["device_a"], arguments["device_b"]}
            count = arguments["count"]
            directions = [
                {
                    "source": "leaf2",
                    "source_interface": (
                        arguments["interface_a"] if arguments["device_a"] == "leaf2" else arguments["interface_b"]
                    ),
                    "packets_sent": count,
                    "packets_observed": count,
                    "missing_packets": 0,
                    "checksum_valid": not failed,
                    "checksum_failures": 2 if failed else 0,
                },
                {
                    "source": (arguments["device_b"] if arguments["device_a"] == "leaf2" else arguments["device_a"]),
                    "packets_sent": count,
                    "packets_observed": count,
                    "missing_packets": 0,
                    "checksum_valid": True,
                    "checksum_failures": 0,
                },
            ]
            return ToolResult(
                success=True,
                data={
                    "directions": directions,
                    "method": "bounded_bidirectional_physical_link_icmp_checksum_capture",
                },
            )

    initial = diagnosis_result(
        {
            "verdict": "inconclusive",
            "fault_type": None,
            "device": None,
            "interface": None,
            "confidence": 0.35,
            "evidence": ["Public observations show packet loss, but do not identify its cause."],
        }
    )
    context = DiagnosticContext(
        scenario_id="opaque-corruption-case",
        topology={},
        symptoms={
            "observations": {
                "pingmesh_metrics": {
                    "anomalies": [
                        {
                            "type": "packet_loss",
                            "src_name": "client2",
                            "dst_name": "client1",
                            "dst_ip": "192.0.2.1",
                            "src_leaf": "leaf2",
                            "dst_leaf": None,
                            "value": 20.0,
                            "sample_count": 30,
                            "persistence": "persistent",
                        }
                    ]
                }
            }
        },
        metadata={
            "runtime_id": "test-corruption-runtime",
            "worker_env": {"NETOPSBENCH_TOPOLOGY_DIR": str(_write_manifest(tmp_path).parent)},
        },
    )
    context.tools = Tools()
    config = HarnessConfig(
        impairment_probes=FeatureConfig(enabled=True),
        topology_ranker=FeatureConfig(enabled=True),
        diagnosability_gate=FeatureConfig(enabled=True),
        corruption_probe=CorruptionProbeConfig(samples_per_pair=5, samples_per_link_direction=5),
    )

    result = asyncio.run(DiagnosticHarness(BaseAgent(initial), config=config).diagnose(context))

    harness = result.metadata["diagnostic_harness"]
    assert harness["route_decision"]["family"] == "packet_corruption"
    assert "active_payload_integrity_failure" in harness["route_decision"]["reasons"]
    assert harness["integrity_link_collection"] == {
        "attempted": True,
        "candidate_count": 1,
        "trigger": "top_ranked_loss_link_discriminator",
    }
    assert result.verdict == "fault_detected"
    assert result.findings["fault_type"] == "packet_corruption"
    assert result.findings["location"] == {"device": "leaf2", "interface": "Ethernet0"}
    assert harness["diagnosability"]["confidence"] >= 0.75
    assert harness["diagnosability"]["margin"] >= 0.20
    assert harness["cost"] == {"tool_calls": 2, "active_probes": 3, "probe_packets": 30}
    direct = next(
        item
        for item in harness["evidence"]
        if item["source"] == "payload_integrity_link_test" and item["value"] is True
    )
    assert direct["covered_links"] == ("spine1:Ethernet4--leaf2:Ethernet0",)


@pytest.mark.parametrize(
    "initial_payload",
    [
        {
            "verdict": "inconclusive",
            "fault_type": None,
            "device": None,
            "interface": None,
            "confidence": 0.35,
            "evidence": ["Public observations show packet loss, but do not identify its cause."],
        },
        {
            "verdict": "fault_detected",
            "fault_type": "route_policy_misconfig",
            "device": "leaf1",
            "interface": None,
            "confidence": 0.85,
            "evidence": ["BGP is established, but traffic through leaf1 has persistent packet loss."],
        },
    ],
    ids=("inconclusive-base", "sanitized-full-medium-route-policy-conflict"),
)
def test_valid_integrity_preserves_packet_loss_link_isolation(tmp_path, initial_payload):
    class Tools:
        def payload_integrity_link_test(self, **arguments):
            count = arguments["count"]
            return ToolResult(
                success=True,
                data={
                    "directions": [
                        {
                            "source": arguments["device_a"],
                            "source_interface": arguments["interface_a"],
                            "packets_sent": count,
                            "packets_observed": count // 2,
                            "missing_packets": count - count // 2,
                            "checksum_valid": True,
                            "checksum_failures": 0,
                        },
                        {
                            "source": arguments["device_b"],
                            "source_interface": arguments["interface_b"],
                            "packets_sent": count,
                            "packets_observed": count,
                            "missing_packets": 0,
                            "checksum_valid": True,
                            "checksum_failures": 0,
                        },
                    ],
                    "method": "bounded_bidirectional_physical_link_icmp_checksum_capture",
                },
            )

        def ping_link_test(self, **arguments):
            count = arguments["count"]
            return ToolResult(
                success=True,
                data={
                    "source": arguments["src"],
                    "destination": arguments["target_device"],
                    "count": count,
                    "output": f"{count} packets transmitted, {count // 2} received, 50% packet loss\n",
                    "return_code": 1,
                },
            )

    initial = diagnosis_result(initial_payload)
    context = DiagnosticContext(
        scenario_id="opaque-loss-after-integrity-case",
        topology={},
        symptoms={
            "observations": {
                "pingmesh_metrics": {
                    "anomalies": [
                        {
                            "type": "packet_loss",
                            "src_name": "client1",
                            "dst_name": "client2",
                            "dst_ip": "192.0.2.2",
                            "src_leaf": "leaf1",
                            "dst_leaf": "leaf2",
                            "value": 20.0,
                            "sample_count": 30,
                            "persistence": "persistent",
                        }
                    ]
                }
            }
        },
        metadata={
            "runtime_id": "test-loss-after-integrity-runtime",
            "worker_env": {"NETOPSBENCH_TOPOLOGY_DIR": str(_write_manifest(tmp_path).parent)},
        },
    )
    context.tools = Tools()
    config = HarnessConfig(
        impairment_probes=FeatureConfig(enabled=True),
        topology_ranker=FeatureConfig(enabled=True),
        diagnosability_gate=FeatureConfig(enabled=True),
        corruption_probe=CorruptionProbeConfig(samples_per_pair=5, samples_per_link_direction=5),
        packet_loss_probe=PacketLossProbeConfig(matrix_packets_per_round=10, matrix_repeat_rounds=2),
    )

    result = asyncio.run(DiagnosticHarness(BaseAgent(initial), config=config).diagnose(context))

    harness = result.metadata["diagnostic_harness"]
    assert harness["route_decision"]["family"] == "packet_loss"
    assert "active_payload_integrity_failure" not in harness["route_decision"]["reasons"]
    assert [item["probe_id"] for item in harness["probe_history"]] == [
        "repeated-packet-loss",
        "payload-integrity-links",
    ]
    assert harness["probe_history"][0]["metadata"]["anomaly_pairs"] == 0
    assert harness["probe_history"][0]["metadata"]["link_isolation_pairs"] == 2
    assert result.verdict == "fault_detected"
    assert result.findings["fault_type"] == "packet_loss"
    assert result.findings["location"] == {"device": "leaf1", "interface": "Ethernet0"}
    assert harness["cost"] == {"tool_calls": 3, "active_probes": 4, "probe_packets": 50}
    if initial_payload["fault_type"] == "route_policy_misconfig":
        assert "semantic_label_conflicts_with_structured_impairment" in harness["route_decision"]["reasons"]


def test_base_recursion_falls_back_to_structured_packet_loss_with_shared_budget(tmp_path):
    class Tools:
        def __init__(self):
            self.semantic_devices = []

        def get_device_acl(self, **arguments):
            self.semantic_devices.append(("acl", arguments["device"]))
            return ToolResult(
                success=True,
                data={"sonic_acl_config": "", "iptables_forward_rules": ""},
            )

        def get_device_config(self, **arguments):
            self.semantic_devices.append(("config", arguments["device"]))
            return ToolResult(success=True, data={"config": "router bgp 65000\n"})

        def ping_link_test(self, **arguments):
            count = arguments["count"]
            return ToolResult(
                success=True,
                data={
                    "source": arguments["src"],
                    "destination": arguments["target_device"],
                    "count": count,
                    "output": f"{count} packets transmitted, {count // 2} received, 50% packet loss\n",
                    "return_code": 1,
                },
            )

        def payload_integrity_link_test(self, **arguments):
            count = arguments["count"]
            return ToolResult(
                success=True,
                data={
                    "directions": [
                        {
                            "source": arguments["device_a"],
                            "source_interface": arguments["interface_a"],
                            "packets_sent": count,
                            "packets_observed": count // 2,
                            "missing_packets": count - count // 2,
                            "checksum_valid": True,
                            "checksum_failures": 0,
                        },
                        {
                            "source": arguments["device_b"],
                            "source_interface": arguments["interface_b"],
                            "packets_sent": count,
                            "packets_observed": count,
                            "missing_packets": 0,
                            "checksum_valid": True,
                            "checksum_failures": 0,
                        },
                    ],
                    "method": "bounded_bidirectional_physical_link_icmp_checksum_capture",
                },
            )

    initial = diagnosis_result(
        {
            "verdict": "inconclusive",
            "fault_type": None,
            "device": None,
            "interface": None,
            "confidence": 0.0,
            "evidence": [],
            "reasoning": "Recursion limit reached without a stop condition.",
        }
    )
    context = DiagnosticContext(
        scenario_id="opaque-runtime-failure-loss-case",
        topology={},
        symptoms={
            "observations": {
                "pingmesh_metrics": {
                    "anomalies": [
                        {
                            "type": "packet_loss",
                            "src_name": "client1",
                            "dst_name": "client2",
                            "dst_ip": "192.0.2.2",
                            "src_leaf": "leaf1",
                            "dst_leaf": "leaf2",
                            "value": 20.0,
                            "sample_count": 30,
                            "persistence": "persistent",
                        }
                    ]
                }
            }
        },
        metadata={
            "runtime_id": "test-runtime-failure-loss-runtime",
            "worker_env": {"NETOPSBENCH_TOPOLOGY_DIR": str(_write_manifest(tmp_path).parent)},
        },
    )
    context.tools = Tools()
    config = HarnessConfig(
        impairment_probes=FeatureConfig(enabled=True),
        topology_ranker=FeatureConfig(enabled=True),
        diagnosability_gate=FeatureConfig(enabled=True),
        corruption_probe=CorruptionProbeConfig(samples_per_pair=5, samples_per_link_direction=5),
        packet_loss_probe=PacketLossProbeConfig(matrix_packets_per_round=10, matrix_repeat_rounds=2),
    )

    result = asyncio.run(DiagnosticHarness(BaseAgent(initial), config=config).diagnose(context))

    harness = result.metadata["diagnostic_harness"]
    assert result.verdict == "fault_detected"
    assert result.findings["fault_type"] == "packet_loss"
    assert result.findings["location"] == {"device": "leaf1", "interface": "Ethernet0"}
    assert harness["route_decision"]["family"] == "packet_loss"
    assert "base_status_recursion_failed" in harness["route_decision"]["reasons"]
    assert [item["probe_id"] for item in harness["probe_history"]] == [
        "repeated-packet-loss",
        "payload-integrity-links",
    ]
    assert context.tools.semantic_devices == []
    assert harness["cost"] == {"tool_calls": 3, "active_probes": 4, "probe_packets": 50}
    assert all(
        item["metadata"].get("runtime_error_used_as_fault_evidence") is not True for item in harness["probe_history"]
    )
