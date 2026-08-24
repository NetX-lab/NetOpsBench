from __future__ import annotations

import pytest

from examples.agents.diagnostic_harness.evidence.base_tools import (
    evidence_from_base_tool_observations,
    explicit_route_policy_denies,
    parse_active_acl_drop,
    trace_step_count,
)
from examples.agents.diagnostic_harness.normalization.interface import LinkEndpoint, PhysicalLink, TopologyIndex
from netopsbench.agents.base import DiagnosticContext
from netopsbench.agents.tracing import AgentTraceRecorder

from .diagnostic_harness_helpers import sample_topology


def _context() -> DiagnosticContext:
    return DiagnosticContext(
        scenario_id="opaque-runtime-correlation",
        topology={},
        symptoms={},
        trace=AgentTraceRecorder(),
    )


def _record(context: DiagnosticContext, tool: str, args: dict, output, *, call_id: str) -> None:
    context.trace.record_tool_start(name=tool, args=args, run_id=call_id)
    context.trace.record_tool_end(output=output, run_id=call_id)


@pytest.mark.parametrize(
    "output",
    [
        {"device": "leaf5", "interfaces": [{"name": "Ethernet4", "admin": "up", "oper": "down"}]},
        {
            "artifact": {
                "structured_content": {
                    "device": "leaf5",
                    "interfaces": [{"name": "Ethernet4", "admin": "up", "oper": "down"}],
                }
            }
        },
    ],
)
def test_adapter_is_independent_of_provider_result_envelope(output):
    context = _context()
    _record(context, "get_device_interfaces", {"device": "leaf5"}, output, call_id="interfaces")

    evidence = evidence_from_base_tool_observations(context, sample_topology())

    assert {(item.category, item.entity_id, item.value) for item in evidence} == {
        ("interface_admin_state", "leaf5:Ethernet4", "up"),
        ("interface_oper_state", "leaf5:Ethernet4", "down"),
    }
    assert all(item.origin.value == "live_telemetry" for item in evidence)
    assert all(item.source == "base_tool:get_device_interfaces" for item in evidence)


def test_adapter_ignores_tool_errors_unallowlisted_tools_and_old_trace_steps():
    context = _context()
    _record(
        context,
        "get_device_interfaces",
        {"device": "leaf5"},
        {"device": "leaf5", "interfaces": [{"name": "Ethernet4", "admin": "down", "oper": "down"}]},
        call_id="old",
    )
    cursor = trace_step_count(context)
    context.trace.record_tool_start(name="get_route_table", args={"device": "leaf5"}, run_id="failed")
    context.trace.record_tool_error(error=TimeoutError("timeout"), run_id="failed")
    _record(
        context,
        "read_evaluator_answer",
        {},
        {"interfaces": [{"name": "Ethernet4", "oper": "down"}]},
        call_id="untrusted",
    )

    assert evidence_from_base_tool_observations(context, sample_topology(), start_index=cursor) == []


def test_adapter_preserves_timestamped_live_interface_transitions():
    context = _context()
    _record(
        context,
        "get_device_logs",
        {"device": "leaf5"},
        {
            "device": "leaf5",
            "logs": [
                {
                    "time": "2026-08-12T09:22:02Z",
                    "message": "Port Ethernet4 oper state set from up to down",
                },
                {
                    "time": "2026-08-12T09:22:06Z",
                    "message": 'EVENT_PUBLISHED: {"ifname":"Ethernet4","status":"up"}',
                },
                {"time": "2026-08-12T09:22:07Z", "message": "Created next hop on Ethernet4"},
            ],
        },
        call_id="logs",
    )

    evidence = evidence_from_base_tool_observations(context, sample_topology())

    assert len(evidence) == 2
    assert {item.entity_id for item in evidence} == {"leaf5:Ethernet4"}
    assert all(item.category == "syslog_event" for item in evidence)
    assert len({item.independence_key for item in evidence}) == 2
    assert all(item.metadata["temporal_transition"] for item in evidence)


