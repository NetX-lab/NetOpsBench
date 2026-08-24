import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime

from examples.agents.diagnostic_harness.config import BudgetConfig
from examples.agents.diagnostic_harness.models import Evidence, EvidenceOrigin, ProbePair
from examples.agents.diagnostic_harness.normalization.interface import LinkEndpoint, PhysicalLink, TopologyIndex
from examples.agents.diagnostic_harness.probes import ProbeBudget
from examples.agents.diagnostic_harness.verification import operational_closure as closure_module
from examples.agents.diagnostic_harness.verification.operational_closure import (
    HealthyVerificationStatus,
    OperationalClosure,
    _candidate_devices,
    _evidence_indicates_fault,
    _with_lookback,
    device_down_from_link_probes,
)
from netopsbench.agents.base import DiagnosticContext
from netopsbench.platform.toolkit._core.common import ToolResult

from .diagnostic_harness_helpers import diagnosis_result, sample_topology, write_two_leaf_manifest


def _base():
    return diagnosis_result(
        {
            "verdict": "inconclusive",
            "fault_type": None,
            "device": "leaf5",
            "interface": None,
            "confidence": 0.0,
            "evidence": [],
            "reasoning": "Graph recursion limit reached.",
        }
    )


def test_temporal_lookback_is_bounded_and_preserves_invalid_timestamps():
    assert _with_lookback("2026-08-14T10:00:00Z", 90) == "2026-08-14T09:58:30Z"
    assert _with_lookback("not-a-timestamp", 90) == "not-a-timestamp"
    assert _with_lookback("2026-08-14T10:00:00Z", 0) == "2026-08-14T10:00:00Z"


def _context(tools, *, anomalies=None):
    context = DiagnosticContext(
        scenario_id="opaque-verification",
        topology={},
        symptoms={
            "observations": {
                "data_source_status": "ok",
                "coverage_status": "complete",
                "anomalies_detected": bool(anomalies),
                "start_time": "2026-08-03T00:00:00Z",
                "end_time": "2026-08-03T00:00:30Z",
                "pingmesh_metrics": {"query_status": {"ok": True}, "anomalies": anomalies or []},
            }
        },
    )
    context.tools = tools
    return context


def _loss(value=0.06):
    return Evidence(
        evidence_id="E-loss",
        entity_type="path",
        entity_id="client1--client9",
        category="packet_loss_rate",
        value=value,
        source="episode",
        timestamp=datetime.now(UTC),
        metadata={"src_leaf": "leaf1", "dst_leaf": "leaf5"},
    )


def test_base_claim_cannot_block_healthy_or_create_device_scope_outage():
    claims = [
        Evidence(
            evidence_id=f"claim-{index}",
            entity_type="path",
            entity_id=f"client{index}--peer{index}",
            category="packet_loss_rate",
            value=1.0,
            source="base_agent",
            timestamp=datetime.now(UTC),
            origin=EvidenceOrigin.BASE_CLAIM,
            supports_submission=False,
            metadata={
                "src_leaf": "leaf5",
                "dst_leaf": f"leaf{index}",
                "src_name": f"client{index}",
            },
        )
        for index in range(1, 5)
    ]

    assert not any(_evidence_indicates_fault(item) for item in claims)
    assert OperationalClosure._device_scope_outage(claims, "leaf5") == ()


def test_repeated_integrity_rows_on_same_attachment_pair_are_not_diverse():
    rows = [
        Evidence(
            evidence_id=f"integrity-{index}",
            entity_type="path",
            entity_id="client1--client2",
            category="payload_integrity_failure",
            value=False,
            source="payload_integrity_test",
            timestamp=datetime.now(UTC),
            origin=EvidenceOrigin.ACTIVE_PROBE,
            metadata={"missing_packets": 0, "src_leaf": "leaf1", "dst_leaf": "leaf2"},
        )
        for index in range(3)
    ]

    selected = closure_module._diverse_clean_integrity_evidence(rows)

    assert [item.evidence_id for item in selected] == ["integrity-0"]


