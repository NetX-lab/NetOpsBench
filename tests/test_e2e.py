#!/usr/bin/env python3
"""
End-to-end tests for NetOpsBench benchmark system.

These tests verify the complete benchmark flow works correctly.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from netopsbench.evaluator.scorer import AgentOutput, EvaluationResult, Evaluator
from netopsbench.platform.faults.injector import FaultInjector
from netopsbench.platform.faults.specs import get_builtin_fault_specs
from netopsbench.platform.pingmesh.generator import PinglistGenerator
from netopsbench.platform.scenario.parser import parse_scenario_file
from netopsbench.platform.scenario.validator import validate_scenario, validate_scenario_topology
from netopsbench.platform.session.scoring import resolve_scenario_files, score_scenario_fault_episodes
from netopsbench.platform.toolkit import fastmcp_server
from netopsbench.platform.toolkit.mcp.registry import load_tool_specs

# Internal test path: direct toolkit import keeps implementation-level e2e checks fast.
from netopsbench.platform.toolkit.toolkit import AgentToolkit, ToolResult
from netopsbench.platform.topology.generator import TOPOLOGY_SCALES, generate_topology
from netopsbench.platform.utils.interface_names import are_interfaces_equivalent


def _generated_scenario_path(filename: str) -> str:
    generated_root = Path("scenarios/generated")
    if not generated_root.exists():
        pytest.skip("scenarios/generated not found; generate scenarios before running this test")
    matches = sorted(generated_root.rglob(filename))
    if not matches:
        pytest.skip(f"{filename} not found under scenarios/generated; generate matching scenarios first")
    return str(matches[0])


class TestTopologyGenerator:
    """Tests for topology generation."""

    def test_topology_scales_defined(self):
        """Test that all expected topology scales are defined."""
        expected_scales = ["xs", "small", "medium", "large"]
        for scale in expected_scales:
            assert scale in TOPOLOGY_SCALES, f"Scale {scale} not defined"

    def test_generate_xs_topology(self):
        """Test generating an xs topology."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = generate_topology("xs", tmpdir)

            assert os.path.exists(result["yaml_file"]), "YAML file not created"
            assert os.path.exists(result["metadata_file"]), "Metadata file not created"
            assert len(result["config_files"]) == 4, "Expected 4 config files (2 spines + 2 leafs)"

            # Verify metadata
            metadata = result["metadata"]
            assert metadata["scale"]["num_spines"] == 2
            assert metadata["scale"]["num_leafs"] == 2
            assert metadata["scale"]["clients_per_leaf"] == 1
            assert metadata["scale"]["total_clients"] == 2

            rendered_yaml = Path(result["yaml_file"]).read_text(encoding="utf-8")
            assert "yyyyyt123/netopsbench-sonic-vs-202505-telemetry:202505-telemetry" in rendered_yaml

    def test_generated_switch_configs_seed_gnmi_defaults(self):
        """Generated SONiC configs should seed the GNMI config expected by 202505 images."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = generate_topology("xs", tmpdir)

            for config_path in result["config_files"]:
                text = Path(config_path).read_text(encoding="utf-8")
                assert "sonic-db-cli CONFIG_DB hmset 'GNMI|gnmi' port 50051" in text
                assert "sonic-db-cli CONFIG_DB hmset 'GNMI|certs'" in text
                assert "pgrep -x telemetry" in text
                assert "/usr/sbin/telemetry -port 50051 -noTLS -client_auth none" in text

    def test_generate_small_topology(self):
        """Test generating a small topology."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = generate_topology("small", tmpdir)

            metadata = result["metadata"]
            assert metadata["scale"]["num_spines"] == 2
            assert metadata["scale"]["num_leafs"] == 4
            assert metadata["scale"]["clients_per_leaf"] == 2
            assert metadata["scale"]["total_clients"] == 8


