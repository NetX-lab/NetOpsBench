import asyncio
from types import SimpleNamespace

from examples.agents.diagnostic_harness.config import BudgetConfig, CorruptionProbeConfig
from examples.agents.diagnostic_harness.models import ProbePair, RankedInterfaceCandidate
from examples.agents.diagnostic_harness.probes import LinkPayloadIntegrityProbe, PayloadIntegrityProbe, ProbeBudget
from examples.agents.diagnostic_harness.probes import base as probe_base_module
from examples.agents.diagnostic_harness.probes import corruption as corruption_module
from examples.agents.diagnostic_harness.probes.corruption import (
    LINK_PAYLOAD_INTEGRITY_TOOL,
    PAYLOAD_INTEGRITY_TOOL,
)
from netopsbench.platform.toolkit._core.common import ToolResult
from netopsbench.platform.toolkit.mcp.registry import load_tool_specs


def test_real_tool_registry_contains_bounded_payload_integrity_capture():
    available = {spec.name for spec in load_tool_specs()}
    assert PAYLOAD_INTEGRITY_TOOL in available
    assert LINK_PAYLOAD_INTEGRITY_TOOL in available


def test_unsupported_corruption_probe_never_fabricates_integrity_result_or_tool_call(monkeypatch):
    monkeypatch.setattr(corruption_module, "load_tool_specs", lambda: [])
    outcome = asyncio.run(
        PayloadIntegrityProbe(CorruptionProbeConfig(allow_interface_counter_fallback=False)).run(
            SimpleNamespace(tools=object()),
            pair=ProbePair("client1", "192.0.2.2"),
            budget=ProbeBudget(BudgetConfig()),
        )
    )

    assert outcome.status == "unsupported"
    assert outcome.tool_calls == 0
    assert outcome.probe_packets == 0
    assert outcome.observations[0].supported is False
    assert outcome.evidence[0].category == "missing_observation"
    assert outcome.evidence[0].reliability == 0
    assert all(item.category != "payload_integrity_failure" for item in outcome.evidence)


def test_unsupported_probe_reuses_real_interface_error_metric_contract_as_fallback(monkeypatch):
    monkeypatch.setattr(corruption_module, "load_tool_specs", lambda: [])

    class CounterTools:
        def __init__(self):
            self.calls = []

        def get_interface_metrics(self, **arguments):
            self.calls.append(arguments)
            return ToolResult(
                success=True,
                data={
                    "summary": {
                        "in_errors": {"window_delta": 4.0},
                        "out_errors": {"window_delta": 2.0},
                    }
                },
            )

    tools = CounterTools()
    outcome = asyncio.run(
        PayloadIntegrityProbe().run(
            SimpleNamespace(tools=tools),
            pair=ProbePair("client1", "192.0.2.2"),
            budget=ProbeBudget(BudgetConfig()),
            target_device="leaf5",
            target_interface="Ethernet4",
        )
    )

    assert outcome.status == "unsupported"
    assert outcome.tool_calls == 1
    assert tools.calls == [
        {
            "device": "leaf5",
            "interface": "Ethernet4",
            "time_range_minutes": 5,
            "metric_type": "errors",
            "view": "summary",
            "max_points": 120,
        }
    ]
    counters = next(item for item in outcome.evidence if item.category == "interface_counter_delta")
    assert counters.value == {"in_errors": 4.0, "out_errors": 2.0}
    assert "cannot uniquely distinguish" in counters.metadata["diagnostic_limit"]
    assert all(item.category != "payload_integrity_failure" for item in outcome.evidence)


def test_counter_fallback_tool_failure_remains_non_supporting_tool_error(monkeypatch):
    monkeypatch.setattr(corruption_module, "load_tool_specs", lambda: [])

    class FailingTools:
        def get_interface_metrics(self, **arguments):
            return ToolResult(success=False, data=None, error="InfluxDB query timed out")

    outcome = asyncio.run(
        PayloadIntegrityProbe().run(
            SimpleNamespace(tools=FailingTools()),
            pair=ProbePair("client1", "192.0.2.2"),
            budget=ProbeBudget(BudgetConfig()),
            target_device="leaf5",
            target_interface="Ethernet4",
        )
    )

    error = next(item for item in outcome.evidence if item.category == "tool_error")
    assert error.reliability == 0
    assert "timed out" in error.value["error"]


