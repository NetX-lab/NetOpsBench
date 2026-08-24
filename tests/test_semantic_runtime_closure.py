import asyncio
from datetime import UTC, datetime

from examples.agents.diagnostic_harness import DiagnosticHarness, HarnessConfig
from examples.agents.diagnostic_harness import semantic_closure as semantic_closure_module
from examples.agents.diagnostic_harness.config import BudgetConfig
from examples.agents.diagnostic_harness.evidence.semantic import (
    evidence_from_diagnosis,
    extract_semantic_family_hints,
)
from examples.agents.diagnostic_harness.models import Evidence, EvidenceOrigin
from examples.agents.diagnostic_harness.normalization.interface import TopologyIndex
from examples.agents.diagnostic_harness.probes.base import ProbeBudget
from examples.agents.diagnostic_harness.semantic_closure import (
    SemanticRuntimeClosure,
    _concentrated_route_queries,
    _connected_prefix_for_target,
    _rank_route_policy_prefixes,
    _route_payload_proves_owner,
    _route_policy_configuration_difference,
    _selected_discard_route,
    _selected_static_route,
)
from netopsbench.agents.base import DiagnosticContext
from netopsbench.platform.toolkit._core.common import ToolResult

from .diagnostic_harness_helpers import BaseAgent, diagnosis_result, write_two_leaf_manifest


def test_route_policy_live_config_requires_prefix_level_difference():
    normal = "router bgp 65001\n network 192.0.2.0/30 route-map RM-ALLOW\nroute-map RM-ALLOW permit 10\n"
    denied = "ip prefix-list BLOCK seq 10 deny 192.0.2.0/30\nroute-map RM-FILTER permit 10\n"
    mapped_deny = (
        "ip prefix-list BLOCK seq 10 permit 192.0.2.0/30\n"
        "route-map RM-FILTER deny 10\n"
        " match ip address prefix-list BLOCK\n"
        "route-map RM-FILTER permit 20\n"
    )

    assert _route_policy_configuration_difference(normal, None) is None
    assert _route_policy_configuration_difference(normal, "192.0.2.0/30") is None
    assert _route_policy_configuration_difference(normal, "192.0.2.4/30") == "missing_prefix_configuration"
    assert _route_policy_configuration_difference(denied, "192.0.2.0/30") == "explicit_prefix_deny"
    assert _route_policy_configuration_difference(mapped_deny, "192.0.2.0/30") == "explicit_prefix_deny"


def test_route_policy_prefix_comparison_uses_address_scope_not_literal_mask():
    normal = "router bgp 65001\n network 192.0.2.0/30 route-map RM-ALLOW\nroute-map RM-ALLOW permit 10\n"
    denied = "ip prefix-list BLOCK seq 10 deny 192.0.2.0/30\nroute-map RM-FILTER permit 10\n"

    assert _route_policy_configuration_difference(normal, "192.0.2.2") is None
    assert _route_policy_configuration_difference(normal, "192.0.2.2/32") is None
    assert _route_policy_configuration_difference(normal, "192.0.2.0/24") is None
    assert _route_policy_configuration_difference(denied, "192.0.2.2") == "explicit_prefix_deny"


def test_route_policy_hint_binds_missing_prefix_not_nearby_present_prefix():
    result = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "route_policy_misconfig",
            "device": "leaf1",
            "confidence": 0.95,
            "evidence": ["leaf1 advertises only network 192.168.101.4/30; network 192.168.101.0/30 is missing"],
        }
    )

    hints = extract_semantic_family_hints(result)

    assert len(hints) == 1
    assert hints[0].family == "route_policy"
    assert hints[0].prefix == "192.168.101.0/30"


def test_route_policy_hint_parses_network_statement_for_prefix_clause():
    result = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "route_policy_misconfig",
            "device": "leaf1",
            "confidence": 0.95,
            "evidence": [
                "leaf1 BGP config only advertises 'network 192.168.101.4/30 route-map RM-ALLOW'; "
                "the network statement for 192.168.101.0/30 is missing"
            ],
        }
    )

    hints = extract_semantic_family_hints(result)

    assert len(hints) == 1
    assert hints[0].family == "route_policy"
    assert hints[0].prefix == "192.168.101.0/30"


def test_route_policy_prefix_ranking_resists_many_unrelated_rib_misses(tmp_path):
    context = _context(tmp_path=tmp_path)
    direct = Evidence(
        evidence_id="direct-policy-hint",
        entity_type="device",
        entity_id="leaf5",
        category="configuration_difference",
        value={"prefix": "192.0.2.2/32", "semantic_family": "route_policy"},
        source="base_diagnosis_text",
        timestamp=datetime.now(UTC),
        reliability=0.75,
        origin=EvidenceOrigin.BASE_CLAIM,
        supports_submission=False,
        metadata={
            "prefix": "192.0.2.2/32",
            "semantic_family": "route_policy",
            "direct_configuration_evidence": True,
        },
    )
    consequences = [
        Evidence(
            evidence_id=f"rib-miss-{index}",
            entity_type="device",
            entity_id=f"leaf{index}",
            category="route_presence",
            value={"prefix": f"198.51.{index}.0/24", "present": False},
            source="get_bgp_rib",
            timestamp=datetime.now(UTC),
            origin=EvidenceOrigin.LIVE_TELEMETRY,
            supports_submission=False,
            metadata={"prefix": f"198.51.{index}.0/24", "semantic_family": "route_policy"},
        )
        for index in range(1, 33)
    ]

    ranked = _rank_route_policy_prefixes(
        context,
        [*consequences, direct],
        preferred_owner="leaf5",
    )

    assert ranked[0] == "192.0.2.2/32"