def test_complete_active_coverage_can_close_healthy_after_passive_symptom_is_reconciled():
    class Tools:
        def query_bgp_events(self, **_arguments):
            return ToolResult(success=True, data={"events": []})

        def get_bgp_neighbors(self, **_arguments):
            return ToolResult(success=True, data={"neighbors": [{"state": "ESTABLISHED"}]})

    now = datetime.now(UTC)
    evidence = [
        Evidence(
            evidence_id="active-path-coverage-certificate",
            entity_type="failure_domain",
            entity_id="candidate-paths",
            category="coverage_certificate",
            value={"candidate_links_covered": ["L1", "L2"]},
            source="evidence_reconciliation",
            timestamp=now,
            origin=EvidenceOrigin.ACTIVE_PROBE,
            supports_submission=False,
            metadata={"coverage_complete": True, "missing_data_is_healthy": False},
        ),
        Evidence(
            evidence_id="interface-up",
            entity_type="interface",
            entity_id="leaf1:Ethernet0",
            category="interface_oper_state",
            value="up",
            source="get_device_interfaces",
            timestamp=now,
            origin=EvidenceOrigin.LIVE_TELEMETRY,
        ),
        Evidence(
            evidence_id="route-present",
            entity_type="device",
            entity_id="leaf1",
            category="route_presence",
            value={"prefix": "192.0.2.0/30", "present": True, "is_discard": False},
            source="get_bgp_rib",
            timestamp=now,
            origin=EvidenceOrigin.LIVE_TELEMETRY,
        ),
        Evidence(
            evidence_id="integrity-clean",
            entity_type="path",
            entity_id="client1--client2",
            category="payload_integrity_failure",
            value=False,
            source="payload_integrity_test",
            timestamp=now,
            origin=EvidenceOrigin.ACTIVE_PROBE,
            metadata={"missing_packets": 0, "src_leaf": "leaf1", "dst_leaf": "leaf5"},
        ),
    ]
    budget = ProbeBudget(BudgetConfig(max_extra_tool_calls_per_hard_case=3))

    outcome = asyncio.run(
        OperationalClosure().verify_healthy(
            _context(Tools(), anomalies=[{"type": "packet_loss"}]),
            base_result=_base(),
            topology=sample_topology(),
            evidence=evidence,
            budget=budget,
        )
    )

    assert outcome.status == HealthyVerificationStatus.VERIFIED_HEALTHY.value
    assert outcome.result is not None
    assert outcome.result.verdict == "network_healthy"
    assert budget.tool_calls == 2


def test_healthy_reuse_rejects_planning_only_route_observation():
    route = Evidence(
        evidence_id="planning-route",
        entity_type="device",
        entity_id="leaf1",
        category="route_presence",
        value={"prefix": "192.0.2.0/30", "present": True, "is_discard": False},
        source="base_tool:get_route_table",
        timestamp=datetime.now(UTC),
        origin=EvidenceOrigin.LIVE_TELEMETRY,
        supports_submission=False,
    )

    assert OperationalClosure._existing_healthy_observation([route], "route_presence", device="leaf1") is None
    trusted = replace(route, supports_submission=True)
    assert OperationalClosure._existing_healthy_observation([trusted], "route_presence", device="leaf1") is trusted


def test_healthy_verification_budget_scales_with_required_coverage_not_inventory():
    topology = TopologyIndex(
        devices={
            **{f"leaf{index}": "leaf" for index in range(1, 65)},
            **{f"client{index}": "client" for index in range(1, 257)},
        },
        links=(),
        source="large-budget-test",
    )

    required = OperationalClosure().healthy_verification_budget(topology)

    # Ten-percent attachment coverage requires four diverse pairs for 64
    # attachment domains.  Adding 256 unrelated clients does not grow it.
    assert required == {"tool_calls": 8, "active_probes": 4, "probe_packets": 80}


def test_untrusted_coverage_claim_cannot_replace_episode_connectivity():
    class Tools:
        def query_bgp_events(self, **_arguments):
            raise AssertionError("verification should stop before live calls")

    evidence = [
        Evidence(
            evidence_id="untrusted-coverage-claim",
            entity_type="failure_domain",
            entity_id="candidate-paths",
            category="coverage_certificate",
            value={"candidate_links_covered": ["L1", "L2"]},
            source="base_agent",
            timestamp=datetime.now(UTC),
            origin=EvidenceOrigin.BASE_CLAIM,
            supports_submission=False,
            metadata={"coverage_complete": True, "missing_data_is_healthy": False},
        )
    ]
    budget = ProbeBudget(BudgetConfig(max_extra_tool_calls_per_hard_case=3))

    outcome = asyncio.run(
        OperationalClosure().verify_healthy(
            _context(Tools(), anomalies=[{"type": "packet_loss"}]),
            base_result=_base(),
            topology=sample_topology(),
            evidence=evidence,
            budget=budget,
        )
    )

    assert outcome.status == HealthyVerificationStatus.INSUFFICIENT_OBSERVATION.value
    assert outcome.result is None
    assert budget.tool_calls == 0


