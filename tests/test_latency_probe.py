import asyncio
from types import SimpleNamespace

from examples.agents.diagnostic_harness.config import BudgetConfig, LatencyProbeConfig
from examples.agents.diagnostic_harness.models import ProbePair, RankedInterfaceCandidate
from examples.agents.diagnostic_harness.probes import (
    DirectionalLinkLatencyProbe,
    ProbeBudget,
    RTTMatrixProbe,
    select_probe_pairs,
)
from examples.agents.diagnostic_harness.probes import base as probe_base
from netopsbench.platform.toolkit._core.common import ToolResult


class RTTTools:
    def __init__(self, samples_by_destination, error_destination=None):
        self.samples_by_destination = samples_by_destination
        self.error_destination = error_destination
        self.calls = []

    def ping_test(self, **arguments):
        self.calls.append(arguments)
        destination = arguments["dst_ip"]
        if destination == self.error_destination:
            return ToolResult(success=False, data=None, error="Ping timed out")
        samples = self.samples_by_destination[destination]
        count = arguments["count"]
        replies = "\n".join(
            f"64 bytes from {destination}: icmp_seq={index} ttl=64 time={value} ms"
            for index, value in enumerate(samples, 1)
        )
        output = (
            f"{replies}\n{count} packets transmitted, {len(samples)} received, 0% packet loss\n"
            f"rtt min/avg/max/mdev = {min(samples)}/{sum(samples) / len(samples)}/{max(samples)}/0.1 ms\n"
        )
        return ToolResult(
            success=True,
            data={
                "source": arguments["src"],
                "destination": destination,
                "count": count,
                "payload_size": None,
                "dont_fragment": False,
                "output": output,
                "return_code": 0,
            },
        )

    def ping_link_test(self, **arguments):
        self.calls.append(arguments)
        destination = arguments["target_device"]
        samples = self.samples_by_destination[destination]
        count = arguments["count"]
        replies = "\n".join(
            f"64 bytes from 10.0.0.2: icmp_seq={index} ttl=64 time={value} ms" for index, value in enumerate(samples, 1)
        )
        return ToolResult(
            success=True,
            data={
                "source": arguments["src"],
                "destination": "10.0.0.2",
                "count": count,
                "payload_size": None,
                "dont_fragment": False,
                "output": (
                    f"{replies}\n{count} packets transmitted, {len(samples)} received, 0% packet loss\n"
                    f"rtt min/avg/max/mdev = {min(samples)}/{sum(samples) / len(samples)}/{max(samples)}/0.1 ms\n"
                ),
                "return_code": 0,
            },
        )


def test_rtt_matrix_computes_median_p95_jitter_and_control_contrast():
    tools = RTTTools(
        {
            "192.0.2.10": (40.0, 41.0, 42.0, 43.0, 44.0, 45.0, 60.0),
            "192.0.2.20": (1.0, 1.1, 1.2, 1.0, 1.1, 1.2, 1.1),
        }
    )
    pairs = [
        ProbePair("client1", "192.0.2.10"),
        ProbePair("client1", "192.0.2.20", metadata={"control": True}),
    ]
    outcome = asyncio.run(
        RTTMatrixProbe(LatencyProbeConfig(samples_per_pair=7)).run(
            SimpleNamespace(tools=tools),
            pairs=pairs,
            budget=ProbeBudget(BudgetConfig()),
        )
    )

    assert outcome.status == "completed"
    assert outcome.tool_calls == 2
    assert outcome.probe_packets == 14
    assert all(call["count"] == 7 for call in tools.calls)
    anomalous = outcome.observations[0]
    assert anomalous.median_ms == 43.0
    assert anomalous.p95_ms == 60.0
    assert anomalous.min_ms == 40.0
    assert anomalous.jitter_ms > 0
    assert anomalous.absolute_anomaly
    assert anomalous.relative_anomaly
    assert anomalous.median_multiplier > 3.0
    assert anomalous.p95_multiplier > 3.0
    assert outcome.observations[1].control
    assert {item.category for item in outcome.evidence} == {
        "latency_median",
        "latency_p95",
        "packet_loss_rate",
    }