def test_route_policy_closure_verifies_specific_config_hint_before_rib_noise(tmp_path):
    class Tools:
        def __init__(self):
            self.route_targets = []

        def get_route_table(self, **arguments):
            self.route_targets.append(arguments["prefix"])
            return ToolResult(success=True, data={"routes": []})

        def get_device_config(self, **arguments):
            config = (
                "interface Ethernet20\n ip address 192.0.2.2/32\nrouter bgp 65005\n route-map RM-ALLOW permit 10\n"
                if arguments["device"] == "leaf5"
                else "router bgp 65001\n route-map RM-ALLOW permit 10\n"
            )
            return ToolResult(success=True, data={"config": config})

    initial = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "route_policy_misconfig",
            "device": "leaf5",
            "confidence": 0.8,
            "evidence": ["leaf5 route-map is missing network 192.0.2.2/32"],
        }
    )
    context = _context(tmp_path=tmp_path)
    context.tools = Tools()
    noise = Evidence(
        evidence_id="unrelated-rib-miss",
        entity_type="device",
        entity_id="leaf1",
        category="route_presence",
        value={"prefix": "198.51.100.2/32", "present": False},
        source="get_bgp_rib",
        timestamp=datetime.now(UTC),
        origin=EvidenceOrigin.LIVE_TELEMETRY,
        supports_submission=False,
        metadata={"prefix": "198.51.100.2/32", "semantic_family": "route_policy"},
    )

    outcome = asyncio.run(
        SemanticRuntimeClosure().inspect(
            context,
            base_result=initial,
            topology=TopologyIndex.from_context(context),
            evidence=[noise, *evidence_from_diagnosis(initial)],
            budget=ProbeBudget(BudgetConfig()),
            requested_family="route_policy",
        )
    )

    assert outcome.result is not None
    assert outcome.result.findings["fault_type"] == "route_policy_misconfig"
    assert outcome.result.findings["location"]["device"] == "leaf5"
    assert context.tools.route_targets[0] == "192.0.2.2"


def test_connected_interface_scope_resolves_maskless_network_hint():
    config = "interface Ethernet20\n ip address 192.168.208.1/30\nrouter bgp 65001\n"

    assert _connected_prefix_for_target(config, "192.168.208.0") == "192.168.208.0/30"
    assert _connected_prefix_for_target(config, "192.168.208.2") == "192.168.208.0/30"
    assert _connected_prefix_for_target(config, "192.168.209.0") is None


def test_live_rib_owner_resolution_accepts_only_direct_routes():
    connected = {"routes": [{"prefix": "192.0.2.0/30", "protocol": "connected", "selected": True}]}
    learned = {"routes": [{"prefix": "192.0.2.0/30", "protocol": "bgp", "selected": True}]}

    assert _route_payload_proves_owner(connected, "192.0.2.2") is True
    assert _route_payload_proves_owner(learned, "192.0.2.2") is False


def test_route_selection_uses_most_specific_covering_prefix():
    payload = {
        "routes": [
            {"prefix": "0.0.0.0/0", "protocol": "static", "selected": True, "is_discard": False},
            {
                "prefix": "192.0.2.0/30",
                "protocol": "static",
                "selected": True,
                "is_discard": True,
            },
        ]
    }

    assert _selected_static_route(payload, prefix="192.0.2.2")["prefix"] == "192.0.2.0/30"
    assert _selected_discard_route(payload, destination="192.0.2.2")["prefix"] == "192.0.2.0/30"
    assert _selected_discard_route(payload, destination="198.51.100.2") is None


def test_route_policy_missing_prefix_ignores_non_owner_leaf(tmp_path):
    class Tools:
        def __init__(self):
            self.config_calls = []

        def get_route_table(self, **arguments):
            route_count = 1 if arguments["device"] == "leaf5" else 0
            return ToolResult(success=True, data={"routes": [{}] * route_count})

        def get_device_config(self, **arguments):
            self.config_calls.append(arguments["device"])
            return ToolResult(
                success=True,
                data={"config": "router bgp 65001\nroute-map EXPORT permit 10\n"},
            )

    initial = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "route_policy_misconfig",
            "device": "leaf5",
            "confidence": 0.95,
            "evidence": ["leaf5 route-map is missing network 192.0.2.0/30"],
        }
    )
    context = _context(tmp_path=tmp_path)
    context.tools = Tools()

    outcome = asyncio.run(
        SemanticRuntimeClosure().inspect(
            context,
            base_result=initial,
            topology=TopologyIndex.from_context(context),
            evidence=evidence_from_diagnosis(initial),
            budget=ProbeBudget(BudgetConfig()),
            requested_family="route_policy",
        )
    )
    result = outcome.result

    assert result is not None
    assert result.verdict == "fault_detected"
    assert result.findings["fault_type"] == "route_policy_misconfig"
    assert result.findings["location"]["device"] == "leaf5"
    assert context.tools.config_calls == ["leaf5"]


def test_route_policy_maskless_hint_requires_connected_owner_scope(tmp_path):
    class Tools:
        def __init__(self):
            self.calls = []

        def get_route_table(self, **arguments):
            self.calls.append(("route", arguments["device"], arguments["prefix"]))
            return ToolResult(
                success=True,
                data={
                    "routes": [
                        {
                            "prefix": "192.168.208.0/30",
                            "protocol": "connected",
                            "selected": True,
                            "nexthops": [],
                        }
                    ]
                },
            )

        def get_device_config(self, **arguments):
            self.calls.append(("config", arguments["device"]))
            config = (
                "interface Ethernet20\n ip address 192.168.208.1/30\nrouter bgp 65005\n route-map RM-ALLOW permit 10\n"
                if arguments["device"] == "leaf5"
                else "router bgp 65001\n route-map RM-ALLOW permit 10\n"
            )
            return ToolResult(success=True, data={"config": config})

    initial = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "route_policy_misconfig",
            "device": "leaf5",
            "confidence": 0.6,
            "evidence": ["BGP configuration on leaf5 is missing network 192.168.208.0"],
        }
    )
    context = _context(tmp_path=tmp_path)
    context.tools = Tools()

    outcome = asyncio.run(
        SemanticRuntimeClosure().inspect(
            context,
            base_result=initial,
            topology=TopologyIndex.from_context(context),
            evidence=evidence_from_diagnosis(initial),
            budget=ProbeBudget(BudgetConfig()),
            requested_family="route_policy",
        )
    )

    assert outcome.result is not None
    assert outcome.result.findings["location"]["device"] == "leaf5"
    config = next(item for item in outcome.outcome.evidence if item.category == "configuration_difference")
    assert config.value["prefix"] == "192.168.208.0/30"