def test_concentrated_outage_device_outranks_wrong_base_device_for_query_order():
    evidence = [
        Evidence(
            evidence_id=f"E-outage-{index}",
            entity_type="path",
            entity_id=f"client9--peer{index}",
            category="packet_loss_rate",
            value=1.0,
            source="pingmesh_episode",
            timestamp=datetime.now(UTC),
            metadata={"src_leaf": "leaf5", "dst_leaf": peer},
        )
        for index, peer in enumerate(("leaf1", "spine1", "spine3"), start=1)
    ]

    assert _candidate_devices(sample_topology(), evidence, "leaf1", limit=2)[0] == "leaf5"


def test_live_down_interface_outranks_wrong_base_device_for_query_order():
    evidence = [
        Evidence(
            evidence_id="E-down",
            entity_type="interface",
            entity_id="leaf5:Ethernet4",
            category="interface_oper_state",
            value="down",
            source="base_tool:get_device_interfaces",
            timestamp=datetime.now(UTC),
            origin=EvidenceOrigin.LIVE_TELEMETRY,
        )
    ]

    assert _candidate_devices(sample_topology(), evidence, "leaf1", limit=2)[0] == "leaf5"


def test_link_state_verification_submits_canonical_down_interface():
    class Tools:
        def get_device_interfaces(self, *, device):
            assert device == "leaf5"
            return ToolResult(
                success=True,
                data={
                    "interfaces": [
                        {"name": "Ethernet4", "admin": "down", "oper": "down"},
                    ]
                },
            )

    outcome = asyncio.run(
        OperationalClosure().verify_link_state(
            _context(Tools()),
            base_result=_base(),
            topology=sample_topology(),
            evidence=[_loss(1.0)],
            budget=ProbeBudget(BudgetConfig()),
        )
    )

    assert outcome.result.verdict == "fault_detected"
    assert outcome.result.findings["fault_type"] == "link_down"
    assert outcome.result.findings["location"] == {"device": "leaf5", "interface": "Ethernet4"}
    assert {item.category for item in outcome.outcome.evidence} == {
        "interface_admin_state",
        "interface_oper_state",
    }


def test_bgp_verification_does_not_call_plain_idle_state_a_misconfiguration():
    class Tools:
        def get_bgp_neighbors(self, *, device):
            return ToolResult(success=True, data={"device": device, "neighbors": [{"state": "Idle"}]})

    outcome = asyncio.run(
        OperationalClosure().verify_bgp(
            _context(Tools()),
            base_result=_base(),
            topology=sample_topology(),
            evidence=[],
            budget=ProbeBudget(BudgetConfig()),
        )
    )

    assert outcome.result is None
    assert outcome.status == "bgp_state_without_configuration_attribution"
    state = [item for item in outcome.outcome.evidence if item.category == "bgp_neighbor_state"]
    assert state and all(not item.metadata["direct_bgp_configuration_evidence"] for item in state)


def test_bgp_verification_submits_only_with_explicit_configuration_failure_detail():
    class Tools:
        def get_bgp_neighbors(self, *, device):
            return ToolResult(
                success=True,
                data={
                    "device": device,
                    "neighbors": [
                        {"state": "Idle", "detail": {"last_reset": "Last reset due to Bad Peer AS"}}
                    ],
                },
            )

    outcome = asyncio.run(
        OperationalClosure().verify_bgp(
            _context(Tools()),
            base_result=_base(),
            topology=sample_topology(),
            evidence=[],
            budget=ProbeBudget(BudgetConfig()),
        )
    )

    assert outcome.result is not None
    assert outcome.result.findings["fault_type"] == "bgp_neighbor_misconfig"
    assert outcome.outcome.evidence[0].metadata["bgp_configuration_fault_reason"] == "peer_as_mismatch"