def test_adapter_emits_planning_only_local_mtu_outlier():
    leaf_endpoints = [LinkEndpoint("leaf1", f"Ethernet{index}", ()) for index in (0, 4, 8)]
    spine_endpoints = [LinkEndpoint(f"spine{index}", "Ethernet0", ()) for index in (1, 2, 3)]
    topology = TopologyIndex(
        devices={"leaf1": "leaf", "spine1": "spine", "spine2": "spine", "spine3": "spine"},
        links=tuple(
            PhysicalLink(
                f"leaf1:Ethernet{port}--spine{index}:Ethernet0",
                leaf,
                spine,
            )
            for index, port, leaf, spine in zip((1, 2, 3), (0, 4, 8), leaf_endpoints, spine_endpoints, strict=True)
        ),
        source="test",
    )
    context = _context()
    _record(
        context,
        "get_device_interfaces",
        {"device": "leaf1"},
        {
            "device": "leaf1",
            "interfaces": [
                {"name": "Ethernet0", "admin": "up", "oper": "up", "mtu": 9100},
                {"name": "Ethernet4", "admin": "up", "oper": "up", "mtu": 9100},
                {"name": "Ethernet8", "admin": "up", "oper": "up", "mtu": 1400},
            ],
        },
        call_id="interfaces-mtu",
    )

    evidence = evidence_from_base_tool_observations(context, topology)
    outlier = next(item for item in evidence if item.category == "configuration_difference")

    assert outlier.entity_id == "leaf1:Ethernet8"
    assert outlier.value == {"local_mtu": 1400, "device_mode_mtu": 9100, "different": True}
    assert outlier.metadata["semantic_family"] == "mtu"
    assert not outlier.supports_submission


def test_adapter_marks_explicit_bgp_configuration_error_separately_from_state():
    context = _context()
    _record(
        context,
        "get_bgp_neighbors",
        {"device": "leaf5"},
        {
            "device": "leaf5",
            "neighbors": [{"neighbor": "10.0.0.1", "state": "Idle", "last_error": "Bad Peer AS"}],
        },
        call_id="bgp-config",
    )

    evidence = evidence_from_base_tool_observations(context, sample_topology())

    assert len(evidence) == 1
    assert evidence[0].metadata["direct_bgp_configuration_evidence"] is True


def test_plain_static_route_is_candidate_context_not_fault_support():
    context = _context()
    _record(
        context,
        "get_device_config",
        {"device": "leaf5"},
        {"device": "leaf5", "config": "ip route 192.0.2.0/24 192.0.2.1\n"},
        call_id="config",
    )
    _record(
        context,
        "get_route_table",
        {"device": "leaf5", "prefix": "192.0.2.0/24"},
        {
            "device": "leaf5",
            "routes": [
                {
                    "prefix": "192.0.2.0/24",
                    "protocol": "static",
                    "selected": True,
                    "nexthops": [{"address": "192.0.2.1"}],
                }
            ],
        },
        call_id="route",
    )

    evidence = evidence_from_base_tool_observations(context, sample_topology())

    assert {item.category for item in evidence} == {"configured_static_route", "observed_routing_entry"}
    assert all(not item.supports_submission for item in evidence)
    assert all(item.usable_for_planning for item in evidence)


def test_selected_discard_route_is_direct_live_route_evidence():
    context = _context()
    _record(
        context,
        "get_route_table",
        {"device": "leaf5", "prefix": "198.51.100.0/24"},
        {
            "device": "leaf5",
            "routes": [
                {
                    "prefix": "198.51.100.0/24",
                    "protocol": "static",
                    "selected": True,
                    "is_discard": True,
                    "nexthops": [{"interface": "Null0"}],
                }
            ],
        },
        call_id="route",
    )

    evidence = evidence_from_base_tool_observations(context, sample_topology())

    assert len(evidence) == 1
    assert evidence[0].category == "observed_routing_entry"
    assert evidence[0].value["is_discard"] is True
    assert evidence[0].supports_submission is True


def test_missing_bgp_rib_entry_is_noncausal_route_consequence():
    context = _context()
    _record(
        context,
        "get_bgp_rib",
        {"device": "leaf5", "prefix": "203.0.113.0/24"},
        {
            "device": "leaf5",
            "prefix": "203.0.113.0/24",
            "bgp_rib": "% Network not in table\n",
        },
        call_id="bgp-rib",
    )

    evidence = evidence_from_base_tool_observations(context, sample_topology())

    assert len(evidence) == 1
    assert evidence[0].category == "route_presence"
    assert "semantic_family" not in evidence[0].metadata
    assert evidence[0].metadata["missing_bgp_route"] is True
    assert evidence[0].metadata["route_semantic_candidate"] is True
    assert evidence[0].usable_for_planning
    assert not evidence[0].supports_submission