def test_real_checksum_capture_becomes_corruption_evidence_with_bounded_packets():
    class IntegrityTools:
        def payload_integrity_test(self, **arguments):
            assert arguments == {"src": "client1", "dst_ip": "192.0.2.2", "count": 5}
            return ToolResult(
                success=True,
                data={
                    "observation_complete": True,
                    "received": True,
                    "checksum_valid": False,
                    "receive_timestamp": "2026-08-01 12:00:00.000000",
                    "packets_sent": 5,
                    "packets_observed": 5,
                    "missing_packets": 0,
                    "checksum_failures": 2,
                    "method": "bounded_active_destination_ingress_icmp_checksum_capture",
                },
            )

    outcome = asyncio.run(
        PayloadIntegrityProbe(CorruptionProbeConfig(samples_per_pair=5)).run(
            SimpleNamespace(tools=IntegrityTools()),
            pair=ProbePair("client1", "192.0.2.2"),
            budget=ProbeBudget(BudgetConfig()),
        )
    )

    assert outcome.status == "completed"
    assert outcome.tool_calls == 1
    assert outcome.probe_packets == 5
    evidence = next(item for item in outcome.evidence if item.category == "payload_integrity_failure")
    assert evidence.value is True
    assert evidence.metadata["checksum_failures"] == 2


def test_integrity_probe_samples_multiple_disjoint_pairs_with_one_shared_budget():
    class IntegrityTools:
        def __init__(self):
            self.sources = []

        def payload_integrity_test(self, **arguments):
            self.sources.append(arguments["src"])
            corrupt = arguments["src"] == "client3"
            return ToolResult(
                success=True,
                data={
                    "observation_complete": True,
                    "received": True,
                    "checksum_valid": not corrupt,
                    "packets_sent": 20,
                    "packets_observed": 20,
                    "missing_packets": 0,
                    "checksum_failures": int(corrupt),
                },
            )

    tools = IntegrityTools()
    outcome = asyncio.run(
        PayloadIntegrityProbe().run(
            SimpleNamespace(tools=tools),
            pairs=[
                ProbePair("client1", "192.0.2.2"),
                ProbePair("client3", "192.0.2.4"),
            ],
            budget=ProbeBudget(BudgetConfig()),
        )
    )

    assert outcome.status == "completed"
    assert tools.sources == ["client1", "client3"]
    assert outcome.tool_calls == 2
    assert outcome.probe_packets == 40
    assert any(item.category == "payload_integrity_failure" and item.value for item in outcome.evidence)


def test_inventory_integrity_pairs_cover_access_edges_on_investigated_leaf(monkeypatch):
    clients = [
        SimpleNamespace(name=f"client{index}", data_ip=f"192.0.2.{index}", attached_switch="leaf1")
        for index in range(1, 5)
    ] + [
        SimpleNamespace(name=f"client{index}", data_ip=f"192.0.2.{index}", attached_switch="leaf2")
        for index in range(5, 9)
    ]
    monkeypatch.setattr(
        probe_base_module,
        "_load_context_manifest",
        lambda _context: SimpleNamespace(clients=lambda: clients),
    )

    pairs = probe_base_module.select_probe_pairs(
        SimpleNamespace(symptoms={}),
        family="packet_corruption",
        max_pairs=4,
        preferred_leaves=("leaf1",),
    )

    assert [pair.source for pair in pairs] == ["client1", "client2", "client3", "client4"]
    assert {pair.destination_leaf for pair in pairs} == {"leaf2"}
    assert all(pair.metadata["coverage"] == "focused_access_edge" for pair in pairs)