def test_link_state_verification_closes_multi_endpoint_device_outage_without_using_tool_error_as_evidence():
    class Tools:
        def get_device_interfaces(self, *, device):
            assert device == "leaf5"
            return ToolResult(success=False, error="container is not running")

        def ping_link_test(self, **arguments):
            assert arguments["target_device"] == "leaf5"
            count = arguments["count"]
            return ToolResult(
                success=True,
                data={
                    "count": count,
                    "output": f"{count} packets transmitted, 0 received, 100% packet loss\n",
                    "return_code": 1,
                },
            )

    evidence = []
    for index, (client, peer) in enumerate(
        (
            ("client9", "leaf1"),
            ("client9", "leaf2"),
            ("client10", "leaf1"),
            ("client10", "leaf2"),
        ),
        start=1,
    ):
        evidence.append(
            Evidence(
                evidence_id=f"E-device-{index}",
                entity_type="path",
                entity_id=f"{client}--peer-{index}",
                category="packet_loss_rate",
                value=1.0,
                source="episode",
                timestamp=datetime.now(UTC),
                metadata={
                    "src_leaf": "leaf5",
                    "dst_leaf": peer,
                    "src_name": client,
                    "dst_name": f"peer-{index}",
                },
            )
        )

    base_topology = sample_topology()
    leaf5 = LinkEndpoint("leaf5", "Ethernet8", ("eth3", "Ethernet8"))
    spine4 = LinkEndpoint("spine4", "Ethernet4", ("eth2", "Ethernet4"))
    topology = TopologyIndex(
        devices={**base_topology.devices, "spine4": "spine"},
        links=(*base_topology.links, PhysicalLink("leaf5:Ethernet8--spine4:Ethernet4", leaf5, spine4)),
        source="test-device-liveness",
    )
    outcome = asyncio.run(
        OperationalClosure().verify_link_state(
            _context(Tools()),
            base_result=_base(),
            topology=topology,
            evidence=evidence,
            budget=ProbeBudget(BudgetConfig()),
        )
    )

    assert outcome.result.verdict == "fault_detected"
    assert outcome.result.findings["fault_type"] == "device_down"
    assert outcome.result.findings["location"] == {"device": "leaf5", "interface": None}
    assert all("tool_error" not in item for item in outcome.result.findings["evidence"])
    errors = [item for item in outcome.outcome.evidence if item.category == "tool_error"]
    assert errors and all(item.reliability == 0 for item in errors)
    liveness = [item for item in outcome.outcome.evidence if item.category == "device_liveness"]
    assert len(liveness) == 2
    assert len({item.independence_key for item in liveness}) == 2


def test_link_state_reuses_failed_action_signature_only_to_plan_peer_liveness():
    class Tools:
        interface_calls = 0

        def get_device_interfaces(self, **_arguments):
            self.interface_calls += 1
            raise AssertionError("a known failed action signature must not be repeated")

        def ping_link_test(self, **arguments):
            count = arguments["count"]
            return ToolResult(
                success=True,
                data={
                    "count": count,
                    "output": f"{count} packets transmitted, 0 received, 100% packet loss\n",
                    "return_code": 1,
                },
            )

    leaf = [LinkEndpoint("leaf5", f"Ethernet{index}", (f"eth{index}",)) for index in (0, 4, 8)]
    peers = [LinkEndpoint(f"spine{index}", "Ethernet0", ("eth1",)) for index in (1, 2, 3)]
    links = tuple(
        PhysicalLink(
            f"leaf5:{leaf_endpoint.canonical_interface}--{peer.device}:Ethernet0",
            leaf_endpoint,
            peer,
        )
        for leaf_endpoint, peer in zip(leaf, peers, strict=True)
    )
    topology = TopologyIndex(
        devices={"leaf5": "leaf", "spine1": "spine", "spine2": "spine", "spine3": "spine"},
        links=links,
        source="test-failed-signature",
    )
    failed_action = Evidence(
        "base-interface-error",
        "device",
        "leaf5",
        "tool_error",
        {"error": "container unavailable"},
        "base_tool:get_device_interfaces",
        datetime.now(UTC),
        reliability=0.0,
        origin=EvidenceOrigin.UNKNOWN,
        supports_submission=False,
    )
    tools = Tools()

    outcome = asyncio.run(
        OperationalClosure().verify_link_state(
            _context(tools),
            base_result=_base(),
            topology=topology,
            evidence=[failed_action],
            budget=ProbeBudget(BudgetConfig()),
        )
    )

    assert tools.interface_calls == 0
    assert outcome.result is not None
    assert outcome.result.findings["fault_type"] == "device_down"
    liveness = [item for item in outcome.outcome.evidence if item.category == "device_liveness"]
    assert len(liveness) == 3
    assert len({item.independence_key for item in liveness}) == 3