def test_active_acl_and_drop_counter_are_reused_as_structured_evidence():
    context = _context()
    _record(
        context,
        "get_device_acl",
        {"device": "leaf5"},
        {
            "device": "leaf5",
            "sonic_acl_config": (
                "Name Type Binding Description Stage Status\n"
                "EDGE_FILTER L3 Ethernet4 configured-policy ingress Active\n"
                "Table Rule Priority Action Match Status\n"
                "EDGE_FILTER RULE_1 999 DROP DST_IP: 192.0.2.0/24 Active\n"
            ),
            "iptables_forward_rules": "1 17 680 DROP 0 -- * * 0.0.0.0/0 192.0.2.0/24\n",
        },
        call_id="acl",
    )

    evidence = evidence_from_base_tool_observations(context, sample_topology())

    assert {item.category for item in evidence} == {"configuration_difference", "interface_counter_delta"}
    assert {item.entity_id for item in evidence} == {"leaf5:Ethernet4"}
    assert {item.metadata["semantic_family"] for item in evidence} == {"acl"}
    assert len({item.independence_key for item in evidence}) == 2


def test_acl_parser_requires_observed_drop_traffic():
    payload = {
        "sonic_acl_config": (
            "EDGE_FILTER L3 Ethernet4 configured-policy ingress Active\n"
            "EDGE_FILTER RULE_1 999 DROP DST_IP: 192.0.2.0/24 Active\n"
        ),
        "iptables_forward_rules": "1 0 0 DROP 0 -- * * 0.0.0.0/0 192.0.2.0/24\n",
    }

    assert parse_active_acl_drop(payload) is None


def test_positive_interface_counter_delta_is_location_only_evidence():
    context = _context()
    _record(
        context,
        "get_interface_metrics",
        {"device": "leaf5", "interface": "Ethernet4"},
        {
            "device": "leaf5",
            "interface": "Ethernet4",
            "summary": {
                "in_discarded_packets": {"window_delta": 23},
                "in_errors": {"window_delta": 0},
            },
        },
        call_id="metrics",
    )

    evidence = evidence_from_base_tool_observations(context, sample_topology())

    assert len(evidence) == 1
    assert evidence[0].category == "interface_counter_delta"
    assert evidence[0].entity_id == "leaf5:Ethernet4"
    assert evidence[0].value == {"metric": "in_discarded_packets", "delta": 23.0}
    assert not evidence[0].supports_submission


def test_pingmesh_tool_results_remain_structured_planning_evidence():
    context = _context()
    _record(
        context,
        "get_pingmesh_summary",
        {},
        {
            "time_scope": {"start_time": "2026-08-11T00:00:00Z", "end_time": "2026-08-11T00:01:00Z"},
            "path_type_summary": {
                "cross_rack": {"packet_loss": 0.06, "rtt_p99": 1.2},
                "same_rack": {"packet_loss": 0.0, "rtt_p99": 0.4},
            },
        },
        call_id="summary",
    )
    _record(
        context,
        "get_pingmesh_hotspots",
        {},
        {
            "time_scope": {"start_time": "2026-08-11T00:00:00Z", "end_time": "2026-08-11T00:01:00Z"},
            "hotspots": [{"src_leaf": "leaf1", "dst_leaf": "leaf5", "packet_loss": 1.5}],
        },
        call_id="hotspots",
    )

    evidence = evidence_from_base_tool_observations(context, sample_topology())
    cross_rack = next(
        item for item in evidence if item.entity_id == "cross_rack" and item.category == "packet_loss_rate"
    )
    hotspot = next(item for item in evidence if item.entity_id == "leaf1--leaf5")

    assert cross_rack.value == pytest.approx(0.06)
    assert cross_rack.metadata["weak_performance_symptom"] is True
    assert hotspot.value == pytest.approx(0.015)
    assert hotspot.metadata["leaf_aggregate"] is True
    assert hotspot.origin.value == "live_telemetry"
    assert hotspot.supports_submission is True


