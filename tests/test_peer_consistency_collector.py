import asyncio
from datetime import UTC, datetime

from examples.agents.diagnostic_harness.config import BudgetConfig
from examples.agents.diagnostic_harness.evidence.cache import TTLToolCache
from examples.agents.diagnostic_harness.models import Evidence, RankedInterfaceCandidate
from examples.agents.diagnostic_harness.probes.base import ProbeBudget
from examples.agents.diagnostic_harness.topology.peer_consistency import PeerConsistencyCollector
from netopsbench.platform.toolkit._core.common import ToolResult


class Tools:
    def __init__(self):
        self.calls = []

    def get_device_interfaces(self, *, device):
        self.calls.append(device)
        mtu = 1400 if device == "spine1" else 9100
        return ToolResult(
            success=True,
            data={
                "interfaces": [
                    {"name": "Ethernet0", "admin": "up", "oper": "up", "mtu": mtu},
                ]
            },
        )


class Context:
    def __init__(self):
        self.tools = Tools()


def _candidate():
    return RankedInterfaceCandidate(
        link_id="spine1:Ethernet0--leaf1:Ethernet0",
        primary_device="spine1",
        primary_interface="Ethernet0",
        peer_device="leaf1",
        peer_interface="Ethernet0",
        score=0.8,
    )


def test_peer_collector_finds_real_mtu_difference_and_uses_cache():
    context = Context()
    budget = ProbeBudget(BudgetConfig())
    cache = TTLToolCache()
    collector = PeerConsistencyCollector()

    first = asyncio.run(collector.collect(context, candidates=[_candidate()], budget=budget, cache=cache))
    second = asyncio.run(collector.collect(context, candidates=[_candidate()], budget=budget, cache=cache))

    differences = [item for item in first if item.category == "configuration_difference"]
    assert len(differences) == 1
    assert differences[0].entity_id == "spine1:Ethernet0"
    assert differences[0].value == {"different": True, "field": "mtu", "local_mtu": 1400, "peer_mtu": 9100}
    assert len(second) == 5
    assert context.tools.calls == ["spine1", "leaf1"]
    assert budget.tool_calls == 2
    assert cache.stats == {"hits": 2, "misses": 2, "size": 2}


def test_peer_collector_records_tool_error_without_treating_it_as_difference():
    class FailingTools:
        def get_device_interfaces(self, *, device):
            return ToolResult(success=False, error=f"{device} timeout")

    context = Context()
    context.tools = FailingTools()
    evidence = asyncio.run(
        PeerConsistencyCollector().collect(
            context,
            candidates=[_candidate()],
            budget=ProbeBudget(BudgetConfig()),
            cache=TTLToolCache(),
        )
    )

    assert evidence
    assert {item.category for item in evidence} == {"tool_error"}
    assert all(item.reliability == 0.0 for item in evidence)


def test_peer_collector_compares_canonical_client_access_interface():
    class AccessTools:
        def get_device_interfaces(self, *, device):
            if device == "leaf1":
                interfaces = [{"name": "Ethernet16", "admin": "up", "oper": "up", "mtu": 1400}]
            else:
                # The toolkit parser normalizes the transient ``@if`` suffix
                # before the collector receives this structured payload.
                interfaces = [{"name": "eth1", "mtu": 1500}]
            return ToolResult(success=True, data={"interfaces": interfaces})

    candidate = RankedInterfaceCandidate(
        link_id="leaf1:Ethernet16--client1:eth1",
        primary_device="leaf1",
        primary_interface="Ethernet16",
        peer_device="client1",
        peer_interface="eth1",
        score=0.5,
        layer="access",
    )
    context = Context()
    context.tools = AccessTools()

    evidence = asyncio.run(
        PeerConsistencyCollector().collect(
            context,
            candidates=[candidate],
            budget=ProbeBudget(BudgetConfig()),
            cache=TTLToolCache(),
        )
    )

    difference = next(item for item in evidence if item.category == "configuration_difference")
    assert difference.entity_id == "leaf1:Ethernet16"
    assert difference.value == {"different": True, "field": "mtu", "local_mtu": 1400, "peer_mtu": 1500}


def test_peer_collector_ignores_role_normal_jumbo_access_delta():
    class JumboAccessTools:
        def get_device_interfaces(self, *, device):
            mtu = 9100 if device == "leaf1" else 9232
            name = "Ethernet16" if device == "leaf1" else "eth1"
            return ToolResult(
                success=True, data={"interfaces": [{"name": name, "admin": "up", "oper": "up", "mtu": mtu}]}
            )

    candidate = RankedInterfaceCandidate(
        link_id="leaf1:Ethernet16--client1:eth1",
        primary_device="leaf1",
        primary_interface="Ethernet16",
        peer_device="client1",
        peer_interface="eth1",
        score=0.7,
        layer="access",
    )
    context = Context()
    context.tools = JumboAccessTools()

    evidence = asyncio.run(
        PeerConsistencyCollector().collect(
            context,
            candidates=[candidate],
            budget=ProbeBudget(BudgetConfig()),
            cache=TTLToolCache(),
        )
    )

    assert "configuration_difference" not in {item.category for item in evidence}


def test_peer_collector_keeps_only_mtu_candidate_compatible_with_measured_cutoff():
    class CandidateTools:
        def get_device_interfaces(self, *, device):
            rows = {
                "spine1": [{"name": "Ethernet0", "admin": "up", "oper": "up", "mtu": 1400}],
                "leaf1": [
                    {"name": "Ethernet0", "admin": "up", "oper": "up", "mtu": 9100},
                    {"name": "Ethernet16", "admin": "up", "oper": "up", "mtu": 9100},
                ],
                "client1": [{"name": "eth1", "admin": "up", "oper": "up", "mtu": 9232}],
            }
            return ToolResult(success=True, data={"interfaces": rows[device]})

    fabric = _candidate()
    normal_access = RankedInterfaceCandidate(
        link_id="leaf1:Ethernet16--client1:eth1",
        primary_device="leaf1",
        primary_interface="Ethernet16",
        peer_device="client1",
        peer_interface="eth1",
        score=0.9,
        layer="access",
    )
    threshold = Evidence(
        evidence_id="E-size-threshold",
        entity_type="path",
        entity_id="client1--client9",
        category="packet_size_threshold",
        value={
            "largest_successful_payload_size": 1372,
            "smallest_failed_payload_size": 1472,
            "size_dependent_failure": True,
        },
        source="mtu_sweep",
        timestamp=datetime.now(UTC),
    )
    context = Context()
    context.tools = CandidateTools()

    evidence = asyncio.run(
        PeerConsistencyCollector().collect(
            context,
            candidates=[normal_access, fabric],
            budget=ProbeBudget(BudgetConfig()),
            cache=TTLToolCache(),
            evidence=[threshold],
        )
    )
    differences = [item for item in evidence if item.category == "configuration_difference"]

    assert [item.entity_id for item in differences] == ["spine1:Ethernet0"]
    assert differences[0].metadata["threshold_consistent"] is True
