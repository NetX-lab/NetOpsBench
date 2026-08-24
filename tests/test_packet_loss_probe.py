import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

from examples.agents.diagnostic_harness.config import BudgetConfig, PacketLossProbeConfig
from examples.agents.diagnostic_harness.models import Evidence, EvidenceOrigin, ProbePair
from examples.agents.diagnostic_harness.probes import ProbeBudget, RepeatedPacketLossProbe, select_probe_pairs
from examples.agents.diagnostic_harness.probes import base as probe_base
from examples.agents.diagnostic_harness.probes.base import parse_ping_payload
from netopsbench.platform.toolkit._core.common import ToolResult


def _ping_output(sent: int, received: int, rtts=(1.0, 1.5)) -> str:
    loss = 100 * (sent - received) / sent
    replies = "\n".join(
        f"64 bytes from 192.0.2.2: icmp_seq={index} ttl=64 time={rtt} ms" for index, rtt in enumerate(rtts, 1)
    )
    return (
        f"{replies}\n{sent} packets transmitted, {received} received, {loss:g}% packet loss\n"
        "rtt min/avg/max/mdev = 1.000/1.250/1.500/0.250 ms\n"
    )


class PingTools:
    def __init__(self, received_by_call=None):
        self.calls = []
        self.received_by_call = list(received_by_call or [])

    def ping_test(self, **arguments):
        self.calls.append(arguments)
        count = arguments["count"]
        received = self.received_by_call.pop(0) if self.received_by_call else count
        return ToolResult(
            success=True,
            data={
                "source": arguments["src"],
                "destination": arguments["dst_ip"],
                "count": count,
                "payload_size": arguments["payload_size"],
                "dont_fragment": arguments["dont_fragment"],
                "output": _ping_output(count, received),
                "return_code": 0 if received else 1,
            },
        )

    def ping_link_test(self, **arguments):
        self.calls.append(arguments)
        count = arguments["count"]
        received = self.received_by_call.pop(0) if self.received_by_call else count
        return ToolResult(
            success=True,
            data={
                "source": arguments["src"],
                "destination": "10.0.0.2",
                "count": count,
                "payload_size": arguments["payload_size"],
                "dont_fragment": False,
                "output": _ping_output(count, received),
                "return_code": 0 if received else 1,
            },
        )


def test_ping_parser_uses_real_linux_output_fields():
    observation = parse_ping_payload(
        {
            "source": "client1",
            "destination": "192.0.2.2",
            "count": 3,
            "payload_size": 1472,
            "dont_fragment": True,
            "output": _ping_output(3, 2, (1.2, 1.8)),
            "return_code": 1,
        }
    )

    assert observation.sent == 3
    assert observation.received == 2
    assert observation.loss_rate == 1 / 3
    assert observation.rtt_samples_ms == (1.2, 1.8)
    assert observation.payload_size == 1472
    assert observation.dont_fragment


def test_repeated_loss_splits_at_real_tool_count_cap_and_aggregates_rounds():
    tools = PingTools(received_by_call=[16, 8, 16, 8])
    context = SimpleNamespace(tools=tools)
    budget = ProbeBudget(BudgetConfig(max_extra_tool_calls_per_hard_case=12, max_probe_packets_per_case=500))
    probe = RepeatedPacketLossProbe(PacketLossProbeConfig(packets_per_pair=30, repeat_rounds=2))

    outcome = asyncio.run(
        probe.run(
            context,
            pairs=[ProbePair("client1", "192.0.2.2")],
            budget=budget,
        )
    )

    assert [call["count"] for call in tools.calls] == [20, 10, 20, 10]
    assert outcome.tool_calls == 4
    assert outcome.probe_packets == 60
    assert outcome.status == "completed"
    result = outcome.observations[0]
    assert (result.sent, result.received, result.rounds) == (60, 48, 2)
    assert result.loss_rate == 0.2
    evidence = next(item for item in outcome.evidence if item.category == "packet_loss_rate")
    assert evidence.metadata["strong"]