def test_directional_link_latency_is_exact_typed_evidence():
    context = _context()
    _record(
        context,
        "latency_link_test",
        {
            "device_a": "leaf5",
            "interface_a": "Ethernet4",
            "device_b": "spine3",
            "interface_b": "Ethernet4",
            "count": 7,
        },
        {
            "device_a": "leaf5",
            "interface_a": "Ethernet4",
            "device_b": "spine3",
            "interface_b": "Ethernet4",
            "count_per_direction": 7,
            "directions": [
                {
                    "source": "leaf5",
                    "source_interface": "Ethernet4",
                    "target_device": "spine3",
                    "target_interface": "Ethernet4",
                    "sample_count": 7,
                    "median_ms": 120.0,
                    "p95_ms": 121.0,
                },
                {
                    "source": "spine3",
                    "source_interface": "Ethernet4",
                    "target_device": "leaf5",
                    "target_interface": "Ethernet4",
                    "sample_count": 7,
                    "median_ms": 0.4,
                    "p95_ms": 0.6,
                },
            ],
        },
        call_id="one-way-latency",
    )

    evidence = evidence_from_base_tool_observations(context, sample_topology())

    assert len(evidence) == 4
    assert {item.category for item in evidence} == {"latency_median", "latency_p95"}
    assert all(item.covered_links == ("leaf5:Ethernet4--spine3:Ethernet4",) for item in evidence)
    assert all(item.path_observation_confidence == 1.0 for item in evidence)
    assert len({item.independence_key for item in evidence}) == 2
    slow = next(item for item in evidence if item.category == "latency_median" and item.value == 120.0)
    healthy = next(item for item in evidence if item.category == "latency_median" and item.value == 0.4)
    assert slow.metadata["category_anomaly"] is True
    assert slow.metadata["fault_endpoint_device"] == "leaf5"
    assert slow.metadata["fault_endpoint_interface"] == "Ethernet4"
    assert healthy.metadata["category_anomaly"] is False


def test_directional_link_latency_rejects_non_peer_endpoints():
    context = _context()
    _record(
        context,
        "latency_link_test",
        {
            "device_a": "leaf5",
            "interface_a": "Ethernet4",
            "device_b": "spine1",
            "interface_b": "Ethernet0",
        },
        {
            "directions": [
                {
                    "source": "leaf5",
                    "source_interface": "Ethernet4",
                    "target_device": "spine1",
                    "target_interface": "Ethernet0",
                    "sample_count": 7,
                    "median_ms": 120.0,
                },
                {
                    "source": "spine1",
                    "source_interface": "Ethernet0",
                    "target_device": "leaf5",
                    "target_interface": "Ethernet4",
                    "sample_count": 7,
                    "median_ms": 0.4,
                },
            ]
        },
        call_id="invalid-link",
    )

    assert evidence_from_base_tool_observations(context, sample_topology()) == []


def test_link_ping_is_reused_as_exact_path_loss_evidence():
    context = _context()
    _record(
        context,
        "ping_link_test",
        {
            "src": "leaf5",
            "source_interface": "Ethernet4",
            "target_device": "spine3",
            "target_interface": "Ethernet4",
            "count": 20,
        },
        {
            "source": "leaf5",
            "source_interface": "Ethernet4",
            "target_device": "spine3",
            "target_interface": "Ethernet4",
            "destination": "10.0.0.2",
            "count": 20,
            "output": "20 packets transmitted, 15 received, 25% packet loss\n",
            "return_code": 1,
        },
        call_id="link-loss",
    )

    evidence = evidence_from_base_tool_observations(context, sample_topology())

    assert len(evidence) == 1
    assert evidence[0].category == "packet_loss_rate"
    assert evidence[0].value == pytest.approx(0.25)
    assert evidence[0].covered_links == ("leaf5:Ethernet4--spine3:Ethernet4",)
    assert evidence[0].metadata["rounds"] == 1