def test_exact_link_matrix_uses_same_role_cohort_as_relative_baseline():
    tools = RTTTools(
        {
            "spine1": (0.4,) * 7,
            "spine2": (0.5,) * 7,
            "spine3": (18.0,) * 7,
            "spine4": (0.6,) * 7,
        }
    )
    pairs = [
        ProbePair(
            "leaf1",
            spine,
            metadata={
                "link_probe": True,
                "selection": "adaptive_ecmp_link_isolation",
                "source_interface": f"Ethernet{index * 4}",
                "target_device": spine,
                "target_interface": "Ethernet0",
            },
        )
        for index, spine in enumerate(("spine1", "spine2", "spine3", "spine4"))
    ]

    outcome = asyncio.run(
        RTTMatrixProbe(LatencyProbeConfig(samples_per_pair=7, absolute_threshold_ms=30.0)).run(
            SimpleNamespace(tools=tools),
            pairs=pairs,
            budget=ProbeBudget(BudgetConfig(max_active_probes_per_case=8)),
        )
    )

    slow = next(item for item in outcome.observations if item.destination == "spine3")
    assert not slow.absolute_anomaly
    assert slow.relative_anomaly
    assert slow.reference_median_ms == 0.45
    assert slow.median_multiplier == 40.0
    assert sum(item.relative_anomaly for item in outcome.observations) == 1


def test_rtt_tool_error_is_not_latency_evidence():
    tools = RTTTools({"192.0.2.20": (1.0,) * 7}, error_destination="192.0.2.10")
    outcome = asyncio.run(
        RTTMatrixProbe().run(
            SimpleNamespace(tools=tools),
            pairs=[ProbePair("client1", "192.0.2.10")],
            budget=ProbeBudget(BudgetConfig()),
        )
    )

    assert outcome.status == "failed"
    assert len(outcome.evidence) == 1
    assert outcome.evidence[0].category == "tool_error"
    assert outcome.evidence[0].reliability == 0


def test_directional_link_rtt_does_not_claim_endpoint_from_round_trip_measurement():
    tools = RTTTools({"spine1": (45.0,) * 7, "leaf1": (1.0,) * 7})
    pairs = [
        ProbePair(
            "leaf1",
            "spine1",
            metadata={
                "link_probe": True,
                "directional_endpoint_probe": True,
                "source_interface": "Ethernet0",
                "target_device": "spine1",
                "target_interface": "Ethernet4",
            },
        ),
        ProbePair(
            "spine1",
            "leaf1",
            metadata={
                "link_probe": True,
                "directional_endpoint_probe": True,
                "source_interface": "Ethernet4",
                "target_device": "leaf1",
                "target_interface": "Ethernet0",
            },
        ),
    ]

    outcome = asyncio.run(
        RTTMatrixProbe(LatencyProbeConfig(samples_per_pair=7)).run(
            SimpleNamespace(tools=tools), pairs=pairs, budget=ProbeBudget(BudgetConfig())
        )
    )

    anomalous = [
        item for item in outcome.evidence if item.category == "latency_median" and item.metadata["category_anomaly"]
    ]
    assert len(anomalous) == 1
    assert "fault_endpoint_device" not in anomalous[0].metadata
    assert "fault_endpoint_interface" not in anomalous[0].metadata