class TestAgentToolkit:
    """Tests for agent toolkit."""

    def test_toolkit_initialization(self):
        """Test toolkit initializes correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            topo = generate_topology("xs", tmpdir)
            toolkit = AgentToolkit(topology_metadata=topo["metadata"])
            assert toolkit is not None
            assert toolkit.grafana_url is not None
            assert toolkit.influxdb_url is not None

    def test_tool_registry_specs_complete(self):
        """Tool registry specs should define non-empty name/group/handler."""
        specs = load_tool_specs()
        assert specs, "tool specs should not be empty"
        for spec in specs:
            assert spec.name
            assert spec.group
            assert callable(spec.handler)

    def test_get_topology_returns_result(self):
        """Test get_topology returns a valid result."""
        with tempfile.TemporaryDirectory() as tmpdir:
            topo = generate_topology("xs", tmpdir)
            toolkit = AgentToolkit(topology_metadata=topo["metadata"])
            result = toolkit.get_topology()

            assert isinstance(result, ToolResult)
            assert result.success is True
            assert result.data is not None
            assert "devices" in result.data
            assert "links" in result.data

    def test_toolkit_loads_dynamic_topology(self):
        """Test toolkit can load dynamic topology from metadata."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Generate topology
            result = generate_topology("xs", tmpdir)
            metadata = result["metadata"]

            # Create toolkit with metadata
            toolkit = AgentToolkit(topology_metadata=metadata)

            assert toolkit.topology_name == "dcn"
            assert "spine1" in toolkit.container_names
            assert "leaf1" in toolkit.container_names
            assert "client1" in toolkit.container_names

    def test_reload_topology(self):
        """Test toolkit can reload topology at runtime."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Generate topology
            result = generate_topology("xs", tmpdir)

            toolkit = AgentToolkit(topology_metadata=result["metadata"])
            reload_result = toolkit.reload_topology(metadata_file=result["metadata_file"])

            assert reload_result.success is True
            assert reload_result.data["topology_name"] == "dcn"


class TestPingmeshGenerator:
    """Tests for Pingmesh pinglist generation across topology scales."""

    @pytest.mark.parametrize("scale", list(TOPOLOGY_SCALES.keys()))
    def test_pinglist_scales_with_topology(self, scale):
        """Pinglist should be N*(N-1) for N clients across all scales."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = generate_topology(scale, tmpdir)
            metadata = result["metadata"]

            generator = PinglistGenerator()
            tasks = generator.generate(metadata)

            total_clients = metadata["scale"]["total_clients"]
            assert len(tasks) == total_clients * (total_clients - 1)
            assert all(t.src_name != t.dst_name for t in tasks)
            assert {t.path_type for t in tasks}.issubset({"same_rack", "cross_rack"})


class TestFastMCPServer:
    """Tests for FastMCP server tool wiring."""

    def test_fastmcp_tool_registry_exists(self):
        """FastMCP module should expose a non-empty tool registry."""
        assert len(fastmcp_server.EXPOSED_TOOLS) > 0

    def test_fastmcp_has_all_expected_tools(self):
        """FastMCP module should export all MCP tool callables."""
        expected_tools = [
            "get_topology",
            "get_device_interfaces",
            "get_bgp_neighbors",
            "get_route_table",
            "get_device_config",
            "get_bgp_rib",
            "get_device_acl",
            "get_device_logs",
            "traceroute",
            "ping_test",
            "get_interface_metrics",
            "get_all_bgp_status",
            "get_pingmesh_summary",
            "get_pingmesh_hotspots",
        ]

        for tool in expected_tools:
            assert hasattr(fastmcp_server, tool), f"Tool {tool} callable missing from fastmcp_server"
            assert tool in fastmcp_server.EXPOSED_TOOLS, f"Tool {tool} missing from EXPOSED_TOOLS"

    def test_tool_registry_matches_fastmcp_tools(self):
        """Tool registry should stay in sync with FastMCP exported tools."""
        tool_names = [spec.name for spec in load_tool_specs()]

        assert len(tool_names) == len(fastmcp_server.EXPOSED_TOOLS), "Tool count mismatch"
        for name in tool_names:
            assert name in fastmcp_server.EXPOSED_TOOLS, f"Tool {name} in definitions but not in FastMCP"