def test_route_policy_route_visibility_on_transit_device_is_not_prefix_ownership(tmp_path):
    class Tools:
        def get_route_table(self, **_arguments):
            return ToolResult(
                success=True,
                data={"routes": [{"prefix": "192.168.208.0/30", "protocol": "bgp", "selected": True, "nexthops": []}]},
            )

        def get_device_config(self, **_arguments):
            return ToolResult(
                success=True,
                data={"config": "router bgp 65000\n route-map RM-ALLOW permit 10\n"},
            )

    initial = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "route_policy_misconfig",
            "device": "spine1",
            "confidence": 0.6,
            "evidence": ["BGP configuration on spine1 is missing network 192.168.208.0"],
        }
    )
    context = _context(tmp_path=tmp_path)
    context.tools = Tools()

    outcome = asyncio.run(
        SemanticRuntimeClosure().inspect(
            context,
            base_result=initial,
            topology=TopologyIndex.from_context(context),
            evidence=evidence_from_diagnosis(initial),
            budget=ProbeBudget(BudgetConfig()),
            requested_family="route_policy",
        )
    )

    assert outcome.result is None
    assert not any(item.category == "configuration_difference" for item in outcome.outcome.evidence)


def test_base_static_claim_does_not_suppress_live_route_collection(tmp_path):
    class Tools:
        def __init__(self):
            self.calls = []

        def get_device_config(self, **arguments):
            self.calls.append(("config", arguments["device"]))
            config = "ip route 192.0.2.9/32 192.0.2.10\n" if arguments["device"] == "leaf5" else ""
            return ToolResult(success=True, data={"config": config})

        def get_route_table(self, **arguments):
            self.calls.append(("route", arguments["device"]))
            return ToolResult(
                success=True,
                data={
                    "routes": [
                        {
                            "prefix": arguments["prefix"],
                            "protocol": "static",
                            "nexthops": [],
                            "selected": True,
                            "is_discard": False,
                        }
                    ]
                },
            )

    initial = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "static_route_misconfig",
            "device": "leaf5",
            "confidence": 0.95,
            "evidence": [
                "leaf5 config contains ip route 192.0.2.9/32 192.0.2.10",
                "leaf5 route table selects the unresolved static route",
            ],
        }
    )
    context = _context(tmp_path=tmp_path)
    context.tools = Tools()

    outcome = asyncio.run(
        SemanticRuntimeClosure().inspect(
            context,
            base_result=initial,
            topology=TopologyIndex.from_context(context),
            evidence=evidence_from_diagnosis(initial),
            budget=ProbeBudget(BudgetConfig()),
            requested_family="static_route",
        )
    )
    result = outcome.result

    assert result is not None
    assert result.verdict == "fault_detected"
    assert result.findings["fault_type"] == "static_route_misconfig"
    assert ("config", "leaf5") in context.tools.calls
    assert ("route", "leaf5") in context.tools.calls
    live = [item for item in outcome.outcome.evidence if item.source in {"get_device_config", "get_route_table"}]
    assert live
    assert all(item.origin.value != "base_claim" for item in live)


def test_planning_only_base_tool_route_targets_live_two_source_verification(tmp_path):
    class Tools:
        def __init__(self):
            self.calls = []

        def get_device_config(self, **arguments):
            self.calls.append(("config", arguments["device"]))
            return ToolResult(
                success=True,
                data={"config": "ip route 192.0.2.9/32 192.0.2.10\n"},
            )

        def get_route_table(self, **arguments):
            self.calls.append(("route", arguments["device"], arguments["prefix"]))
            return ToolResult(
                success=True,
                data={
                    "routes": [
                        {
                            "prefix": arguments["prefix"],
                            "protocol": "static",
                            "selected": True,
                            "nexthops": [],
                        }
                    ]
                },
            )

    initial = diagnosis_result(
        {
            "verdict": "inconclusive",
            "fault_type": None,
            "device": None,
            "interface": None,
            "confidence": 0.0,
            "reasoning": "Base diagnosis was incomplete.",
        }
    )
    planning = Evidence(
        evidence_id="base-tool-config-static",
        entity_type="device",
        entity_id="leaf5",
        category="configured_static_route",
        value={"prefix": "192.0.2.9/32", "next_hop": "192.0.2.10"},
        source="base_tool:get_device_config",
        timestamp=datetime.now(UTC),
        origin=EvidenceOrigin.CONFIG_READ,
        supports_submission=False,
        metadata={"semantic_family": "static_route", "base_tool_observation": True},
    )
    context = _context(tmp_path=tmp_path)
    tools = Tools()
    context.tools = tools

    outcome = asyncio.run(
        SemanticRuntimeClosure().inspect(
            context,
            base_result=initial,
            topology=TopologyIndex.from_context(context),
            evidence=[planning],
            budget=ProbeBudget(BudgetConfig()),
            requested_family="static_route",
        )
    )

    assert outcome.result is not None
    assert outcome.result.findings["fault_type"] == "static_route_misconfig"
    assert outcome.result.findings["location"]["device"] == "leaf5"
    assert tools.calls == [("config", "leaf5"), ("route", "leaf5", "192.0.2.9/32")]
    assert all(item.source in {"get_device_config", "get_route_table"} for item in outcome.outcome.evidence)