def test_repeated_loss_stops_at_shared_tool_budget_without_guessing():
    tools = PingTools()
    context = SimpleNamespace(tools=tools)
    budget = ProbeBudget(BudgetConfig(max_extra_tool_calls_per_hard_case=2, max_probe_packets_per_case=500))
    probe = RepeatedPacketLossProbe(PacketLossProbeConfig(packets_per_pair=30, repeat_rounds=2))

    outcome = asyncio.run(
        probe.run(
            context,
            pairs=[ProbePair("client1", "192.0.2.2"), ProbePair("client2", "192.0.2.3")],
            budget=budget,
        )
    )

    assert len(tools.calls) == 2
    assert outcome.status == "partial"
    assert any(item.category == "tool_error" and item.reliability == 0 for item in outcome.evidence)


def test_probe_timeout_becomes_tool_error_evidence():
    class SlowTools:
        async def ping_test(self, **arguments):
            await asyncio.sleep(0.05)
            return ToolResult(success=True, data={})

    probe = RepeatedPacketLossProbe(PacketLossProbeConfig(packets_per_pair=1, repeat_rounds=1, timeout_seconds=0.001))
    outcome = asyncio.run(
        probe.run(
            SimpleNamespace(tools=SlowTools()),
            pairs=[ProbePair("client1", "192.0.2.2")],
            budget=ProbeBudget(BudgetConfig()),
        )
    )

    assert outcome.status == "failed"
    assert outcome.evidence[0].category == "tool_error"
    assert "timed out" in outcome.evidence[0].value["error"]


def test_pair_selection_uses_public_pingmesh_observation_not_case_identity():
    context = SimpleNamespace(
        symptoms={
            "observations": {
                "pingmesh_metrics": {
                    "anomalies": [
                        {
                            "type": "packet_loss",
                            "src_name": "client3",
                            "dst_ip": "192.168.105.2",
                            "dst_name": "client9",
                            "src_leaf": "leaf2",
                            "dst_leaf": "leaf5",
                            "value": 12.0,
                            "severity": "high",
                            "persistence": "persistent",
                        }
                    ]
                }
            }
        },
        metadata={},
    )

    pairs = select_probe_pairs(context, family="packet_loss", max_pairs=2)

    assert pairs == [
        ProbePair(
            source="client3",
            destination="192.168.105.2",
            destination_name="client9",
            source_leaf="leaf2",
            destination_leaf="leaf5",
            metadata={
                "selection": "anomaly",
                "control": False,
                "anomaly_type": "packet_loss",
                "observed_value": 12.0,
                "baseline": None,
                "threshold": None,
            },
        )
    ]


def test_pair_selection_uses_structured_evidence_when_raw_anomaly_block_is_absent():
    context = SimpleNamespace(symptoms={"observations": {}}, metadata={})
    evidence = Evidence(
        evidence_id="E-live-loss",
        entity_type="path",
        entity_id="client7--client21",
        category="packet_loss_rate",
        value=0.25,
        source="pingmesh_episode",
        timestamp=datetime.now(UTC),
        origin=EvidenceOrigin.PUBLIC_OBSERVATION,
        metadata={
            "src_name": "client7",
            "dst_name": "client21",
            "dst_ip": "192.0.2.21",
            "src_leaf": "leaf2",
            "dst_leaf": "leaf6",
        },
    )

    pairs = select_probe_pairs(context, family="packet_loss", max_pairs=1, evidence=[evidence])

    assert [(item.source, item.destination) for item in pairs] == [("client7", "192.0.2.21")]
    assert pairs[0].metadata["evidence_id"] == "E-live-loss"