def test_existing_link_probes_close_device_scope_only_with_peer_controls():
    candidate_links = (
        PhysicalLink(
            "spine2:Ethernet0--leaf1:Ethernet0",
            LinkEndpoint("spine2", "Ethernet0", ("eth1",)),
            LinkEndpoint("leaf1", "Ethernet0", ("eth1",)),
        ),
        PhysicalLink(
            "spine2:Ethernet4--leaf2:Ethernet0",
            LinkEndpoint("spine2", "Ethernet4", ("eth2",)),
            LinkEndpoint("leaf2", "Ethernet0", ("eth1",)),
        ),
        PhysicalLink(
            "spine1:Ethernet0--leaf1:Ethernet4",
            LinkEndpoint("spine1", "Ethernet0", ("eth1",)),
            LinkEndpoint("leaf1", "Ethernet4", ("eth2",)),
        ),
        PhysicalLink(
            "spine1:Ethernet4--leaf2:Ethernet4",
            LinkEndpoint("spine1", "Ethernet4", ("eth2",)),
            LinkEndpoint("leaf2", "Ethernet4", ("eth2",)),
        ),
    )
    topology = TopologyIndex(
        devices={"spine1": "spine", "spine2": "spine", "leaf1": "leaf", "leaf2": "leaf"},
        links=candidate_links,
        source="device-scope-test",
    )

    def link_evidence(index: int, link_id: str, loss: float) -> Evidence:
        return Evidence(
            evidence_id=f"link-{index}",
            entity_type="path",
            entity_id=link_id,
            category="packet_loss_rate",
            value=loss,
            source="ping_link_test",
            timestamp=datetime.now(UTC),
            probe_id="link-isolation",
            observed_path=(link_id,),
            possible_paths=((link_id,),),
            covered_links=(link_id,),
            path_observation_confidence=1.0,
            origin=EvidenceOrigin.ACTIVE_PROBE,
        )

    closure = device_down_from_link_probes(
        _base(),
        topology=topology,
        evidence=[
            link_evidence(1, candidate_links[0].link_id, 1.0),
            link_evidence(2, candidate_links[1].link_id, 1.0),
            link_evidence(3, candidate_links[2].link_id, 0.0),
            link_evidence(4, candidate_links[3].link_id, 0.0),
        ],
    )

    assert closure is not None and closure.result is not None
    assert closure.result.findings["fault_type"] == "device_down"
    assert closure.result.findings["location"] == {"device": "spine2", "interface": None}
    assert len(closure.outcome.evidence) == 2


def test_one_failed_link_cannot_be_promoted_to_device_down():
    topology = sample_topology()
    link = topology.links[0]
    evidence = Evidence(
        evidence_id="one-link",
        entity_type="path",
        entity_id=link.link_id,
        category="packet_loss_rate",
        value=1.0,
        source="ping_link_test",
        timestamp=datetime.now(UTC),
        probe_id="link-isolation",
        observed_path=(link.link_id,),
        possible_paths=((link.link_id,),),
        covered_links=(link.link_id,),
        path_observation_confidence=1.0,
        origin=EvidenceOrigin.ACTIVE_PROBE,
    )

    assert device_down_from_link_probes(_base(), topology=topology, evidence=[evidence]) is None


def test_temporal_verification_requires_bgp_transition_and_interface_log():
    class Tools:
        def query_bgp_events(self, **_arguments):
            return ToolResult(
                success=True,
                data={
                    "events": [
                        {
                            "device": "leaf5",
                            "event_type": "session_flap",
                            "peer": "10.0.0.1",
                            "states_observed": ["ESTABLISHED", "ACTIVE"],
                        },
                        {
                            "device": "spine3",
                            "event_type": "session_flap",
                            "peer": "10.0.0.2",
                            "states_observed": ["ACTIVE", "ESTABLISHED"],
                        },
                    ]
                },
            )

        def get_device_logs(self, *, device, **_arguments):
            logs = (
                [{"message": "Port Ethernet4 oper error event: no_rx_reachability occurred"}]
                if device == "leaf5"
                else []
            )
            return ToolResult(success=True, data={"logs": logs})

    outcome = asyncio.run(
        OperationalClosure().verify_temporal(
            _context(Tools(), anomalies=[{"type": "packet_loss"}]),
            base_result=_base(),
            topology=sample_topology(),
            evidence=[_loss()],
            budget=ProbeBudget(BudgetConfig()),
        )
    )

    assert outcome.result.verdict == "fault_detected"
    assert outcome.result.findings["fault_type"] == "link_flapping"
    assert outcome.result.findings["location"] == {"device": "leaf5", "interface": "Ethernet4"}
    assert {item.source for item in outcome.outcome.evidence} >= {"query_bgp_events", "get_device_logs"}