class TestFaultInjector:
    """Tests for fault injection."""

    def test_injector_initialization(self):
        """Test fault injector initializes correctly."""
        injector = FaultInjector(
            topology_metadata={"name": "dcn", "devices": {"spines": [], "leafs": [], "clients": []}}
        )
        assert injector is not None
        assert injector.topology_name == "dcn"
        assert injector.container_names == {}
        assert injector.active_faults == []

    def test_injector_loads_dynamic_topology(self):
        """Test injector can load dynamic topology."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = generate_topology("xs", tmpdir)
            metadata = result["metadata"]

            injector = FaultInjector(topology_metadata=metadata)

            assert "spine1" in injector.container_names
            assert "leaf1" in injector.container_names

    def test_reload_topology(self):
        """Test injector can reload topology at runtime."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = generate_topology("xs", tmpdir)

            injector = FaultInjector(
                topology_metadata={"name": "dcn", "devices": {"spines": [], "leafs": [], "clients": []}}
            )
            reload_result = injector.reload_topology(metadata_file=result["metadata_file"])

            assert reload_result["success"] is True

    def test_fault_types_defined(self):
        """Test all expected fault types are defined."""
        expected_faults = [
            "link_down",
            "link_flapping",
            "device_down",
            "bgp_neighbor_misconfig",
            "route_policy_misconfig",
            "mtu_mismatch",
            "packet_corruption",
            "packet_loss",
            "high_latency",
            "blackhole_route",
            "static_route_misconfig",
        ]

        defined_faults = [spec.name for spec in get_builtin_fault_specs()]
        for fault in expected_faults:
            assert fault in defined_faults, f"Fault type {fault} not defined"

    def test_inject_high_latency_uses_latency_ms_contract(self):
        injector = FaultInjector(topology_metadata={"devices": {"spines": [], "leafs": [], "clients": []}})

        class FakeImpairment:
            def __init__(self):
                self.calls = []

            def inject_high_latency(self, device, interface, latency_ms=100):
                self.calls.append((device, interface, latency_ms))
                return {"success": True, "type": "high_latency", "latency_ms": latency_ms}

        fake = FakeImpairment()
        injector._impairment = fake

        result = injector.inject_high_latency("leaf1", "Ethernet0", latency_ms=120)

        assert result["success"] is True
        assert fake.calls == [("leaf1", "Ethernet0", 120)]

    def test_inject_mtu_mismatch_uses_mtu_contract(self):
        injector = FaultInjector(topology_metadata={"devices": {"spines": [], "leafs": [], "clients": []}})

        class FakeImpairment:
            def __init__(self):
                self.calls = []

            def inject_mtu_mismatch(self, device, interface, mtu=1400):
                self.calls.append((device, interface, mtu))
                return {"success": True, "type": "mtu_mismatch", "mtu": mtu}

        fake = FakeImpairment()
        injector._impairment = fake

        result = injector.inject_mtu_mismatch("leaf1", "Ethernet0", mtu=1450)

        assert result["success"] is True
        assert fake.calls == [("leaf1", "Ethernet0", 1450)]

    def test_inject_packet_corruption_uses_corruption_pct_contract(self):
        injector = FaultInjector(topology_metadata={"devices": {"spines": [], "leafs": [], "clients": []}})

        class FakeImpairment:
            def __init__(self):
                self.calls = []

            def inject_packet_corruption(self, device, interface, corruption_pct=20):
                self.calls.append((device, interface, corruption_pct))
                return {"success": True, "type": "packet_corruption", "corruption_pct": corruption_pct}

        fake = FakeImpairment()
        injector._impairment = fake

        result = injector.inject_packet_corruption("leaf1", "Ethernet0", corruption_pct=17)

        assert result["success"] is True
        assert fake.calls == [("leaf1", "Ethernet0", 17)]

    def test_inject_packet_loss_uses_loss_pct_contract(self):
        injector = FaultInjector(topology_metadata={"devices": {"spines": [], "leafs": [], "clients": []}})

        class FakeImpairment:
            def __init__(self):
                self.calls = []

            def inject_packet_loss(self, device, interface, loss_pct=10):
                self.calls.append((device, interface, loss_pct))
                return {"success": True, "type": "packet_loss", "loss_pct": loss_pct}

        fake = FakeImpairment()
        injector._impairment = fake

        result = injector.inject_packet_loss("leaf1", "Ethernet0", loss_pct=11)

        assert result["success"] is True
        assert fake.calls == [("leaf1", "Ethernet0", 11)]

    def test_injector_interface_alias_resolution_supports_vendor_style(self):
        injector = FaultInjector(topology_metadata={"devices": {"spines": [], "leafs": [], "clients": []}})
        assert injector._iface.resolve_linux("ethernet-1/2") == "eth2"
        assert injector._iface.resolve_sonic("ethernet-1/2") == "Ethernet4"


