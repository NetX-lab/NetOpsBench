import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

from examples.agents.diagnostic_harness.config import BudgetConfig, MTUProbeConfig
from examples.agents.diagnostic_harness.models import Evidence, ProbePair, RankedInterfaceCandidate
from examples.agents.diagnostic_harness.probes import MTULinkSweepProbe, MTUPacketSizeSweepProbe, ProbeBudget
from netopsbench.platform.toolkit._core.common import ToolResult


class MTUTools:
    def __init__(self, threshold=1400, fail_size=None):
        self.threshold = threshold
        self.fail_size = fail_size
        self.calls = []

    def ping_test(self, **arguments):
        self.calls.append(arguments)
        size = arguments["payload_size"]
        if size == self.fail_size:
            return ToolResult(success=False, data=None, error="Ping timed out")
        count = arguments["count"]
        success = size < self.threshold
        if success:
            output = (
                f"{count} packets transmitted, {count} received, 0% packet loss\n"
                "rtt min/avg/max/mdev = 0.4/0.5/0.6/0.1 ms\n"
            )
        else:
            output = "ping: local error: message too long, mtu=1400\n"
        return ToolResult(
            success=True,
            data={
                "source": arguments["src"],
                "destination": arguments["dst_ip"],
                "count": count,
                "payload_size": size,
                "dont_fragment": arguments["dont_fragment"],
                "output": output,
                "return_code": 0 if success else 1,
            },
        )


def test_mtu_sweep_finds_stable_payload_threshold_using_df_ping():
    tools = MTUTools(threshold=1400)
    config = MTUProbeConfig(payload_sizes=(64, 512, 1200, 1372, 1400, 1472), packets_per_size=2)
    outcome = asyncio.run(
        MTUPacketSizeSweepProbe(config).run(
            SimpleNamespace(tools=tools),
            pairs=[ProbePair("client1", "192.0.2.2", source_leaf="leaf1", destination_leaf="leaf5")],
            budget=ProbeBudget(BudgetConfig()),
        )
    )

    assert outcome.status == "completed"
    assert outcome.tool_calls == 6
    assert outcome.probe_packets == 12
    assert all(call["dont_fragment"] is True for call in tools.calls)
    assert [call["payload_size"] for call in tools.calls] == [64, 512, 1200, 1372, 1400, 1472]
    result = outcome.observations[0]
    assert result.largest_successful_size == 1372
    assert result.smallest_failed_size == 1400
    assert result.size_dependent_failure
    threshold = next(item for item in outcome.evidence if item.category == "packet_size_threshold")
    assert threshold.value["size_dependent_failure"]
    assert threshold.metadata["size_semantics"] == "icmp_payload_bytes"
    assert all(item.ip_packet_size == item.payload_size + 28 for item in result.observations)


def test_mtu_sweep_preserves_tool_error_as_non_supporting_evidence():
    tools = MTUTools(threshold=1400, fail_size=1200)
    config = MTUProbeConfig(payload_sizes=(64, 1200, 1400), packets_per_size=1)
    outcome = asyncio.run(
        MTUPacketSizeSweepProbe(config).run(
            SimpleNamespace(tools=tools),
            pairs=[ProbePair("client1", "192.0.2.2")],
            budget=ProbeBudget(BudgetConfig()),
        )
    )

    assert outcome.status == "partial"
    error = next(item for item in outcome.evidence if item.category == "tool_error")
    assert error.reliability == 0
    assert "timed out" in error.value["error"]


def test_mtu_sweep_stops_at_tool_budget():
    tools = MTUTools()
    config = MTUProbeConfig(payload_sizes=(64, 512, 1200, 1372), packets_per_size=2)
    outcome = asyncio.run(
        MTUPacketSizeSweepProbe(config).run(
            SimpleNamespace(tools=tools),
            pairs=[ProbePair("client1", "192.0.2.2")],
            budget=ProbeBudget(BudgetConfig(max_extra_tool_calls_per_hard_case=2)),
        )
    )

    assert len(tools.calls) == 2
    assert outcome.status == "partial"
    assert any(item.category == "tool_error" for item in outcome.evidence)
    assert outcome.observations[0].smallest_failed_size is None
    assert not outcome.observations[0].size_dependent_failure