def test_configured_static_route_category_is_a_planning_seed_without_redundant_metadata(tmp_path):
    class Tools:
        def __init__(self):
            self.calls = []

        def get_device_config(self, **arguments):
            self.calls.append(("config", arguments["device"]))
            return ToolResult(success=True, data={"config": "ip route 192.0.2.9/32 192.0.2.10\n"})

        def get_route_table(self, **arguments):
            self.calls.append(("route", arguments["device"], arguments.get("prefix")))
            return ToolResult(
                success=True,
                data={
                    "routes": [
                        {
                            "prefix": "192.0.2.9/32",
                            "protocol": "static",
                            "selected": True,
                            "nexthops": [],
                        }
                    ]
                },
            )

    initial = diagnosis_result(
        {"verdict": "inconclusive", "fault_type": None, "device": None, "interface": None, "confidence": 0.0}
    )
    planning = Evidence(
        "config-seed",
        "device",
        "leaf5",
        "configured_static_route",
        {"prefix": "192.0.2.9/32", "next_hop": "192.0.2.10"},
        "base_tool:get_device_config",
        datetime.now(UTC),
        origin=EvidenceOrigin.CONFIG_READ,
        supports_submission=False,
    )
    context = _context(tmp_path=tmp_path)
    tools = Tools()
    context.tools = tools

    outcome = asyncio.run(
        SemanticRuntimeClosure().inspect(
            context,
            base_result=initial,
            topology=TopologyIndex.from_context(context),
            evidence=[planning],
            budget=ProbeBudget(BudgetConfig()),
            requested_family="static_route",
        )
    )

    assert outcome.result is not None
    assert outcome.result.findings["fault_type"] == "static_route_misconfig"
    assert tools.calls == [("config", "leaf5"), ("route", "leaf5", "192.0.2.9/32")]


def test_multileaf_route_candidates_require_explicit_adaptive_mode():
    context = _context(src_leaf="leaf1", dst_leaf="leaf5")
    context.topology["devices"]["leafs"].append({"name": "leaf9"})
    topology = TopologyIndex.from_context(context)
    rows = [
        Evidence(
            evidence_id=f"loss-{index}",
            entity_type="path",
            entity_id=f"client{index}--client{index + 1}",
            category="packet_loss_rate",
            value=1.0,
            source="pingmesh_episode",
            timestamp=datetime.now(UTC),
            metadata={
                "src_leaf": source,
                "dst_leaf": destination,
                "src_ip": f"192.0.2.{index}",
                "dst_ip": f"192.0.2.{index + 10}",
            },
        )
        for index, (source, destination) in enumerate(
            (("leaf1", "leaf5"), ("leaf5", "leaf9"), ("leaf9", "leaf1")),
            start=1,
        )
    ]

    assert _concentrated_route_queries(rows, topology, limit=4) == []
    expanded = _concentrated_route_queries(rows, topology, limit=4, allow_multileaf=True)
    assert len(expanded) == 4
    assert len({device for device, _destination, _evidence in expanded}) >= 2


def test_runtime_route_breadth_precedes_full_table_fallback(monkeypatch, tmp_path):
    consequence = Evidence(
        evidence_id="loss-consequence",
        entity_type="path",
        entity_id="client1--client2",
        category="packet_loss_rate",
        value=0.25,
        source="pingmesh_episode",
        timestamp=datetime.now(UTC),
        metadata={"src_ip": "192.0.2.1", "dst_ip": "192.0.2.9"},
    )
    queries = [
        ("spine1", "192.0.2.9", consequence),
        ("spine2", "192.0.2.9", consequence),
        ("spine3", "192.0.2.9", consequence),
    ]
    monkeypatch.setattr(semantic_closure_module, "_concentrated_route_queries", lambda *_args, **_kwargs: queries)

    class Tools:
        def __init__(self):
            self.calls = []

        def get_route_table(self, **arguments):
            self.calls.append((arguments["device"], arguments.get("prefix")))
            if arguments["device"] == "spine3" and arguments.get("prefix"):
                return ToolResult(
                    success=True,
                    data={
                        "routes": [
                            {
                                "prefix": "192.0.2.9/32",
                                "protocol": "static",
                                "selected": True,
                                "is_discard": True,
                                "nexthops": [{"interface": "Null0"}],
                            }
                        ]
                    },
                )
            return ToolResult(success=True, data={"routes": []})

    initial = diagnosis_result(
        {"verdict": "inconclusive", "fault_type": None, "device": None, "interface": None, "confidence": 0.0}
    )
    context = _context(tmp_path=tmp_path)
    context.tools = Tools()

    outcome = asyncio.run(
        SemanticRuntimeClosure().inspect(
            context,
            base_result=initial,
            topology=TopologyIndex.from_context(context),
            evidence=[consequence],
            budget=ProbeBudget(BudgetConfig()),
            requested_family="runtime_semantic",
            max_targeted_route_queries=3,
            allow_multileaf_route_queries=True,
        )
    )

    assert outcome.result is not None
    assert outcome.result.findings["fault_type"] == "blackhole_route"
    assert context.tools.calls[:3] == [
        ("spine1", "192.0.2.9"),
        ("spine2", "192.0.2.9"),
        ("spine3", "192.0.2.9"),
    ]
    assert all(prefix is not None for _device, prefix in context.tools.calls[:3])


def test_partial_ecmp_star_queries_fabric_members_for_route_contrast():
    topology = TopologyIndex(
        devices={"spine1": "spine", "spine2": "spine", "spine3": "spine", "leaf5": "leaf"},
        links=(),
        source="partial-ecmp-test",
    )
    rows = [
        Evidence(
            evidence_id=f"partial-{index}",
            entity_type="path",
            entity_id=f"client{index}--client20",
            category="packet_loss_rate",
            value=0.25,
            source="pingmesh_episode",
            timestamp=datetime.now(UTC),
            metadata={
                "src_leaf": source_leaf,
                "dst_leaf": "leaf5",
                "src_ip": f"192.0.2.{index}",
                "dst_ip": "192.0.2.20",
            },
        )
        for index, source_leaf in enumerate(("leaf1", "leaf2", "leaf3", "leaf4"), start=1)
    ]

    queries = _concentrated_route_queries(rows, topology, limit=4, allow_multileaf=True)

    assert [(device, destination) for device, destination, _item in queries] == [
        ("spine1", "192.0.2.20"),
        ("spine2", "192.0.2.20"),
        ("spine3", "192.0.2.20"),
        ("leaf5", "192.0.2.20"),
    ]
    assert queries[0][2].metadata["route_candidate_destinations"] == ("192.0.2.20",)