def test_stratified_integrity_pairs_cover_distinct_attachment_domains(monkeypatch):
    clients = [
        SimpleNamespace(
            name=f"client{index}",
            data_ip=f"192.0.2.{index}",
            attached_switch=f"leaf{index}",
        )
        for index in range(1, 6)
    ]
    monkeypatch.setattr(
        probe_base_module,
        "_load_context_manifest",
        lambda _context: SimpleNamespace(clients=lambda: clients),
    )

    pairs = probe_base_module.select_probe_pairs(
        SimpleNamespace(symptoms={}),
        family="packet_corruption",
        max_pairs=3,
        stratified_attachment_coverage=True,
        preferred_leaves=("leaf3",),
    )

    assert len(pairs) == 3
    assert {
        attachment
        for pair in pairs
        for attachment in (pair.source_attachment, pair.destination_attachment)
    } == {f"leaf{index}" for index in range(1, 6)}
    assert [pair.metadata["coverage"] for pair in pairs] == [
        "disjoint_attachment_pair",
        "disjoint_attachment_pair",
        "odd_attachment_remainder",
    ]


def test_stratified_integrity_pairs_prioritize_uncovered_attachment_domains(monkeypatch):
    clients = [
        SimpleNamespace(
            name=f"client{index}",
            data_ip=f"192.0.2.{index}",
            attached_switch=f"leaf{index}",
        )
        for index in range(1, 5)
    ]
    monkeypatch.setattr(
        probe_base_module,
        "_load_context_manifest",
        lambda _context: SimpleNamespace(clients=lambda: clients),
    )

    pairs = probe_base_module.select_probe_pairs(
        SimpleNamespace(symptoms={}),
        family="packet_corruption",
        max_pairs=1,
        stratified_attachment_coverage=True,
        covered_attachment_domains=("leaf1", "leaf2"),
    )

    assert len(pairs) == 1
    assert {pairs[0].source_attachment, pairs[0].destination_attachment} == {"leaf3", "leaf4"}


def test_active_missing_sequences_are_loss_not_corruption():
    class MissingTools:
        def payload_integrity_test(self, **arguments):
            return ToolResult(
                success=True,
                data={
                    "observation_complete": True,
                    "received": True,
                    "checksum_valid": True,
                    "packets_sent": 20,
                    "packets_observed": 15,
                    "missing_packets": 5,
                    "checksum_failures": 0,
                },
            )

    outcome = asyncio.run(
        PayloadIntegrityProbe().run(
            SimpleNamespace(tools=MissingTools()),
            pair=ProbePair("client1", "192.0.2.2"),
            budget=ProbeBudget(BudgetConfig()),
        )
    )

    loss = next(item for item in outcome.evidence if item.category == "packet_loss_rate")
    assert all(item.category != "payload_integrity_failure" for item in outcome.evidence)
    assert loss.value == 0.25


def _candidate(index: int) -> RankedInterfaceCandidate:
    return RankedInterfaceCandidate(
        link_id=f"spine{index}:Ethernet0--leaf1:Ethernet{(index - 1) * 4}",
        primary_device="leaf1",
        primary_interface=f"Ethernet{(index - 1) * 4}",
        peer_device=f"spine{index}",
        peer_interface="Ethernet0",
        score=0.2,
    )


def test_link_integrity_failure_is_bound_to_one_covered_link():
    class LinkTools:
        def payload_integrity_link_test(self, **arguments):
            failed = arguments["device_b"] == "spine2"
            return ToolResult(
                success=True,
                data={
                    "directions": [
                        {
                            "packets_sent": 20,
                            "packets_observed": 20,
                            "missing_packets": 0,
                            "checksum_valid": not failed,
                            "checksum_failures": 3 if failed else 0,
                        },
                        {
                            "packets_sent": 20,
                            "packets_observed": 20,
                            "missing_packets": 0,
                            "checksum_valid": True,
                            "checksum_failures": 0,
                        },
                    ],
                    "method": "bounded_bidirectional_physical_link_icmp_checksum_capture",
                },
            )

    candidates = [_candidate(1), _candidate(2)]
    outcome = asyncio.run(
        LinkPayloadIntegrityProbe().run(
            SimpleNamespace(tools=LinkTools()),
            candidates=candidates,
            budget=ProbeBudget(BudgetConfig()),
            max_candidates=2,
        )
    )

    assert outcome.status == "completed"
    assert outcome.tool_calls == 2
    assert outcome.probe_packets == 160
    failed = next(item for item in outcome.evidence if item.value is True)
    assert failed.covered_links == (candidates[1].link_id,)
    assert failed.observed_path == (candidates[1].link_id,)
    assert failed.path_observation_confidence == 1.0