def test_temporal_verification_uses_recent_neighbor_reset_when_event_index_is_empty():
    class Tools:
        def query_bgp_events(self, **_arguments):
            return ToolResult(success=True, data={"events": []})

        def get_device_logs(self, *, device, **_arguments):
            logs = [{"message": "Port Ethernet4 link down then link up"}] if device == "leaf5" else []
            return ToolResult(success=True, data={"logs": logs})

        def get_bgp_neighbors(self, *, device):
            assert device == "leaf5"
            return ToolResult(
                success=True,
                data={"neighbors": [{"state": "ESTABLISHED", "uptime_seconds": 5}]},
            )

    outcome = asyncio.run(
        OperationalClosure().verify_temporal(
            _context(Tools(), anomalies=[{"type": "packet_loss"}]),
            base_result=_base(),
            topology=sample_topology(),
            evidence=[_loss()],
            budget=ProbeBudget(BudgetConfig()),
        )
    )

    assert outcome.result is not None
    assert outcome.result.findings["fault_type"] == "link_flapping"
    assert outcome.result.findings["location"] == {"device": "leaf5", "interface": "Ethernet4"}
    assert {item.source for item in outcome.outcome.evidence} >= {"get_device_logs", "get_bgp_neighbors"}


def test_temporal_triage_preserves_third_call_for_neighbor_corroboration():
    class Tools:
        def __init__(self):
            self.calls = []

        def query_bgp_events(self, **_arguments):
            self.calls.append("events")
            return ToolResult(success=True, data={"events": []})

        def get_device_logs(self, *, device, **_arguments):
            self.calls.append(f"logs:{device}")
            logs = [{"message": "Port Ethernet4 link down then link up"}] if device == "leaf5" else []
            return ToolResult(success=True, data={"logs": logs})

        def get_bgp_neighbors(self, *, device):
            self.calls.append(f"neighbors:{device}")
            return ToolResult(success=True, data={"neighbors": [{"state": "ESTABLISHED", "uptime_seconds": 5}]})

    tools = Tools()
    budget = ProbeBudget(BudgetConfig())
    budget.configure_stages(family="temporal_verification", triage_cap=3)
    with budget.use_stage("triage"):
        outcome = asyncio.run(
            OperationalClosure().verify_temporal(
                _context(tools, anomalies=[{"type": "packet_loss"}]),
                base_result=_base(),
                topology=sample_topology(),
                evidence=[_loss()],
                budget=budget,
            )
        )

    assert outcome.result is not None
    assert outcome.result.findings["fault_type"] == "link_flapping"
    assert tools.calls == ["events", "logs:leaf5", "neighbors:leaf5"]


def test_healthy_verifier_with_no_safe_integrity_pair_is_insufficient():
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

    budget = ProbeBudget(BudgetConfig())
    outcome = asyncio.run(
        OperationalClosure().verify_healthy(
            _context(Tools()),
            base_result=_base(),
            topology=sample_topology(),
            evidence=[],
            budget=budget,
        )
    )

    assert outcome.status == HealthyVerificationStatus.INSUFFICIENT_OBSERVATION.value
    assert outcome.result is None
    assert budget.tool_calls == 4
    assert any(item.evidence_id == "healthy-integrity-pair-missing" for item in outcome.outcome.evidence)


def test_missing_healthy_observation_is_not_treated_as_normal():
    class Tools:
        def query_bgp_events(self, **_arguments):
            return ToolResult(success=True, data={"events": []})

        def get_device_interfaces(self, *, device):
            return ToolResult(success=False, error="timeout")

        def get_bgp_neighbors(self, *, device):
            return ToolResult(success=True, data={"neighbors": [{"state": "ESTABLISHED"}]})

        def get_route_table(self, *, device, **_arguments):
            return ToolResult(success=True, data={"routes": [{"prefix": "0.0.0.0/0"}]})

    outcome = asyncio.run(
        OperationalClosure().verify_healthy(
            _context(Tools()),
            base_result=_base(),
            topology=sample_topology(),
            evidence=[],
            budget=ProbeBudget(BudgetConfig()),
        )
    )

    assert outcome.status == HealthyVerificationStatus.INSUFFICIENT_OBSERVATION.value
    assert outcome.result is None
    assert any(item.category == "tool_error" and item.reliability == 0 for item in outcome.outcome.evidence)