def test_xlarge_route_contrast_covers_every_local_ecmp_member():
    topology = TopologyIndex(
        devices={**{f"spine{index}": "spine" for index in range(1, 17)}, "leaf65": "leaf"},
        links=(),
        attachment_devices=("leaf65",),
        source="xlarge-route-contrast-test",
    )
    rows = [
        Evidence(
            evidence_id=f"partial-{index}",
            entity_type="path",
            entity_id=f"client{index}--client65",
            category="packet_loss_rate",
            value=0.25,
            source="pingmesh_episode",
            timestamp=datetime.now(UTC),
            metadata={
                "src_leaf": f"leaf{index}",
                "dst_leaf": "leaf65",
                "src_ip": f"192.0.2.{index}",
                "dst_ip": "198.51.100.65",
            },
        )
        for index in range(1, 5)
    ]

    queries = _concentrated_route_queries(rows, topology, limit=40, allow_multileaf=True)

    queried_devices = {device for device, _destination, _evidence in queries}
    assert {f"spine{index}" for index in range(1, 17)} <= queried_devices
    assert len(queries) == 17


def test_partial_ecmp_route_scan_checks_both_directions_once_per_spine(tmp_path):
    context = _context(tmp_path=tmp_path)
    topology = TopologyIndex(
        devices={
            "spine1": "spine",
            "spine2": "spine",
            "spine3": "spine",
            "leaf1": "leaf",
            "leaf2": "leaf",
            "leaf3": "leaf",
            "leaf5": "leaf",
        },
        links=(),
        source="two-direction-route-test",
    )
    rows = []
    for index, source_leaf in enumerate(("leaf1", "leaf2", "leaf3"), start=1):
        rows.append(
            Evidence(
                evidence_id=f"to-a-{index}",
                entity_type="path",
                entity_id=f"client{index}--client20",
                category="packet_loss_rate",
                value=0.25,
                source="pingmesh_episode",
                timestamp=datetime.now(UTC),
                metadata={
                    "src_leaf": source_leaf,
                    "dst_leaf": "leaf5",
                    "src_ip": f"192.0.2.{index}",
                    "dst_ip": "192.0.2.20",
                },
            )
        )
        rows.append(
            Evidence(
                evidence_id=f"to-b-{index}",
                entity_type="path",
                entity_id=f"client{index + 10}--client30",
                category="packet_loss_rate",
                value=0.25,
                source="pingmesh_episode",
                timestamp=datetime.now(UTC),
                metadata={
                    "src_leaf": source_leaf,
                    "dst_leaf": "leaf5",
                    "src_ip": f"198.51.100.{index}",
                    "dst_ip": "198.51.100.30",
                },
            )
        )

    class Tools:
        def __init__(self):
            self.calls = []

        def get_route_table(self, **arguments):
            self.calls.append(arguments)
            discard = arguments["device"] == "spine3"
            return ToolResult(
                success=True,
                data={
                    "routes": [
                        {
                            "prefix": "198.51.100.30/32" if discard else "0.0.0.0/0",
                            "protocol": "static" if discard else "bgp",
                            "selected": True,
                            "is_discard": discard,
                            "nexthops": [] if discard else [{"interface": "Ethernet0"}],
                        }
                    ]
                },
            )

        def get_device_acl(self, **_arguments):
            raise AssertionError("ECMP route scan must not spend its bounded call on generic ACL triage")

    context.tools = Tools()
    result = asyncio.run(
        SemanticRuntimeClosure().inspect(
            context,
            base_result=_runtime_failure(),
            topology=topology,
            evidence=rows,
            budget=ProbeBudget(BudgetConfig(max_extra_tool_calls_per_hard_case=4)),
            requested_family="runtime_semantic",
            max_targeted_route_queries=3,
            allow_multileaf_route_queries=True,
        )
    )

    assert result.result is not None
    assert result.result.findings["fault_type"] == "blackhole_route"
    assert result.result.findings["location"]["device"] == "spine3"
    assert result.result.metadata["diagnostic_harness"]["semantic_closure"]["evidence_ids"][-1] == "to-b-1"
    assert [call["device"] for call in context.tools.calls] == ["spine1", "spine2", "spine3"]
    assert all("prefix" not in call and call["max_routes"] == 500 for call in context.tools.calls)


def _runtime_failure(*, reasoning: str = "GRAPH_RECURSION_LIMIT reached before a result was emitted."):
    return diagnosis_result(
        {
            "verdict": "inconclusive",
            "fault_type": None,
            "device": None,
            "interface": None,
            "confidence": 0.0,
            "reasoning": reasoning,
        }
    )


def _write_manifest(tmp_path):
    return write_two_leaf_manifest(
        tmp_path,
        topology_id="semantic-test",
        second_leaf="leaf5",
        second_client="client9",
    )


def _context(*, src_leaf: str = "leaf1", dst_leaf: str = "leaf5", tmp_path=None) -> DiagnosticContext:
    return DiagnosticContext(
        scenario_id="opaque-runtime-failure",
        topology={
            "devices": {
                "leafs": [{"name": "leaf1"}, {"name": "leaf5"}],
                "spines": [{"name": "spine1"}],
                "clients": [],
            },
            "links": [
                {
                    "endpoints": [
                        {"device": "spine1", "interface": "eth1"},
                        {"device": "leaf1", "interface": "eth1"},
                    ]
                },
                {
                    "endpoints": [
                        {"device": "spine1", "interface": "eth2"},
                        {"device": "leaf5", "interface": "eth1"},
                    ]
                },
            ],
        },
        symptoms={
            "observations": {
                "pingmesh_metrics": {
                    "anomalies": [
                        {
                            "type": "path_unreachable",
                            "src_name": "client1",
                            "dst_name": "client9",
                            "src_ip": "192.0.2.1",
                            "dst_ip": "192.0.2.2",
                            "src_leaf": src_leaf,
                            "dst_leaf": dst_leaf,
                            "value": 100.0,
                            "sample_count": 30,
                            "persistence": "persistent",
                        }
                    ]
                }
            }
        },
        metadata=(
            {"worker_env": {"NETOPSBENCH_TOPOLOGY_DIR": str(_write_manifest(tmp_path).parent)}}
            if tmp_path is not None
            else {}
        ),
    )