def test_one_way_link_latency_binds_only_the_slow_egress_endpoint():
    class Tools:
        def latency_link_test(self, **arguments):
            return ToolResult(
                success=True,
                data={
                    "directions": [
                        {
                            "source": arguments["device_a"],
                            "source_interface": arguments["interface_a"],
                            "target_device": arguments["device_b"],
                            "target_interface": arguments["interface_b"],
                            "sample_count": arguments["count"],
                            "median_ms": 120.0,
                            "p95_ms": 121.0,
                        },
                        {
                            "source": arguments["device_b"],
                            "source_interface": arguments["interface_b"],
                            "target_device": arguments["device_a"],
                            "target_interface": arguments["interface_a"],
                            "sample_count": arguments["count"],
                            "median_ms": 0.4,
                            "p95_ms": 0.6,
                        },
                    ]
                },
            )

    candidate = RankedInterfaceCandidate(
        link_id="spine1:Ethernet4--leaf2:Ethernet0",
        primary_device="leaf2",
        primary_interface="Ethernet0",
        peer_device="spine1",
        peer_interface="Ethernet4",
        score=0.8,
    )
    budget = ProbeBudget(BudgetConfig())
    outcome = asyncio.run(
        DirectionalLinkLatencyProbe(LatencyProbeConfig(samples_per_pair=7)).run(
            SimpleNamespace(tools=Tools()), candidate=candidate, budget=budget
        )
    )

    assert outcome.status == "completed"
    assert outcome.tool_calls == 1
    assert outcome.probe_packets == 14
    assert outcome.metadata["fault_endpoint_device"] == "leaf2"
    direct = [
        item for item in outcome.evidence if item.category == "latency_median" and item.metadata["category_anomaly"]
    ]
    assert len(direct) == 1
    assert direct[0].covered_links == (candidate.link_id,)
    assert direct[0].metadata["fault_endpoint_interface"] == "Ethernet0"


def test_p95_only_jitter_is_not_direct_latency_link_evidence():
    tools = RTTTools(
        {
            "192.0.2.10": (1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 5.0),
            "192.0.2.20": (1.0,) * 7,
        }
    )
    pairs = [
        ProbePair("client1", "192.0.2.10"),
        ProbePair("client1", "192.0.2.20", metadata={"control": True}),
    ]
    outcome = asyncio.run(
        RTTMatrixProbe(LatencyProbeConfig(samples_per_pair=7)).run(
            SimpleNamespace(tools=tools), pairs=pairs, budget=ProbeBudget(BudgetConfig())
        )
    )
    median = next(item for item in outcome.evidence if item.category == "latency_median")
    p95 = next(item for item in outcome.evidence if item.category == "latency_p95")
    assert median.metadata["category_anomaly"] is False
    assert p95.metadata["category_anomaly"] is False
    assert p95.metadata["p95_relative_anomaly"] is True


def test_missing_per_packet_samples_is_missing_observation_not_synthetic_median():
    class SummaryOnlyTools:
        def ping_test(self, **arguments):
            return ToolResult(
                success=True,
                data={
                    "source": arguments["src"],
                    "destination": arguments["dst_ip"],
                    "count": arguments["count"],
                    "payload_size": None,
                    "dont_fragment": False,
                    "output": (
                        "7 packets transmitted, 7 received, 0% packet loss\nrtt min/avg/max/mdev = 1.0/2.0/3.0/0.2 ms\n"
                    ),
                    "return_code": 0,
                },
            )

    outcome = asyncio.run(
        RTTMatrixProbe().run(
            SimpleNamespace(tools=SummaryOnlyTools()),
            pairs=[ProbePair("client1", "192.0.2.10")],
            budget=ProbeBudget(BudgetConfig()),
        )
    )

    assert outcome.status == "failed"
    assert outcome.observations == ()
    assert outcome.evidence[0].category == "missing_observation"
    assert outcome.evidence[0].reliability == 0


def test_rtt_matrix_obeys_active_probe_budget():
    tools = RTTTools({"192.0.2.10": (1.0,) * 7, "192.0.2.20": (1.0,) * 7})
    budget = ProbeBudget(BudgetConfig(max_active_probes_per_case=1))
    outcome = asyncio.run(
        RTTMatrixProbe().run(
            SimpleNamespace(tools=tools),
            pairs=[ProbePair("client1", "192.0.2.10"), ProbePair("client2", "192.0.2.20")],
            budget=budget,
        )
    )

    assert len(tools.calls) == 1
    assert outcome.status == "partial"
    assert any(item.category == "tool_error" for item in outcome.evidence)