def test_healthy_verifier_accepts_three_independent_integrity_controls_when_extra_breadth_times_out(
    monkeypatch,
):
    class Tools:
        def __init__(self):
            self.integrity_calls = 0

        def query_bgp_events(self, **_arguments):
            return ToolResult(success=True, data={"events": []})

        def get_device_interfaces(self, *, device):
            return ToolResult(success=True, data={"interfaces": [{"name": "Ethernet0", "admin": "up", "oper": "up"}]})

        def get_bgp_neighbors(self, *, device):
            return ToolResult(success=True, data={"neighbors": [{"state": "ESTABLISHED"}]})

        def get_route_table(self, *, device, **_arguments):
            return ToolResult(success=True, data={"routes": [{"prefix": "0.0.0.0/0"}]})

        def payload_integrity_test(self, **arguments):
            self.integrity_calls += 1
            if self.integrity_calls == 4:
                return ToolResult(success=False, error="timeout")
            count = arguments["count"]
            return ToolResult(
                success=True,
                data={
                    "received": True,
                    "checksum_valid": True,
                    "packets_sent": count,
                    "packets_observed": count,
                    "missing_packets": 0,
                    "integrity_complete": True,
                },
            )

    topology = TopologyIndex(
        devices={
            **{f"leaf{index}": "leaf" for index in range(1, 9)},
            **{f"client{index}": "client" for index in range(1, 9)},
        },
        links=(),
        source="large-healthy-test",
    )
    pairs = [
        ProbePair(
            f"client{2 * index - 1}",
            f"192.0.2.{2 * index}",
            source_leaf=f"leaf{2 * index - 1}",
            destination_leaf=f"leaf{2 * index}",
        )
        for index in range(1, 5)
    ]
    monkeypatch.setattr(closure_module, "select_probe_pairs", lambda *_args, **_kwargs: pairs)

    outcome = asyncio.run(
        OperationalClosure().verify_healthy(
            _context(Tools()),
            base_result=_base(),
            topology=topology,
            evidence=[],
            budget=ProbeBudget(BudgetConfig()),
        )
    )

    assert outcome.status == HealthyVerificationStatus.VERIFIED_HEALTHY.value
    assert outcome.result is not None
    assert any(item.category == "tool_error" for item in outcome.outcome.evidence)
    certificate = next(item for item in outcome.outcome.evidence if item.category == "coverage_certificate")
    assert certificate.value["global_reachability_complete"] is True
    assert certificate.metadata["missing_data_is_healthy"] is False
    assert certificate.supports_submission is False


def test_link_state_verification_checks_explicit_peer_first_and_closes_spine_outage():
    class Tools:
        def __init__(self):
            self.interface_calls = []

        def get_device_interfaces(self, *, device):
            self.interface_calls.append(device)
            return ToolResult(success=False, error="container is not running")

        def ping_link_test(self, **arguments):
            assert arguments["target_device"] == "spine3"
            count = arguments["count"]
            return ToolResult(
                success=True,
                data={"count": count, "output": f"{count} packets transmitted, 0 received, 100% packet loss\n"},
            )

    base = sample_topology()
    leaf1 = LinkEndpoint("leaf1", "Ethernet8", ("eth3", "Ethernet8"))
    spine3 = LinkEndpoint("spine3", "Ethernet8", ("eth3", "Ethernet8"))
    topology = TopologyIndex(
        devices=base.devices,
        links=(*base.links, PhysicalLink("leaf1:Ethernet8--spine3:Ethernet8", leaf1, spine3)),
        source="test-spine-outage",
    )
    initial = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "link_down",
            "device": "leaf5",
            "interface": "Ethernet4",
            "confidence": 0.95,
            "evidence": ["spine3 container is not running"],
        }
    )
    tools = Tools()

    outcome = asyncio.run(
        OperationalClosure().verify_link_state(
            _context(tools),
            base_result=initial,
            topology=topology,
            evidence=[],
            budget=ProbeBudget(BudgetConfig()),
        )
    )

    assert tools.interface_calls == ["spine3"]
    assert outcome.result is not None
    assert outcome.result.findings["fault_type"] == "device_down"
    assert outcome.result.findings["location"] == {"device": "spine3", "interface": None}


def test_two_timestamped_interface_events_close_flap_without_bgp():
    class Tools:
        def __init__(self):
            self.calls = []

        def query_bgp_events(self, **_arguments):
            self.calls.append("events")
            return ToolResult(success=True, data={"events": []})

        def get_device_logs(self, *, device, **_arguments):
            self.calls.append(f"logs:{device}")
            if device != "leaf5":
                return ToolResult(success=True, data={"logs": []})
            return ToolResult(
                success=True,
                data={
                    "logs": [
                        {"timestamp": "2026-08-03T00:00:10Z", "message": "Port Ethernet4 link down"},
                        {"timestamp": "2026-08-03T00:00:12Z", "message": "Port Ethernet4 link up"},
                    ]
                },
            )

    tools = Tools()
    outcome = asyncio.run(
        OperationalClosure().verify_temporal(
            _context(tools, anomalies=[{"type": "packet_loss"}]),
            base_result=_base(),
            topology=sample_topology(),
            evidence=[_loss()],
            budget=ProbeBudget(BudgetConfig()),
        )
    )

    assert outcome.result is not None
    assert outcome.result.findings["fault_type"] == "link_flapping"
    assert tools.calls == ["events", "logs:leaf5"]