def test_runtime_closure_uses_active_acl_and_dataplane_drop_evidence(tmp_path):
    class Tools:
        def get_device_acl(self, **arguments):
            device = arguments["device"]
            if device != "leaf1":
                return ToolResult(success=True, data={"sonic_acl_config": "", "iptables_forward_rules": ""})
            return ToolResult(
                success=True,
                data={
                    "sonic_acl_config": (
                        "ACL_TEST L3 Ethernet0 injected ingress Active\n"
                        "ACL_TEST RULE_1 999 DROP DST_IP: 192.0.2.0/24 Active\n"
                    ),
                    "iptables_forward_rules": "1 42 8400 DROP all -- * * 0.0.0.0/0 192.0.2.0/24\n",
                },
            )

    context = _context(src_leaf="leaf1", dst_leaf="leaf1", tmp_path=tmp_path)
    context.tools = Tools()

    result = asyncio.run(DiagnosticHarness(BaseAgent(_runtime_failure())).diagnose(context))

    assert result.verdict == "fault_detected"
    assert result.findings["fault_type"] == "acl_misconfig"
    assert result.findings["location"] == {"device": "leaf1", "interface": "Ethernet0"}
    harness = result.metadata["diagnostic_harness"]
    assert harness["hard_path_status"] == "semantic_runtime_closure"
    assert harness["probe_outcome"]["metadata"]["runtime_error_used_as_fault_evidence"] is False
    assert harness["cost"] == {"tool_calls": 1, "active_probes": 0, "probe_packets": 0}


def test_runtime_closure_requires_config_and_unresolved_static_route_evidence():
    class Tools:
        def get_device_acl(self, **_arguments):
            return ToolResult(success=True, data={"sonic_acl_config": "", "iptables_forward_rules": ""})

        def get_device_config(self, **arguments):
            config = "ip route 192.0.2.9/32 192.0.2.10\n" if arguments["device"] == "leaf5" else ""
            return ToolResult(success=True, data={"device": arguments["device"], "config": config})

        def get_route_table(self, **arguments):
            return ToolResult(
                success=True,
                data={
                    "device": arguments["device"],
                    "routes": [
                        {
                            "prefix": arguments["prefix"],
                            "protocol": "static",
                            "nexthops": [],
                            "selected": True,
                            "is_discard": False,
                        }
                    ],
                },
            )

    context = _context()
    context.tools = Tools()

    result = asyncio.run(DiagnosticHarness(BaseAgent(_runtime_failure())).diagnose(context))

    assert result.verdict == "fault_detected"
    assert result.findings["fault_type"] == "static_route_misconfig"
    assert result.findings["location"] == {"device": "leaf5", "interface": None}
    harness = result.metadata["diagnostic_harness"]
    assert harness["semantic_closure"]["public_tools_only"]
    assert harness["cost"] == {"tool_calls": 5, "active_probes": 0, "probe_packets": 0}
    sources = {item["source"] for item in harness["evidence"] if item["reliability"] > 0}
    assert {"get_device_config", "get_route_table"} <= sources


def test_runtime_closure_rejects_selected_static_next_hop_that_is_a_topology_client():
    class Tools:
        def get_device_config(self, **arguments):
            config = "ip route 198.51.100.0/24 192.0.2.1\n" if arguments["device"] == "leaf5" else ""
            return ToolResult(success=True, data={"device": arguments["device"], "config": config})

        def get_route_table(self, **arguments):
            return ToolResult(
                success=True,
                data={
                    "device": arguments["device"],
                    "routes": [
                        {
                            "prefix": arguments["prefix"],
                            "protocol": "static",
                            "nexthops": [{"address": "192.0.2.1"}],
                            "selected": True,
                            "is_discard": False,
                        }
                    ],
                },
            )

    context = _context()
    context.topology["devices"]["clients"] = [
        {"name": "client1", "role": "client", "data_ip": "192.0.2.1", "attached_switch": "leaf1"}
    ]
    context.tools = Tools()

    outcome = asyncio.run(
        SemanticRuntimeClosure().inspect(
            context,
            base_result=_runtime_failure(),
            topology=TopologyIndex.from_context(context),
            evidence=evidence_from_diagnosis(_runtime_failure()),
            budget=ProbeBudget(BudgetConfig()),
            requested_family="static_route",
        )
    )

    assert outcome.result is not None
    assert outcome.result.findings["fault_type"] == "static_route_misconfig"
    assert outcome.result.findings["location"]["device"] == "leaf5"
    route = next(item for item in outcome.outcome.evidence if item.category == "route_presence")
    assert route.value["invalid_next_hop_role"] is True
    assert route.value["next_hop_device"] == "client1"


def test_runtime_closure_resolves_client_next_hop_from_runtime_manifest(tmp_path):
    class Tools:
        def get_device_config(self, **arguments):
            config = "ip route 198.51.100.0/24 192.0.2.2\n" if arguments["device"] == "leaf5" else ""
            return ToolResult(success=True, data={"config": config})

        def get_route_table(self, **arguments):
            return ToolResult(
                success=True,
                data={
                    "routes": [
                        {
                            "prefix": arguments["prefix"],
                            "protocol": "unknown",
                            "admin_distance": 1,
                            "nexthops": [{"via": "192.0.2.2", "interface": "Ethernet8"}],
                            "selected": True,
                            "is_discard": False,
                        }
                    ]
                },
            )

    initial = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "static_route_misconfig",
            "device": "leaf5",
            "confidence": 0.95,
            "evidence": ["leaf5 config contains ip route 198.51.100.0/24 192.0.2.2"],
        }
    )
    context = _context(tmp_path=tmp_path)
    context.tools = Tools()

    outcome = asyncio.run(
        SemanticRuntimeClosure().inspect(
            context,
            base_result=initial,
            topology=TopologyIndex.from_context(context),
            evidence=evidence_from_diagnosis(initial),
            budget=ProbeBudget(BudgetConfig()),
            requested_family="static_route",
        )
    )

    assert outcome.result is not None
    assert outcome.result.findings["fault_type"] == "static_route_misconfig"
    route = next(item for item in outcome.outcome.evidence if item.category == "route_presence")
    assert route.value["next_hop_device"] == "client9"
    assert route.value["invalid_next_hop_role"] is True