def test_loss_matrix_confirms_anomaly_then_covers_links_breadth_first(monkeypatch):
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
                            "type": "packet_loss",
                            "src_name": "client1",
                            "dst_name": "client2",
                            "dst_ip": "192.0.2.2",
                            "src_leaf": "leaf1",
                            "dst_leaf": "leaf2",
                            "value": 20.0,
                            "severity": "high",
                            "persistence": "persistent",
                        },
                        {
                            "type": "packet_loss",
                            "src_name": "client3",
                            "dst_name": "client2",
                            "dst_ip": "192.0.2.2",
                            "src_leaf": "leaf3",
                            "dst_leaf": "leaf2",
                            "value": 15.0,
                        },
                    ]
                }
            }
        }
    )

    pairs = select_probe_pairs(
        context,
        family="packet_loss",
        max_pairs=4,
        anomaly_pairs=1,
        control_pairs=1,
        link_isolation=True,
        packets_per_round=10,
        repeat_rounds=2,
        control_repeat_rounds=1,
    )

    assert [pair.metadata["selection"] for pair in pairs] == [
        "anomaly",
        "healthy_control",
        "link_isolation",
        "link_isolation",
    ]
    assert {pair.source for pair in pairs[2:]} == {"spine1", "spine2"}
    assert all(pair.metadata["source_interface"] == "eth2" for pair in pairs[2:])
    assert {pair.metadata["target_interface"] for pair in pairs[2:]} == {"eth1", "eth2"}
    assert all(pair.destination == "leaf2" and pair.metadata["link_probe"] for pair in pairs[2:])
    assert [pair.metadata["packets_per_round"] for pair in pairs] == [10, 10, 20, 20]
    assert [pair.metadata["repeat_rounds"] for pair in pairs] == [2, 1, 1, 1]

    tools = PingTools()
    outcome = asyncio.run(
        RepeatedPacketLossProbe().run(
            SimpleNamespace(tools=tools),
            pairs=pairs,
            budget=ProbeBudget(BudgetConfig(max_extra_tool_calls_per_hard_case=12)),
        )
    )

    assert outcome.tool_calls == 5
    assert outcome.probe_packets == 70
    assert [call["count"] for call in tools.calls] == [10, 10, 10, 20, 20]
    assert all(call["source_interface"] == "eth2" for call in tools.calls[-2:])
    assert all(call["target_device"] == "leaf2" for call in tools.calls[-2:])
    assert outcome.metadata == {
        "raw_ping_observations": 5,
        "pairs_completed": 4,
        "anomaly_pairs": 1,
        "control_pairs": 1,
        "link_isolation_pairs": 2,
    }


def test_ambiguous_endpoint_concentration_round_robins_isolation_across_two_leafs(monkeypatch):
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
        device("client1", "client", data_ip="192.0.2.1", attached_switch="leaf1"),
        device("client2", "client", data_ip="192.0.2.2", attached_switch="leaf2"),
    ]
    by_name = {item.name: item for item in devices}
    manifest = SimpleNamespace(
        clients=lambda: [item for item in devices if item.role.value == "client"],
        device=by_name.get,
        links=[
            SimpleNamespace(
                endpoints=(
                    SimpleNamespace(device=spine, interface=f"eth-{leaf}"),
                    SimpleNamespace(device=leaf, interface=f"eth-{spine}"),
                )
            )
            for leaf in ("leaf1", "leaf2")
            for spine in ("spine1", "spine2")
        ],
    )
    monkeypatch.setattr(probe_base, "_load_context_manifest", lambda _context: manifest)
    context = SimpleNamespace(
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
                        }
                    ]
                }
            }
        }
    )

    pairs = select_probe_pairs(
        context,
        family="packet_loss",
        max_pairs=4,
        anomaly_pairs=0,
        control_pairs=0,
        link_isolation=True,
        packets_per_round=10,
        repeat_rounds=2,
    )

    assert [pair.destination for pair in pairs] == ["leaf1", "leaf2", "leaf1", "leaf2"]
    assert all(pair.metadata["candidate_leafs"] == ("leaf1", "leaf2") for pair in pairs)


def test_single_link_loss_uses_two_bounded_rounds_and_link_tool():
    tools = PingTools(received_by_call=[15, 14])
    pair = ProbePair(
        "spine1",
        "leaf2",
        metadata={
            "selection": "link_isolation",
            "link_probe": True,
            "source_interface": "eth2",
            "target_device": "leaf2",
            "target_interface": "eth1",
            "packets_per_round": 20,
            "repeat_rounds": 2,
        },
    )

    outcome = asyncio.run(
        RepeatedPacketLossProbe().run(
            SimpleNamespace(tools=tools),
            pairs=[pair],
            budget=ProbeBudget(BudgetConfig()),
        )
    )

    assert [call["count"] for call in tools.calls] == [20, 20]
    assert outcome.probe_packets == 40
    assert outcome.observations[0].rounds == 2
    assert outcome.observations[0].loss_rate == 0.275
    evidence = outcome.evidence[0]
    assert evidence.metadata["link_probe"] is True
    assert evidence.metadata["target_interface"] == "eth1"