def test_error_only_logs_need_independent_bgp_temporal_evidence():
    class Tools:
        def query_bgp_events(self, **_arguments):
            return ToolResult(success=True, data={"events": []})

        def get_device_logs(self, *, device, **_arguments):
            return ToolResult(
                success=True,
                data={"logs": [{"message": "Port Ethernet4 oper error event: no_rx_reachability occurred"}]},
            )

        def get_bgp_neighbors(self, *, device):
            return ToolResult(success=True, data={"neighbors": [{"state": "ESTABLISHED", "uptime_seconds": 9999}]})

    outcome = asyncio.run(
        OperationalClosure().verify_temporal(
            _context(Tools(), anomalies=[{"type": "packet_loss"}]),
            base_result=_base(),
            topology=sample_topology(),
            evidence=[_loss()],
            budget=ProbeBudget(BudgetConfig()),
        )
    )

    assert outcome.result is None
    assert outcome.next_family == "packet_loss"


def test_conflicting_healthy_claim_requires_path_scoped_integrity(monkeypatch):
    class Tools:
        def query_bgp_events(self, **_arguments):
            return ToolResult(success=True, data={"events": []})

        def get_device_interfaces(self, *, device):
            return ToolResult(success=True, data={"interfaces": [{"name": "Ethernet0", "admin": "up", "oper": "up"}]})

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
                    "packets_sent": count,
                    "packets_observed": count,
                    "missing_packets": 0,
                    "integrity_complete": True,
                },
            )

    monkeypatch.setattr(
        closure_module,
        "select_probe_pairs",
        lambda *_args, **_kwargs: [ProbePair("client1", "192.0.2.2")],
    )
    base = replace(
        _base(),
        metadata={"diagnostic_harness": {"base_agent_reliability": {"semantic_conflict": True}}},
    )

    outcome = asyncio.run(
        OperationalClosure().verify_healthy(
            _context(Tools()),
            base_result=base,
            topology=sample_topology(),
            evidence=[],
            budget=ProbeBudget(BudgetConfig()),
        )
    )

    assert outcome.result is None
    assert outcome.status == HealthyVerificationStatus.INSUFFICIENT_OBSERVATION.value
    assert any(item.evidence_id == "healthy-integrity-path-scope-missing" for item in outcome.outcome.evidence)


def test_conflicting_healthy_claim_scopes_integrity_from_public_topology(monkeypatch, tmp_path):
    class Tools:
        def query_bgp_events(self, **_arguments):
            return ToolResult(success=True, data={"events": []})

        def get_device_interfaces(self, *, device):
            return ToolResult(success=True, data={"interfaces": [{"name": "Ethernet0", "admin": "up", "oper": "up"}]})

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
                    "packets_sent": count,
                    "packets_observed": count,
                    "missing_packets": 0,
                    "integrity_complete": True,
                },
            )

    monkeypatch.setattr(
        closure_module,
        "select_probe_pairs",
        lambda *_args, **_kwargs: [
            ProbePair("client1", "192.0.2.2", source_leaf="leaf1", destination_leaf="leaf2")
        ],
    )
    context = _context(Tools())
    manifest = write_two_leaf_manifest(tmp_path)
    context.topology = json.loads(manifest.read_text(encoding="utf-8"))
    base = replace(
        _base(),
        metadata={"diagnostic_harness": {"base_agent_reliability": {"semantic_conflict": True}}},
    )

    outcome = asyncio.run(
        OperationalClosure().verify_healthy(
            context,
            base_result=base,
            topology=TopologyIndex.from_context(context),
            evidence=[],
            budget=ProbeBudget(BudgetConfig()),
        )
    )

    assert outcome.status == HealthyVerificationStatus.VERIFIED_HEALTHY.value
    assert outcome.result is not None
    integrity = [item for item in outcome.outcome.evidence if item.category == "payload_integrity_failure"]
    assert integrity and all(item.possible_paths for item in integrity)