class TestEvaluator:
    """Tests for evaluation system."""

    def test_evaluator_initialization(self):
        """Test evaluator initializes correctly."""
        evaluator = Evaluator()
        assert evaluator is not None
        assert sum(evaluator.weights.values()) == 1.0

    def test_evaluate_correct_answer(self):
        """Test evaluating a correct agent answer."""
        evaluator = Evaluator()

        agent_output = AgentOutput(
            verdict="fault_detected",
            fault_type="link_down",
            location={"device": "spine1", "interface": "Ethernet0"},
            confidence=0.95,
        )

        ground_truth = {"fault_type": "link_down", "location": {"device": "spine1", "interface": "Ethernet0"}}

        result = evaluator.evaluate(agent_output, ground_truth, "test_001")

        assert result.correct_verdict is True
        assert result.correct_device is True
        assert result.correct_interface is True
        assert result.correct_fault_type is True
        assert result.score == 1.0

    def test_evaluate_wrong_device(self):
        """Test evaluating answer with wrong device."""
        evaluator = Evaluator()

        agent_output = AgentOutput(
            verdict="fault_detected",
            fault_type="link_down",
            location={"device": "spine2", "interface": "Ethernet0"},  # Wrong device
            confidence=0.8,
        )

        ground_truth = {"fault_type": "link_down", "location": {"device": "spine1", "interface": "Ethernet0"}}

        result = evaluator.evaluate(agent_output, ground_truth, "test_002")

        assert result.correct_verdict is True
        assert result.correct_device is False
        assert result.score < 1.0

    def test_evaluate_fault_case_wrong_verdict_forces_zero_score(self):
        """Fault cases should earn zero score when verdict is incorrect."""
        evaluator = Evaluator()

        agent_output = AgentOutput(
            verdict="inconclusive",
            fault_type="link_down",
            location={"device": "spine1", "interface": "Ethernet0"},
            confidence=0.8,
        )

        ground_truth = {
            "fault_type": "link_down",
            "location": {"device": "spine1", "interface": "Ethernet0"},
        }

        result = evaluator.evaluate(agent_output, ground_truth, "test_wrong_verdict_zero")

        assert result.correct_verdict is False
        assert result.correct_device is True
        assert result.correct_interface is True
        assert result.score == 0.0

    def test_evaluate_link_peer_equivalent_location(self):
        """Link failures should accept either endpoint of the failed routed link."""
        evaluator = Evaluator()

        agent_output = AgentOutput(
            verdict="fault_detected",
            fault_type="link_down",
            location={"device": "leaf1", "interface": "Ethernet0"},
            confidence=0.85,
        )

        ground_truth = {
            "fault_type": "link_down",
            "location": {"device": "spine1", "interface": "e1-1"},
            "equivalent_locations": [{"device": "leaf1", "interface": "ethernet-1/1"}],
        }

        result = evaluator.evaluate(agent_output, ground_truth, "test_002_peer")

        assert result.correct_verdict is True
        assert result.correct_device is True
        assert result.correct_interface is True
        assert result.score == 1.0
        assert result.details["location_match_mode"] == "equivalent"

    def test_generate_report(self):
        """Test report generation."""
        evaluator = Evaluator()

        results = [
            EvaluationResult(
                testcase_id="test_001",
                correct_verdict=True,
                correct_device=True,
                correct_interface=True,
                correct_fault_type=True,
                score=1.0,
                details={"difficulty": "easy"},
            ),
            EvaluationResult(
                testcase_id="test_002",
                correct_verdict=True,
                correct_device=False,
                correct_interface=True,
                correct_fault_type=True,
                score=0.7,
                details={"difficulty": "medium"},
            ),
        ]

        report = evaluator.generate_report(results, "test_agent", "xs")

        assert report["agent_name"] == "test_agent"
        assert report["topology_scale"] == "xs"
        assert report["summary"]["total_cases"] == 2
        assert report["summary"]["average_score"] == 0.85

    def test_generate_report_excludes_negative_samples_from_fault_metrics(self):
        """Healthy-network cases should affect detection, not fault-localization KPIs."""
        evaluator = Evaluator()

        results = [
            EvaluationResult(
                testcase_id="fault_case",
                correct_verdict=True,
                correct_device=False,
                correct_interface=False,
                correct_fault_type=False,
                score=0.0,
                details={
                    "difficulty": "medium",
                    "ground_truth": {
                        "fault_type": "packet_loss",
                        "location": {"device": "leaf1", "interface": "Ethernet0"},
                    },
                },
            ),
            EvaluationResult(
                testcase_id="healthy_case",
                correct_verdict=True,
                correct_device=True,
                correct_interface=True,
                correct_fault_type=True,
                score=1.0,
                details={
                    "difficulty": "easy",
                    "negative_sample": True,
                    "expected_verdict": "network_healthy",
                    "agent_verdict": "network_healthy",
                },
            ),
        ]

        report = evaluator.generate_report(results, "test_agent", "xs")
        summary = report["summary"]

        assert summary["detection_accuracy"] == 1.0
        assert summary["overall_accuracy"] == 0.5
        assert summary["device_accuracy"] == 0.0
        assert summary["fault_type_accuracy"] == 0.0
        assert summary["interface_applicable_cases"] == 1
        assert summary["correct_interface"] == 0
        assert summary["negative_sample_cases"] == 1
        assert summary["positive_sample_cases"] == 1

    def test_generate_report_device_localization_requires_correct_verdict(self):
        """Primary KPI should require both correct verdict and correct device."""
        evaluator = Evaluator()

        results = [
            EvaluationResult(
                testcase_id="fault_case_wrong_verdict",
                correct_verdict=False,
                correct_device=True,
                correct_interface=True,
                correct_fault_type=True,
                score=0.0,
                details={
                    "difficulty": "medium",
                    "ground_truth": {
                        "fault_type": "packet_loss",
                        "location": {"device": "leaf1", "interface": "Ethernet0"},
                    },
                    "agent_output": {"verdict": "inconclusive"},
                },
            ),
        ]

        report = evaluator.generate_report(results, "test_agent", "xs")
        summary = report["summary"]

        assert summary["device_localization_rate"] == 0.0
        assert summary["device_accuracy"] == 0.0
        assert summary["interface_localization_rate"] == 0.0

    def test_evaluate_route_policy_alias_as_correct_fault_type(self):
        """Route-origination style answers should normalize to route_policy_misconfig."""
        evaluator = Evaluator()

        agent_output = AgentOutput(
            verdict="fault_detected",
            fault_type="route_origination_missing",
            location={"device": "leaf1"},
            confidence=0.9,
        )

        ground_truth = {
            "fault_type": "route_policy_misconfig",
            "location": {"device": "leaf1"},
        }

        result = evaluator.evaluate(agent_output, ground_truth, "test_route_policy_alias")

        assert result.correct_verdict is True
        assert result.correct_device is True
        assert result.correct_fault_type is True