def test_runtime_closure_confirms_selected_discard_route_within_semantic_cap():
    class Tools:
        def __init__(self):
            self.calls = 0

        def get_device_acl(self, **_arguments):
            self.calls += 1
            return ToolResult(success=True, data={"sonic_acl_config": "", "iptables_forward_rules": ""})

        def get_device_config(self, **arguments):
            self.calls += 1
            config = "ip route 192.0.2.0/30 Null0\n" if arguments["device"] == "leaf5" else ""
            return ToolResult(success=True, data={"device": arguments["device"], "config": config})

        def get_route_table(self, **arguments):
            self.calls += 1
            return ToolResult(
                success=True,
                data={
                    "routes": [
                        {
                            "prefix": arguments["prefix"],
                            "protocol": "static",
                            "nexthops": [],
                            "selected": True,
                            "is_discard": True,
                        }
                    ]
                },
            )

    context = _context()
    context.tools = Tools()

    result = asyncio.run(DiagnosticHarness(BaseAgent(_runtime_failure())).diagnose(context))

    assert result.verdict == "fault_detected"
    assert result.findings["fault_type"] == "blackhole_route"
    assert result.findings["location"] == {"device": "leaf5", "interface": None}
    assert context.tools.calls == 5
    evidence = result.metadata["diagnostic_harness"]["evidence"]
    route = next(item for item in evidence if item["source"] == "get_route_table")
    assert route["value"]["is_discard"] is True


def test_runtime_closure_checks_concentrated_flow_routes_before_generic_scans(tmp_path):
    class Tools:
        def __init__(self):
            self.route_queries = []

        def get_route_table(self, **arguments):
            self.route_queries.append((arguments["device"], arguments["prefix"]))
            discard = arguments["device"] == "leaf5" and arguments["prefix"] == "192.0.2.1"
            return ToolResult(
                success=True,
                data={
                    "routes": [
                        {
                            "prefix": arguments["prefix"],
                            "protocol": "static" if discard else "bgp",
                            "nexthops": [] if discard else [{"ip": "198.51.100.1"}],
                            "selected": True,
                            "is_discard": discard,
                        }
                    ]
                },
            )

        def get_device_acl(self, **_arguments):
            return ToolResult(success=True, data={"sonic_acl_config": "", "iptables_forward_rules": ""})

    context = _context(tmp_path=tmp_path)
    context.symptoms["observations"]["pingmesh_metrics"]["anomalies"] = [
        {
            "type": "path_unreachable",
            "src_name": "client1",
            "src_ip": "192.0.2.1",
            "dst_name": "client9",
            "dst_ip": "192.0.2.2",
            "src_leaf": "leaf1",
            "dst_leaf": "leaf5",
            "value": 100.0,
            "sample_count": 30,
            "persistence": "persistent",
        },
        {
            "type": "path_unreachable",
            "src_name": "client9",
            "src_ip": "192.0.2.2",
            "dst_name": "client1",
            "dst_ip": "192.0.2.1",
            "src_leaf": "leaf5",
            "dst_leaf": "leaf1",
            "value": 100.0,
            "sample_count": 30,
            "persistence": "persistent",
        },
    ]
    context.tools = Tools()

    result = asyncio.run(DiagnosticHarness(BaseAgent(_runtime_failure())).diagnose(context))

    assert result.verdict == "fault_detected"
    assert result.findings["fault_type"] == "blackhole_route"
    assert result.findings["location"] == {"device": "leaf5", "interface": None}
    assert context.tools.route_queries == [("leaf1", "192.0.2.2"), ("leaf5", "192.0.2.1")]
    harness = result.metadata["diagnostic_harness"]
    assert harness["cost"] == {"tool_calls": 3, "active_probes": 0, "probe_packets": 0}
    assert {item["source"] for item in harness["evidence"] if item["reliability"] > 0} >= {
        "get_route_table",
        "pingmesh_episode",
    }