def test_latency_pair_selection_includes_anomaly_control_and_unique_link_matrix(monkeypatch):
    def device(name, role, *, data_ip=None, attached_switch=None):
        return SimpleNamespace(
            name=name,
            role=SimpleNamespace(value=role),
            data_ip=data_ip,
            attached_switch=attached_switch,
        )

    devices = [
        device("spine1", "spine"),
        device("spine2", "spine"),
        device("leaf1", "leaf"),
        device("leaf2", "leaf"),
        device("leaf3", "leaf"),
        device("client1", "client", data_ip="192.0.2.1", attached_switch="leaf1"),
        device("client2", "client", data_ip="192.0.2.2", attached_switch="leaf2"),
        device("client3", "client", data_ip="192.0.2.3", attached_switch="leaf3"),
    ]
    by_name = {item.name: item for item in devices}
    manifest = SimpleNamespace(
        clients=lambda: [item for item in devices if item.role.value == "client"],
        device=by_name.get,
        links=[
            SimpleNamespace(
                endpoints=(
                    SimpleNamespace(device="spine1", interface="eth2"),
                    SimpleNamespace(device="leaf2", interface="eth1"),
                )
            ),
            SimpleNamespace(
                endpoints=(
                    SimpleNamespace(device="spine2", interface="eth2"),
                    SimpleNamespace(device="leaf2", interface="eth2"),
                )
            ),
        ],
    )
    monkeypatch.setattr(probe_base, "_load_context_manifest", lambda _context: manifest)
    context = SimpleNamespace(
        symptoms={
            "observations": {
                "pingmesh_metrics": {
                    "anomalies": [
                        {
                            "type": "latency_spike",
                            "src_name": "client1",
                            "dst_name": "client2",
                            "dst_ip": "192.0.2.2",
                            "src_leaf": "leaf1",
                            "dst_leaf": "leaf2",
                            "value": 80.0,
                            "baseline": 1.0,
                            "threshold": 21.0,
                            "severity": "high",
                            "persistence": "persistent",
                        },
                        {
                            "type": "latency_spike",
                            "src_name": "client3",
                            "dst_name": "client2",
                            "dst_ip": "192.0.2.2",
                            "src_leaf": "leaf3",
                            "dst_leaf": "leaf2",
                            "value": 70.0,
                            "baseline": 1.0,
                        },
                    ]
                }
            }
        }
    )

    pairs = select_probe_pairs(
        context,
        family="high_latency",
        max_pairs=4,
        anomaly_pairs=1,
        control_pairs=1,
        link_isolation=True,
    )

    assert [pair.metadata["selection"] for pair in pairs] == [
        "anomaly",
        "healthy_control",
        "link_isolation",
        "link_isolation",
    ]
    assert pairs[1].source_leaf != "leaf2" and pairs[1].destination_leaf != "leaf2"
    assert {(pair.source, pair.destination) for pair in pairs[2:]} == {
        ("spine1", "leaf2"),
        ("spine2", "leaf2"),
    }
    assert all(pair.metadata["source_interface"] == "eth2" for pair in pairs[2:])
    assert {pair.metadata["target_interface"] for pair in pairs[2:]} == {"eth1", "eth2"}
    assert all(pair.metadata["link_probe"] for pair in pairs[2:])


def test_rtt_matrix_uses_real_single_link_tool_for_isolation_pair():
    tools = RTTTools({"leaf2": (80.0,) * 7})
    pair = ProbePair(
        "spine1",
        "leaf2",
        metadata={
            "selection": "link_isolation",
            "link_probe": True,
            "source_interface": "eth2",
            "target_device": "leaf2",
            "target_interface": "eth1",
        },
    )

    outcome = asyncio.run(
        RTTMatrixProbe().run(
            SimpleNamespace(tools=tools),
            pairs=[pair],
            budget=ProbeBudget(BudgetConfig()),
        )
    )

    assert outcome.status == "completed"
    assert tools.calls == [
        {
            "src": "spine1",
            "target_device": "leaf2",
            "source_interface": "eth2",
            "target_interface": "eth1",
            "count": 7,
            "payload_size": None,
        }
    ]
    assert outcome.observations[0].absolute_anomaly
    assert all(item.metadata["link_probe"] for item in outcome.evidence)