class TestScenarioBenchmarking:
    """Tests for scenario-centric benchmark helpers."""

    def test_scenario_parser_rejects_unsupported_fault_type(self):
        """Scenario validation should fail for unsupported fault types."""
        invalid_yaml = """
scenario_id: invalid_fault_case
name: "Invalid fault case"
description: "invalid"
topology_scale: xs
traffic_profile: standard
metadata:
  difficulty: easy
  expected_diagnosis: link_down
episodes:
  - episode_id: ep1
    description: bad fault
    fault_type: made_up_fault
    target_device: spine1
"""

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as tmp:
            tmp.write(invalid_yaml)
            tmp_path = tmp.name

        try:
            scenario = parse_scenario_file(tmp_path)
            errors = validate_scenario(scenario)
            assert any("Unsupported fault_type" in err for err in errors)
        finally:
            os.unlink(tmp_path)

    def test_score_scenario_fault_episodes_only_scores_faults(self):
        """Only non-none episodes should be scored."""
        scenario = parse_scenario_file(_generated_scenario_path("generated_link_down_xs_001.yaml"))
        evaluator = Evaluator()

        scenario_result = {
            "scenario_id": scenario.scenario_id,
            "episodes": [
                {
                    "episode": {
                        "episode_id": "ep001_baseline",
                        "fault_type": "none",
                        "target_device": "spine1",
                        "target_interface": None,
                    }
                },
                {
                    "episode": {
                        "episode_id": "ep002_link_down",
                        "fault_type": "link_down",
                        "target_device": "spine1",
                        "target_interface": "Ethernet0",
                    },
                    "diagnosis": {
                        "verdict": "fault_detected",
                        "fault_type": "link_down",
                        "location": {"device": "spine1", "interface": "Ethernet0"},
                        "confidence": 0.95,
                        "tool_calls": [],
                        "time_taken_seconds": 1.0,
                    },
                },
            ],
        }

        scored = score_scenario_fault_episodes(scenario, scenario_result, evaluator)
        assert len(scored) == 1
        assert scored[0].testcase_id == f"{scenario.scenario_id}:ep002_link_down"
        assert scored[0].score == 1.0

    def test_resolve_scenario_files_from_directory(self):
        """Scenario file resolver should return sorted YAML files by pattern."""
        with tempfile.TemporaryDirectory() as tmpdir:
            p1 = os.path.join(tmpdir, "scenario_02.yaml")
            p2 = os.path.join(tmpdir, "scenario_01.yaml")
            p3 = os.path.join(tmpdir, "note.txt")
            for path in [p1, p2, p3]:
                with open(path, "w") as f:
                    f.write("test")

            files = resolve_scenario_files(tmpdir)
            assert files == sorted([p1, p2])

    def test_topology_guard_rejects_scale_mismatch_by_default(self):
        """Strict topology guard should fail on declared/actual scale mismatch."""
        scenario = parse_scenario_file(_generated_scenario_path("generated_link_down_xs_001.yaml"))

        with tempfile.TemporaryDirectory() as tmpdir:
            metadata = {
                "name": "dcn",
                "scale": {"total_clients": 8},
                "devices": {
                    "spines": [{"name": "spine1"}, {"name": "spine2"}],
                    "leafs": [{"name": "leaf1"}, {"name": "leaf2"}],
                    "clients": [{"name": "client1"}, {"name": "client2"}],
                },
            }

            with open(os.path.join(tmpdir, "topology.json"), "w") as f:
                json.dump(metadata, f)

            os.makedirs(os.path.join(tmpdir, "configs"), exist_ok=True)
            with open(os.path.join(tmpdir, "configs", "spine1.sh"), "w") as f:
                f.write("config interface startup Ethernet0\n")

            result = validate_scenario_topology(
                scenario=scenario,
                topology_dir=tmpdir,
            )

            assert result["status"] == "fail"
            assert result["declared_scale"] == scenario.topology_scale
            assert result["actual_scale"] == "small"

    def test_topology_guard_accepts_e_style_interface_aliases(self):
        """Legacy e1-1 style interface labels should map to SONiC Ethernet ports."""
        scenario = parse_scenario_file(_generated_scenario_path("generated_link_down_medium_001.yaml"))

        with tempfile.TemporaryDirectory() as tmpdir:
            metadata = {
                "name": "dcn",
                "scale": {"total_clients": 16},
                "devices": {
                    "spines": [{"name": f"spine{i}"} for i in range(1, 5)],
                    "leafs": [{"name": f"leaf{i}"} for i in range(1, 9)],
                    "clients": [{"name": f"client{i}"} for i in range(1, 17)],
                },
            }

            with open(os.path.join(tmpdir, "topology.json"), "w") as f:
                json.dump(metadata, f)

            os.makedirs(os.path.join(tmpdir, "configs"), exist_ok=True)
            with open(os.path.join(tmpdir, "configs", "spine1.sh"), "w") as f:
                f.write("config interface startup Ethernet0\n")

            result = validate_scenario_topology(
                scenario=scenario,
                topology_dir=tmpdir,
            )

            assert result["status"] == "pass"

    def test_topology_guard_accepts_vendor_style_interface_aliases(self):
        """Vendor-style ethernet-1/1 labels should also map to SONiC Ethernet ports."""
        scenario = parse_scenario_file(_generated_scenario_path("generated_link_down_small_001.yaml"))

        with tempfile.TemporaryDirectory() as tmpdir:
            metadata = {
                "name": "dcn",
                "scale": {"total_clients": 8},
                "devices": {
                    "spines": [{"name": "spine1"}, {"name": "spine2"}],
                    "leafs": [{"name": f"leaf{i}"} for i in range(1, 5)],
                    "clients": [{"name": f"client{i}"} for i in range(1, 9)],
                },
            }

            with open(os.path.join(tmpdir, "topology.json"), "w") as f:
                json.dump(metadata, f)

            os.makedirs(os.path.join(tmpdir, "configs"), exist_ok=True)
            with open(os.path.join(tmpdir, "configs", "spine1.sh"), "w") as f:
                f.write("config interface startup Ethernet0\n")
            with open(os.path.join(tmpdir, "configs", "spine2.sh"), "w") as f:
                f.write("config interface startup Ethernet4\n")

            result = validate_scenario_topology(
                scenario=scenario,
                topology_dir=tmpdir,
            )

            assert result["status"] == "pass"

    def test_interface_alias_helper_keeps_scale_agnostic_equivalence(self):
        assert are_interfaces_equivalent("e1-1", "Ethernet0") is True
        assert are_interfaces_equivalent("ethernet-1/2", "Ethernet4") is True
        assert are_interfaces_equivalent("eth3", "Ethernet8") is True

    def test_score_scenario_fault_episodes_accepts_link_peer_equivalence(self):
        scenario = parse_scenario_file(_generated_scenario_path("generated_link_down_xs_001.yaml"))
        evaluator = Evaluator()
        scenario_result = {
            "episodes": [
                {
                    "episode": {
                        "episode_id": "ep002_link_down",
                        "fault_type": "link_down",
                        "target_device": "spine1",
                        "target_interface": "e1-1",
                    },
                    "diagnosis": {
                        "verdict": "fault_detected",
                        "fault_type": "link_down",
                        "location": {"device": "leaf1", "interface": "Ethernet0"},
                        "confidence": 0.9,
                        "tool_calls": [],
                        "time_taken_seconds": 1.0,
                    },
                }
            ]
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            os.makedirs(os.path.join(tmpdir, "configs"), exist_ok=True)
            with open(os.path.join(tmpdir, "configs", "spine1.sh"), "w") as f:
                f.write("config interface ip add Ethernet0 192.168.11.1/30\n")
            with open(os.path.join(tmpdir, "configs", "leaf1.sh"), "w") as f:
                f.write("config interface ip add Ethernet0 192.168.11.2/30\n")

            scored = score_scenario_fault_episodes(
                scenario,
                scenario_result,
                evaluator,
                topology_dir=tmpdir,
            )

        assert len(scored) == 1
        assert scored[0].correct_device is True
        assert scored[0].correct_interface is True
        assert scored[0].score == 1.0

    @pytest.mark.parametrize("fault_type", ["packet_loss", "packet_corruption", "high_latency", "mtu_mismatch"])
    def test_score_scenario_interface_symmetric_fault_accepts_peer_endpoint(self, fault_type):
        """Interface-level faults should accept the link-peer endpoint as an equivalent answer."""
        from netopsbench.platform.session.scoring import build_episode_ground_truth

        episode_info = {
            "episode_id": "ep002_fault",
            "fault_type": fault_type,
            "target_device": "leaf1",
            "target_interface": "Ethernet0",
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            os.makedirs(os.path.join(tmpdir, "configs"), exist_ok=True)
            # leaf1 Ethernet0 and spine1 Ethernet0 share the same /30 subnet
            with open(os.path.join(tmpdir, "configs", "leaf1.sh"), "w") as f:
                f.write("config interface ip add Ethernet0 10.0.0.1/30\n")
            with open(os.path.join(tmpdir, "configs", "spine1.sh"), "w") as f:
                f.write("config interface ip add Ethernet0 10.0.0.2/30\n")

            gt = build_episode_ground_truth(episode_info, topology_dir=tmpdir)

        assert (
            "equivalent_locations" in gt
        ), f"{fault_type} should produce equivalent_locations for symmetric interface fault"
        peer = gt["equivalent_locations"][0]
        assert peer["device"] == "spine1"
        assert peer["interface"] == "Ethernet0"


class TestEndToEnd:
    """End-to-end integration tests."""

    def test_full_flow_without_containers(self):
        """Test the full benchmark flow (without actual containers)."""
        # 1. Generate topology
        with tempfile.TemporaryDirectory() as tmpdir:
            topo_result = generate_topology("xs", tmpdir)
            assert topo_result["metadata"] is not None

            # 2. Initialize components with topology
            metadata = topo_result["metadata"]
            toolkit = AgentToolkit(topology_metadata=metadata)
            _injector = FaultInjector(topology_metadata=metadata)
            evaluator = Evaluator()

            # 3. Get topology (should work)
            topo = toolkit.get_topology()
            assert topo.success
            assert topo.data["scale"]["num_spines"] == 2
            # 4. Simulate agent output (mock)
            agent_output = AgentOutput(
                verdict="fault_detected",
                fault_type="link_down",
                location={"device": "spine1", "interface": "Ethernet0"},
                confidence=0.95,
                reasoning="Mock agent correctly identified link down",
            )

            # 5. Evaluate
            ground_truth = {"fault_type": "link_down", "location": {"device": "spine1", "interface": "Ethernet0"}}
            result = evaluator.evaluate(agent_output, ground_truth, "test_link_down")
            assert result.score == 1.0

            # 6. Generate report
            report = evaluator.generate_report([result], "mock_agent", "xs")
            assert report["summary"]["total_cases"] == 1
            assert report["summary"]["average_score"] == 1.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