def test_single_link_checksum_missing_sequence_is_planning_only():
    class LinkTools:
        def payload_integrity_link_test(self, **_arguments):
            return ToolResult(
                success=True,
                data={
                    "directions": [
                        {
                            "packets_sent": 20,
                            "packets_observed": 19,
                            "missing_packets": 1,
                            "checksum_valid": True,
                            "checksum_failures": 0,
                        },
                        {
                            "packets_sent": 20,
                            "packets_observed": 20,
                            "missing_packets": 0,
                            "checksum_valid": True,
                            "checksum_failures": 0,
                        },
                    ],
                    "method": "bounded_bidirectional_physical_link_icmp_checksum_capture",
                },
            )

    outcome = asyncio.run(
        LinkPayloadIntegrityProbe().run(
            SimpleNamespace(tools=LinkTools()),
            candidates=[_candidate(1)],
            budget=ProbeBudget(BudgetConfig()),
            max_candidates=1,
        )
    )

    loss = next(item for item in outcome.evidence if item.category == "packet_loss_rate")
    assert loss.value == 1 / 40
    assert loss.supports_submission is False
    assert loss.metadata["planning_only"] is True
    assert loss.covered_links == (_candidate(1).link_id,)


def test_link_integrity_budget_exhaustion_is_zero_reliability_tool_error():
    outcome = asyncio.run(
        LinkPayloadIntegrityProbe().run(
            SimpleNamespace(tools=object()),
            candidates=[_candidate(1)],
            budget=ProbeBudget(BudgetConfig(max_active_probes_per_case=0)),
            max_candidates=1,
        )
    )

    assert outcome.status == "failed"
    assert outcome.tool_calls == 0
    assert outcome.probe_packets == 0
    assert outcome.evidence[0].category == "tool_error"
    assert outcome.evidence[0].reliability == 0


def test_link_integrity_timeout_never_becomes_corruption_evidence():
    class SlowTools:
        async def payload_integrity_link_test(self, **arguments):
            await asyncio.sleep(0.05)
            return ToolResult(success=True, data={})

    outcome = asyncio.run(
        LinkPayloadIntegrityProbe(CorruptionProbeConfig(timeout_seconds=0.001)).run(
            SimpleNamespace(tools=SlowTools()),
            candidates=[_candidate(1)],
            budget=ProbeBudget(BudgetConfig()),
            max_candidates=1,
        )
    )

    assert outcome.status == "failed"
    assert outcome.evidence[0].category == "tool_error"
    assert outcome.evidence[0].reliability == 0
    assert "timed out" in outcome.evidence[0].value["error"]
    assert all(item.category != "payload_integrity_failure" for item in outcome.evidence)


def test_empty_bounded_capture_is_missing_observation_not_loss():
    class EmptyCaptureTools:
        def payload_integrity_test(self, **arguments):
            return ToolResult(
                success=True,
                data={
                    "observation_complete": False,
                    "received": False,
                    "checksum_valid": None,
                    "packets_observed": 0,
                },
            )

    outcome = asyncio.run(
        PayloadIntegrityProbe().run(
            SimpleNamespace(tools=EmptyCaptureTools()),
            pair=ProbePair("client1", "192.0.2.2"),
            budget=ProbeBudget(BudgetConfig()),
        )
    )

    assert outcome.status == "partial"
    assert {item.category for item in outcome.evidence} == {"missing_observation"}
    assert all(item.reliability == 0 for item in outcome.evidence)