def test_all_sizes_succeed_is_not_reported_as_mtu_mismatch():
    tools = MTUTools(threshold=10000)
    config = MTUProbeConfig(payload_sizes=(64, 1372, 1472, 8972), packets_per_size=1)
    outcome = asyncio.run(
        MTUPacketSizeSweepProbe(config).run(
            SimpleNamespace(tools=tools),
            pairs=[ProbePair("client1", "192.0.2.2")],
            budget=ProbeBudget(BudgetConfig()),
        )
    )

    result = outcome.observations[0]
    assert result.largest_successful_size == 8972
    assert result.smallest_failed_size is None
    assert not result.size_dependent_failure


def test_exact_link_mtu_sweep_uses_lower_endpoint_and_df():
    class Tools:
        def __init__(self):
            self.calls = []

        def ping_link_test(self, **arguments):
            self.calls.append(arguments)
            count = arguments["count"]
            success = arguments["payload_size"] + 28 <= 1400
            received = count if success else 0
            return ToolResult(
                success=True,
                data={
                    "source": arguments["src"],
                    "destination": "192.0.2.2",
                    "count": count,
                    "payload_size": arguments["payload_size"],
                    "dont_fragment": arguments["dont_fragment"],
                    "output": f"{count} packets transmitted, {received} received, 0% packet loss\n",
                    "return_code": 0 if success else 1,
                },
            )

    candidate = RankedInterfaceCandidate(
        link_id="spine1:Ethernet0--leaf1:Ethernet0",
        primary_device="spine1",
        primary_interface="Ethernet0",
        peer_device="leaf1",
        peer_interface="Ethernet0",
        score=0.4,
    )
    difference = Evidence(
        evidence_id="peer-mtu-difference",
        entity_type="interface",
        entity_id="leaf1:Ethernet0",
        category="configuration_difference",
        value={"different": True, "field": "mtu", "local_mtu": 1400, "peer_mtu": 9100},
        source="get_device_interfaces",
        timestamp=datetime.now(UTC),
        metadata={"link_id": candidate.link_id},
    )
    tools = Tools()
    outcome = asyncio.run(
        MTULinkSweepProbe(MTUProbeConfig(packets_per_size=2)).run(
            SimpleNamespace(tools=tools),
            candidates=[candidate],
            evidence=[difference],
            budget=ProbeBudget(BudgetConfig()),
        )
    )

    assert outcome.status == "completed"
    assert [item["src"] for item in tools.calls] == ["leaf1", "leaf1"]
    assert [item["payload_size"] for item in tools.calls] == [64, 1373]
    assert all(item["dont_fragment"] is True for item in tools.calls)
    threshold = next(item for item in outcome.evidence if item.category == "packet_size_threshold")
    assert threshold.value["size_dependent_failure"] is True
    assert threshold.covered_links == (candidate.link_id,)
    assert threshold.metadata["fault_endpoint_device"] == "leaf1"


def test_exact_link_mtu_sweep_fails_closed_on_tool_error():
    class Tools:
        def ping_link_test(self, **_arguments):
            return ToolResult(success=False, data=None, error="link ping timed out")

    candidate = RankedInterfaceCandidate(
        link_id="spine1:Ethernet0--leaf1:Ethernet0",
        primary_device="spine1",
        primary_interface="Ethernet0",
        peer_device="leaf1",
        peer_interface="Ethernet0",
        score=0.4,
    )
    difference = Evidence(
        evidence_id="peer-mtu-difference",
        entity_type="interface",
        entity_id="leaf1:Ethernet0",
        category="configuration_difference",
        value={"different": True, "field": "mtu", "local_mtu": 1400, "peer_mtu": 9100},
        source="get_device_interfaces",
        timestamp=datetime.now(UTC),
        metadata={"link_id": candidate.link_id},
    )
    outcome = asyncio.run(
        MTULinkSweepProbe().run(
            SimpleNamespace(tools=Tools()),
            candidates=[candidate],
            evidence=[difference],
            budget=ProbeBudget(BudgetConfig()),
        )
    )

    assert outcome.status == "failed"
    assert not any(item.category == "packet_size_threshold" for item in outcome.evidence)
    assert all(item.reliability == 0 for item in outcome.evidence)