def test_link_payload_integrity_preserves_corruption_direction_and_scope():
    context = _context()
    _record(
        context,
        "payload_integrity_link_test",
        {
            "device_a": "leaf5",
            "interface_a": "Ethernet4",
            "device_b": "spine3",
            "interface_b": "Ethernet4",
            "count": 40,
        },
        {
            "supported": True,
            "device_a": "leaf5",
            "interface_a": "Ethernet4",
            "device_b": "spine3",
            "interface_b": "Ethernet4",
            "directions": [
                {
                    "source": "leaf5",
                    "source_interface": "Ethernet4",
                    "target_device": "spine3",
                    "target_interface": "Ethernet4",
                    "packets_sent": 40,
                    "packets_observed": 40,
                    "missing_packets": 0,
                    "checksum_valid": False,
                    "checksum_failures": 3,
                    "integrity_complete": True,
                },
                {
                    "source": "spine3",
                    "source_interface": "Ethernet4",
                    "target_device": "leaf5",
                    "target_interface": "Ethernet4",
                    "packets_sent": 40,
                    "packets_observed": 40,
                    "missing_packets": 0,
                    "checksum_valid": True,
                    "checksum_failures": 0,
                    "integrity_complete": True,
                },
            ],
        },
        call_id="link-integrity",
    )

    evidence = evidence_from_base_tool_observations(context, sample_topology())

    assert len(evidence) == 2
    failed = next(item for item in evidence if item.value is True)
    assert failed.category == "payload_integrity_failure"
    assert failed.covered_links == ("leaf5:Ethernet4--spine3:Ethernet4",)
    assert failed.metadata["fault_endpoint_device"] == "leaf5"
    assert failed.metadata["fault_endpoint_interface"] == "Ethernet4"


def test_integrity_missing_packets_remain_planning_only_loss():
    context = _context()
    _record(
        context,
        "payload_integrity_test",
        {"src": "client1", "dst_ip": "192.0.2.2", "count": 20},
        {
            "source": "client1",
            "destination": "192.0.2.2",
            "supported": True,
            "packets_sent": 20,
            "packets_observed": 18,
            "missing_packets": 2,
            "checksum_valid": True,
            "checksum_failures": 0,
            "integrity_complete": False,
        },
        call_id="integrity-loss",
    )

    evidence = evidence_from_base_tool_observations(context, sample_topology())

    integrity = next(item for item in evidence if item.category == "payload_integrity_failure")
    loss = next(item for item in evidence if item.category == "packet_loss_rate")
    assert integrity.value is False
    assert loss.value == pytest.approx(0.1)
    assert loss.supports_submission is False
    assert not loss.covered_links


def test_route_map_deny_resolves_matched_prefix_list_without_model_wording():
    config = """
ip prefix-list PL-BLOCK seq 10 permit 203.0.113.0/24
route-map EXPORT deny 10
 match ip address prefix-list PL-BLOCK
route-map EXPORT permit 20
"""

    assert explicit_route_policy_denies(config) == {"203.0.113.0/24"}

    context = _context()
    _record(
        context,
        "get_device_config",
        {"device": "leaf5"},
        {"device": "leaf5", "config": config},
        call_id="policy",
    )
    evidence = evidence_from_base_tool_observations(context, sample_topology())

    assert len(evidence) == 1
    assert evidence[0].category == "configuration_difference"
    assert evidence[0].value["prefix"] == "203.0.113.0/24"
    # An isolated deny statement is a verification candidate, not proof that
    # it caused this diagnosis.  Route-state consequence must still close it.
    assert evidence[0].supports_submission is False


def test_abnormal_bgp_neighbor_remains_direct_live_evidence():
    context = _context()
    _record(
        context,
        "get_bgp_neighbors",
        {"device": "leaf5"},
        {
            "device": "leaf5",
            "neighbors": [
                {"peer": "10.0.0.1", "state": "ESTABLISHED"},
                {"peer": "10.0.0.2", "state": "ACTIVE"},
            ],
        },
        call_id="bgp-neighbors",
    )

    evidence = evidence_from_base_tool_observations(context, sample_topology())

    assert len(evidence) == 1
    assert evidence[0].category == "bgp_neighbor_state"
    assert evidence[0].entity_id == "leaf5"
    assert evidence[0].value["neighbors"] == [{"peer": "10.0.0.2", "state": "ACTIVE"}]
    assert evidence[0].supports_submission is True