def test_runtime_closure_reuses_targeted_route_for_static_config_confirmation_within_cap(tmp_path):
    class Tools:
        def __init__(self):
            self.calls = []

        def get_device_acl(self, **arguments):
            self.calls.append(("get_device_acl", arguments["device"]))
            return ToolResult(success=True, data={"sonic_acl_config": "", "iptables_forward_rules": ""})

        def get_route_table(self, **arguments):
            self.calls.append(("get_route_table", arguments["device"], arguments.get("prefix")))
            faulty = arguments["device"] == "leaf5" and arguments.get("prefix") == "192.0.2.1"
            return ToolResult(
                success=True,
                data={
                    "routes": [
                        {
                            "prefix": "192.0.2.1/32" if faulty else arguments.get("prefix"),
                            "protocol": "static" if faulty else "bgp",
                            "admin_distance": 1 if faulty else 20,
                            "nexthops": (
                                [{"via": "192.0.2.2", "interface": "eth5"}]
                                if faulty
                                else [{"via": "198.51.100.1", "interface": "eth1"}]
                            ),
                            "selected": True,
                            "is_discard": False,
                        }
                    ]
                },
            )

        def get_device_config(self, **arguments):
            self.calls.append(("get_device_config", arguments["device"]))
            config = "ip route 192.0.2.1/32 192.0.2.2\n" if arguments["device"] == "leaf5" else ""
            return ToolResult(success=True, data={"config": config})

    context = _context(tmp_path=tmp_path)
    context.tools = Tools()
    topology = TopologyIndex.from_context(context)
    base = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "blackhole_route",
            "device": "leaf5",
            "confidence": 0.60,
            "evidence": ["A forwarding loop is localized to leaf5, but the selected route was not verified."],
        }
    )
    rows = [
        Evidence(
            evidence_id="loss-forward",
            entity_type="path",
            entity_id="client1--client9",
            category="packet_loss_rate",
            value=1.0,
            source="pingmesh_episode",
            timestamp=datetime.now(UTC),
            metadata={
                "src_leaf": "leaf1",
                "dst_leaf": "leaf5",
                "src_ip": "192.0.2.1",
                "dst_ip": "192.0.2.2",
            },
        ),
        Evidence(
            evidence_id="loss-reverse",
            entity_type="path",
            entity_id="client9--client1",
            category="packet_loss_rate",
            value=1.0,
            source="pingmesh_episode",
            timestamp=datetime.now(UTC),
            metadata={
                "src_leaf": "leaf5",
                "dst_leaf": "leaf1",
                "src_ip": "192.0.2.2",
                "dst_ip": "192.0.2.1",
            },
        ),
    ]
    budget = ProbeBudget(BudgetConfig(max_extra_tool_calls_per_hard_case=12))
    budget.configure_stages(family="runtime_semantic", semantic_closure_cap=6)

    async def run_closure():
        with budget.use_stage("semantic_closure"):
            return await SemanticRuntimeClosure().inspect(
                context,
                base_result=base,
                topology=topology,
                evidence=rows,
                budget=budget,
                requested_family="runtime_semantic",
                max_targeted_route_queries=2,
            )

    outcome = asyncio.run(run_closure())

    assert outcome.result is not None
    assert outcome.result.findings["fault_type"] == "static_route_misconfig"
    assert outcome.result.findings["location"] == {"device": "leaf5", "interface": None}
    assert budget.tool_calls == 4
    assert budget.spent_by_stage["semantic_closure"] == 4
    assert context.tools.calls == [
        ("get_device_acl", "leaf5"),
        ("get_route_table", "leaf1", "192.0.2.2"),
        ("get_route_table", "leaf5", "192.0.2.1"),
        ("get_device_config", "leaf5"),
    ]
    route = next(item for item in outcome.outcome.evidence if item.category == "route_presence")
    assert route.metadata["reused_targeted_route_snapshot"] is True
    assert route.value["invalid_next_hop_role"] is True


def test_runtime_failure_without_semantic_evidence_remains_inconclusive():
    class Tools:
        def get_device_acl(self, **_arguments):
            return ToolResult(success=True, data={"sonic_acl_config": "", "iptables_forward_rules": ""})

        def get_device_config(self, **arguments):
            return ToolResult(success=True, data={"device": arguments["device"], "config": ""})

    context = _context()
    context.tools = Tools()

    result = asyncio.run(DiagnosticHarness(BaseAgent(_runtime_failure())).diagnose(context))

    assert result.verdict == "inconclusive"
    assert result.findings["fault_type"] is None
    assert result.metadata["diagnostic_harness"]["probe_outcome"]["status"] == "inconclusive"


def test_runtime_tool_errors_are_zero_reliability_and_budget_is_bounded():
    class Tools:
        def __init__(self):
            self.calls = 0

        def get_device_acl(self, **_arguments):
            self.calls += 1
            raise TimeoutError("public ACL query timed out")

        def get_device_config(self, **_arguments):
            self.calls += 1
            raise AssertionError("budget should prevent this invocation")

    context = _context()
    context.tools = Tools()
    config = HarnessConfig(budget=BudgetConfig(max_extra_tool_calls_per_hard_case=1))

    result = asyncio.run(DiagnosticHarness(BaseAgent(_runtime_failure()), config=config).diagnose(context))

    assert result.verdict == "inconclusive"
    assert context.tools.calls == 1
    harness = result.metadata["diagnostic_harness"]
    assert harness["cost"]["tool_calls"] == 1
    errors = [item for item in harness["evidence"] if item["category"] == "tool_error"]
    assert errors
    assert all(item["reliability"] == 0.0 for item in errors)
    assert all(item["evidence_id"] not in result.findings.get("evidence", []) for item in errors)


def test_static_route_client_role_is_resolved_from_selected_egress(tmp_path):
    class Tools:
        def get_device_config(self, **arguments):
            config = "ip route 198.51.100.0/24 203.0.113.99\n" if arguments["device"] == "leaf5" else ""
            return ToolResult(success=True, data={"config": config})

        def get_route_table(self, **arguments):
            return ToolResult(
                success=True,
                data={
                    "routes": [
                        {
                            "prefix": arguments["prefix"],
                            "protocol": "static",
                            "nexthops": [{"via": "203.0.113.99", "interface": "eth5"}],
                            "selected": True,
                            "is_discard": False,
                        }
                    ]
                },
            )

    initial = diagnosis_result(
        {
            "verdict": "fault_detected",
            "fault_type": "static_route_misconfig",
            "device": "leaf5",
            "confidence": 0.90,
            "evidence": ["leaf5 selected static route 198.51.100.0/24 via 203.0.113.99"],
        }
    )
    context = _context(tmp_path=tmp_path)
    context.tools = Tools()

    outcome = asyncio.run(
        SemanticRuntimeClosure().inspect(
            context,
            base_result=initial,
            topology=TopologyIndex.from_context(context),
            evidence=evidence_from_diagnosis(initial),
            budget=ProbeBudget(BudgetConfig()),
            requested_family="static_route",
        )
    )

    assert outcome.result is not None
    route = next(item for item in outcome.outcome.evidence if item.category == "route_presence")
    assert route.value["invalid_next_hop_role"] is True
    assert route.value["next_hop_device"] == "client9"
    assert route.value["egress_interface"]
