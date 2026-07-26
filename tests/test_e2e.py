#!/usr/bin/env python3
"""
End-to-end tests for NetOpsBench benchmark system.

These tests verify the complete benchmark flow works correctly.
"""

import inspect
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from netopsbench.evaluator.scorer import AgentOutput, EvaluationResult, Evaluator
from netopsbench.models.profiles import supported_scales
from netopsbench.models.scenario import EpisodeSpec, ScenarioSpec
from netopsbench.models.topology import TopologyManifest
from netopsbench.platform.client_agent.config import build_client_agent_config
from netopsbench.platform.faults.injector import FaultInjector
from netopsbench.platform.faults.specs import create_fault_registry
from netopsbench.platform.scenario.generator import parse_bgp_config, parse_network_interfaces
from netopsbench.platform.scenario.parser import parse_scenario_file
from netopsbench.platform.scenario.validator import validate_scenario, validate_scenario_topology
from netopsbench.platform.session.scoring import score_scenario_episode
from netopsbench.platform.toolkit import fastmcp_server
from netopsbench.platform.toolkit.mcp.registry import load_tool_specs, tool_schemas, validate_tool_call

# Internal test path: direct toolkit import keeps implementation-level e2e checks fast.
from netopsbench.platform.toolkit.toolkit import AgentToolkit, ToolResult
from netopsbench.platform.topology.generator import generate_topology
from netopsbench.platform.utils.interface_names import are_interfaces_equivalent


def _generated_scenario_path(filename: str) -> str:
    generated_root = Path("scenarios/generated")
    if not generated_root.exists():
        pytest.skip("scenarios/generated not found; generate scenarios before running this test")
    matches = sorted(generated_root.rglob(filename))
    if not matches:
        pytest.skip(f"{filename} not found under scenarios/generated; generate matching scenarios first")
    return str(matches[0])


def _write_config_db(topology_dir: str | Path, device: str, interfaces: dict[str, list[str]]) -> Path:
    interface_table: dict[str, dict] = {}
    for interface_name, cidrs in interfaces.items():
        interface_table[interface_name] = {}
        for cidr in cidrs:
            interface_table[f"{interface_name}|{cidr}"] = {}

    path = Path(topology_dir) / "configs" / "sonic" / device / "config_db.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"INTERFACE": interface_table}), encoding="utf-8")
    return path


def _generated_metadata(scale: str = "xs") -> dict:
    with tempfile.TemporaryDirectory() as tmpdir:
        return generate_topology(scale, tmpdir)["metadata"]


class TestTopologyGeneration:
    """Tests for topology generation."""

    @pytest.mark.parametrize("scale", supported_scales())
    def test_all_scales_use_bind_based_preseed_artifacts(self, scale):
        """Every scale should use the same bind-mounted SONiC startup artifact path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = generate_topology(scale, tmpdir)
            rendered = yaml.safe_load(Path(result["yaml_file"]).read_text(encoding="utf-8"))

            binds = rendered["topology"]["kinds"]["sonic-vs"]["binds"]
            linux_binds = rendered["topology"]["kinds"]["linux"]["binds"]
            assert "configs/sonic/__clabNodeName__/config_db.json:/etc/sonic/config_db.json:rw" in binds
            assert (
                "configs/sonic/__clabNodeName__/port_config.ini:"
                "/usr/share/sonic/device/x86_64-kvm_x86_64-r0/Force10-S6000/port_config.ini:rw"
            ) in binds
            assert (
                "configs/sonic/__clabNodeName__/lanemap.ini:"
                "/usr/share/sonic/device/x86_64-kvm_x86_64-r0/Force10-S6000/lanemap.ini:rw"
            ) in binds
            assert "configs/sonic/start.sh:/usr/bin/start.sh:ro" in binds
            assert "configs/frr/__clabNodeName__.conf:/etc/frr/frr.conf:rw" in binds
            assert "configs/client-agent:/etc/netopsbench:ro" in linux_binds

            assert not list(Path(tmpdir, "configs").glob("*.sh"))
            assert not list(Path(tmpdir, "configs").glob("*.configdb.json"))
            client_agent_dir = Path(tmpdir, "configs", "client-agent")
            assert client_agent_dir.is_dir()
            assert (client_agent_dir / "client-agent.json").is_file()

            manifest = TopologyManifest.model_validate(result["metadata"])
            first_routing = manifest.routing_devices()[0].name
            first_attached = manifest.client_attached_devices()[0].name
            first_routing_config = Path(tmpdir, "configs", "sonic", first_routing, "config_db.json")
            first_attached_config = Path(tmpdir, "configs", "sonic", first_attached, "config_db.json")
            assert first_routing_config.exists()
            assert first_attached_config.exists()
            assert Path(tmpdir, "configs", "sonic", first_routing, "port_config.ini").exists()
            assert Path(tmpdir, "configs", "sonic", first_routing, "lanemap.ini").exists()
            start_wrapper = Path(tmpdir, "configs", "sonic", "start.sh")
            assert start_wrapper.exists()
            assert start_wrapper.stat().st_mode & stat.S_IXUSR
            wrapper_text = start_wrapper.read_text(encoding="utf-8")
            assert (
                "NETOPSBENCH_ORIGINAL_SONIC_START_SHA256="
                "8c5aa959f0a3ed0bf1a57f7ecfd004485d5600b9ab71b388c2b15e109b77ee12"
            ) in wrapper_text
            assert "wait_for_front_panel_links" in wrapper_text
            assert "/sys/class/net/eth[0-9]*" in wrapper_text
            assert "ip link show" not in wrapper_text
            assert "local timeout=600" in wrapper_text
            assert "local interval=1" in wrapper_text
            assert "install_generated_config_db" in wrapper_text
            assert 'cat "$src" > "$dst"' in wrapper_text
            assert Path(tmpdir, "configs", "frr", f"{first_routing}.conf").exists()

            routing_config = json.loads(first_routing_config.read_text(encoding="utf-8"))
            assert routing_config["DEVICE_METADATA"]["localhost"]["platform"] == "x86_64-kvm_x86_64-r0"
            assert routing_config["DEVICE_METADATA"]["localhost"]["mac"].startswith("02:")
            assert routing_config["PORT"]
            assert routing_config["INTERFACE"]
            assert str(first_routing_config) in result["config_files"]

            attached_config = json.loads(first_attached_config.read_text(encoding="utf-8"))
            assert (
                attached_config["DEVICE_METADATA"]["localhost"]["mac"]
                != routing_config["DEVICE_METADATA"]["localhost"]["mac"]
            )
            assert attached_config["PORT"]
            assert str(first_attached_config) in result["config_files"]

    def test_generated_switch_configs_seed_gnmi_defaults(self):
        """Generated SONiC startup configs should seed telemetry tables expected by 202505 images."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = generate_topology("xs", tmpdir)

            for config_path in result["config_files"]:
                payload = json.loads(Path(config_path).read_text(encoding="utf-8"))
                assert payload["GNMI"]["gnmi"]["port"] == "50051"
                assert payload["GNMI"]["gnmi"]["client_auth"] == "false"
                assert payload["GNMI"]["certs"]["server_key"].endswith("streamingtelemetryserver.key")
                assert payload["FLEX_COUNTER_TABLE"]["PORT"]["FLEX_COUNTER_STATUS"] == "enable"
                assert payload["FLEX_COUNTER_TABLE"]["PORT"]["POLL_INTERVAL"] == "10000"
                assert "telegraf" in payload["SYSLOG_SERVER"]

    def test_generate_xlarge_topology_metadata_and_addressing(self):
        """xlarge should fit the Clos address plans and avoid collector collisions."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = generate_topology("xlarge", tmpdir)

            metadata = result["agent_topology"]
            devices = metadata["devices"]
            assert metadata["topology_scale"] == "xlarge"
            assert metadata["management"]["ipv4_subnet"] == "172.20.20.0/23"
            assert metadata["scale"]["num_spines"] == 16
            assert metadata["scale"]["num_leafs"] == 128
            assert metadata["scale"]["clients_per_leaf"] == 1
            assert metadata["scale"]["total_clients"] == 128
            assert metadata["pingmesh"]["destination_batch_size"] == 16

            mgmt_ips = {item["mgmt_ip"] for role in ("spines", "leafs", "clients") for item in devices[role]}
            assert len(mgmt_ips) == 16 + 128 + 128
            assert metadata["collector"]["ipv4"] not in mgmt_ips

            spine16_config = json.loads(
                Path(tmpdir, "configs", "sonic", "spine16", "config_db.json").read_text(encoding="utf-8")
            )
            leaf128_config = json.loads(
                Path(tmpdir, "configs", "sonic", "leaf128", "config_db.json").read_text(encoding="utf-8")
            )
            spine16_ports = Path(tmpdir, "configs", "sonic", "spine16", "port_config.ini").read_text(encoding="utf-8")
            spine16_lanemap = Path(tmpdir, "configs", "sonic", "spine16", "lanemap.ini").read_text(encoding="utf-8")
            leaf128_frr = Path(tmpdir, "configs", "frr", "leaf128.conf").read_text(encoding="utf-8")
            spine16_frr = Path(tmpdir, "configs", "frr", "spine16.conf").read_text(encoding="utf-8")

            assert not Path(tmpdir, "configs", "spine16.sh").exists()
            assert not Path(tmpdir, "configs", "spine16.configdb.json").exists()
            assert "neighbor 10.16.128.2 remote-as 65138" in spine16_frr
            assert "bgp bestpath as-path multipath-relax" in spine16_frr
            assert "maximum-paths 64" in leaf128_frr
            assert len([key for key in spine16_config["INTERFACE"] if "|" not in key]) == 128
            assert spine16_config["INTERFACE"]["Ethernet508"] == {}
            assert spine16_config["INTERFACE"]["Ethernet508|10.16.128.1/30"] == {}
            assert spine16_config["PORT"]["Ethernet508"]["lanes"] == "509,510,511,512"
            assert "Ethernet508" in spine16_ports
            assert "eth1:1,2,3,4" in spine16_lanemap
            assert "eth128:509,510,511,512" in spine16_lanemap
            assert len([line for line in spine16_lanemap.splitlines() if line.startswith("eth")]) == 128
            assert len([key for key in leaf128_config["INTERFACE"] if "|" not in key]) == 17
            assert leaf128_config["INTERFACE"]["Ethernet60|10.16.128.2/30"] == {}
            assert leaf128_config["INTERFACE"]["Ethernet64|192.168.228.1/30"] == {}
            assert "network 192.168.228.0/30 route-map RM-ALLOW" in leaf128_frr
            leaf128_lanemap = Path(tmpdir, "configs", "sonic", "leaf128", "lanemap.ini").read_text(encoding="utf-8")
            assert len([line for line in leaf128_lanemap.splitlines() if line.startswith("eth")]) == 17

    def test_structured_startup_artifacts_drive_scenario_parsers(self):
        """Scenario parsers should use generated ConfigDB and FRR artifacts as the normal path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            generate_topology("xs", tmpdir)
            spine_cfg = Path(tmpdir, "configs", "sonic", "spine1", "config_db.json")
            spine_frr = Path(tmpdir, "configs", "frr", "spine1.conf")

            assert parse_network_interfaces(spine_cfg) == ["Ethernet0", "Ethernet4"]
            bgp_info = parse_bgp_config(spine_frr)
            assert bgp_info["local_as"] == 65001
            assert [neighbor["remote_as"] for neighbor in bgp_info["neighbors"]] == [65011, 65012]


class TestAgentToolkit:
    """Tests for agent toolkit."""

    def test_toolkit_initialization(self):
        """Test toolkit initializes correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            topo = generate_topology("xs", tmpdir)
            toolkit = AgentToolkit(topology_metadata=topo["metadata"])
            assert toolkit is not None
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


class TestClientAgentConfig:
    """Tests for the compact native client-agent configuration."""

    @pytest.mark.parametrize("scale", supported_scales())
    def test_config_scales_linearly_with_topology(self, scale):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = generate_topology(scale, tmpdir)
            manifest = TopologyManifest.model_validate(result["metadata"])
            payload = build_client_agent_config(manifest)

            assert len(payload["clients"]) == manifest.facts.total_clients
            assert len({client["name"] for client in payload["clients"]}) == len(payload["clients"])
            assert "probes" not in payload

    def test_xlarge_config_is_deterministic_and_bounded(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = generate_topology("xlarge", tmpdir)
            manifest = TopologyManifest.model_validate(result["metadata"])
            first = build_client_agent_config(manifest)
            second = build_client_agent_config(manifest)

            assert first == second
            assert len(first["clients"]) == 128
            assert len(json.dumps(first).encode()) < 100_000
            assert first["pingmesh_policy"]["destination_batch_size"] == 16
            assert first["pingmesh_policy"]["coverage_epoch_cycles"] == 32


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
            "get_bgp_neighbor",
            "get_route_table",
            "get_device_config",
            "get_bgp_rib",
            "get_device_acl",
            "get_device_logs",
            "traceroute",
            "ping_test",
            "get_interface_metrics",
            "query_bgp_events",
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

    def test_rl_schemas_and_fastmcp_use_identical_typed_contracts(self):
        schemas = {schema["name"]: schema for schema in tool_schemas()}
        specs = {spec.name: spec for spec in fastmcp_server._TOOL_SPECS}

        assert schemas.keys() == specs.keys()
        for name, spec in specs.items():
            parameters = {parameter for parameter in inspect.signature(spec.handler).parameters if parameter != "self"}
            assert set(schemas[name]["input_schema"]["properties"]) == parameters
            assert schemas[name]["input_schema"]["additionalProperties"] is False

    def test_tool_schema_defaults_are_valid_and_integer_arguments_are_strict(self):
        schemas = {schema["name"]: schema for schema in tool_schemas()}
        payload_size = schemas["ping_test"]["input_schema"]["properties"]["payload_size"]

        for schema in schemas.values():
            for prop in schema["input_schema"]["properties"].values():
                if prop.get("default", object()) is None:
                    assert any(option.get("type") == "null" for option in prop.get("anyOf", []))
        assert payload_size["default"] is None
        assert validate_tool_call("ping_test", {"src": "client1", "dst_ip": "192.0.2.1"})
        assert validate_tool_call(
            "ping_test",
            {"src": "client1", "dst_ip": "192.0.2.1", "payload_size": None},
        )
        assert not validate_tool_call(
            "ping_test",
            {"src": "client1", "dst_ip": "192.0.2.1", "count": "3"},
        )
        assert not validate_tool_call(
            "ping_test",
            {"src": "client1", "dst_ip": "192.0.2.1", "count": True},
        )
        assert validate_tool_call("get_device_logs", {"device": "leaf1", "severity": None})


class TestFaultInjector:
    """Tests for fault injection."""

    def test_injector_initialization(self):
        """Test fault injector initializes correctly."""
        injector = FaultInjector(topology_metadata=_generated_metadata())
        assert injector is not None
        assert injector.container_names["spine1"] == "clab-dcn-spine1"
        assert injector.active_faults == []

    def test_injector_loads_dynamic_topology(self):
        """Test injector can load dynamic topology."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = generate_topology("xs", tmpdir)
            metadata = result["metadata"]

            injector = FaultInjector(topology_metadata=metadata)

            assert "spine1" in injector.container_names
            assert "leaf1" in injector.container_names

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

        defined_faults = [spec.name for spec in create_fault_registry().get_builtin_specs()]
        for fault in expected_faults:
            assert fault in defined_faults, f"Fault type {fault} not defined"

    def test_inject_high_latency_uses_latency_ms_contract(self):
        injector = FaultInjector(topology_metadata=_generated_metadata())

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
        injector = FaultInjector(topology_metadata=_generated_metadata())

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
        injector = FaultInjector(topology_metadata=_generated_metadata())

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
        injector = FaultInjector(topology_metadata=_generated_metadata())

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
        injector = FaultInjector(topology_metadata=_generated_metadata())
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
        assert report["breakdown_by_fault_type"] == {
            "packet_loss": {"total": 1, "correct": 0, "score_sum": 0.0, "accuracy": 0.0, "avg_score": 0.0}
        }

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
episode:
  episode_id: diagnosis
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

    def test_score_scenario_episode_scores_the_canonical_episode(self):
        scenario = parse_scenario_file(_generated_scenario_path("generated_link_down_xs_001.yaml"))
        evaluator = Evaluator()

        scenario_result = {
            "scenario_id": scenario.scenario_id,
            "episode": {
                "episode": {
                    "episode_id": "diagnosis",
                    "fault_type": "link_down",
                    "target_device": scenario.episode.target_device,
                    "target_interface": scenario.episode.target_interface,
                },
                "diagnosis": {
                    "verdict": "fault_detected",
                    "fault_type": "link_down",
                    "location": {
                        "device": scenario.episode.target_device,
                        "interface": scenario.episode.target_interface,
                    },
                    "confidence": 0.95,
                    "tool_calls": [],
                    "time_taken_seconds": 1.0,
                },
            },
        }

        scored = score_scenario_episode(scenario, scenario_result, evaluator)
        assert len(scored) == 1
        assert scored[0].testcase_id == f"{scenario.scenario_id}:diagnosis"
        assert scored[0].score == 1.0

    def test_topology_guard_rejects_scale_mismatch_by_default(self):
        """Strict topology guard should fail on declared/actual scale mismatch."""
        scenario = parse_scenario_file(_generated_scenario_path("generated_link_down_xs_001.yaml"))

        with tempfile.TemporaryDirectory() as tmpdir:
            generate_topology("small", tmpdir)

            result = validate_scenario_topology(
                scenario=scenario,
                topology_dir=tmpdir,
            )

            assert result["status"] == "fail"
            assert result["declared_scale"] == scenario.topology_scale
            assert result["actual_scale"] == "small"

    def test_topology_guard_accepts_e_style_interface_aliases(self):
        """e1-1 style interface labels should map to SONiC Ethernet ports."""
        scenario = parse_scenario_file(_generated_scenario_path("generated_link_down_medium_001.yaml"))

        with tempfile.TemporaryDirectory() as tmpdir:
            generate_topology("medium", tmpdir)

            _write_config_db(tmpdir, "spine1", {"Ethernet0": []})

            result = validate_scenario_topology(
                scenario=scenario,
                topology_dir=tmpdir,
            )

            assert result["status"] == "pass"

    def test_topology_guard_accepts_vendor_style_interface_aliases(self):
        """Vendor-style ethernet-1/1 labels should also map to SONiC Ethernet ports."""
        scenario = parse_scenario_file(_generated_scenario_path("generated_link_down_small_001.yaml"))

        with tempfile.TemporaryDirectory() as tmpdir:
            generate_topology("small", tmpdir)

            _write_config_db(tmpdir, "spine1", {"Ethernet0": []})
            _write_config_db(tmpdir, "spine2", {"Ethernet4": []})

            result = validate_scenario_topology(
                scenario=scenario,
                topology_dir=tmpdir,
            )

            assert result["status"] == "pass"

    def test_configdb_payload_interfaces_are_used_by_runtime_helpers(self):
        """Runtime helpers read interface metadata from generated ConfigDB artifacts."""
        with tempfile.TemporaryDirectory() as tmpdir:
            generate_topology("small", tmpdir)
            spine_config = _write_config_db(tmpdir, "spine1", {"Ethernet4": ["10.0.0.2/30"]})
            _write_config_db(tmpdir, "leaf1", {"Ethernet4": ["10.0.0.1/30"]})

            scenario = ScenarioSpec(
                scenario_id="configdb_interface_case",
                name="ConfigDB interface case",
                topology_scale="small",
                metadata={"difficulty": "easy"},
                episode=EpisodeSpec(
                    episode_id="diagnosis",
                    fault_type="link_down",
                    target_device="spine1",
                    target_interface="e1-2",
                ),
            )
            result = validate_scenario_topology(scenario=scenario, topology_dir=tmpdir)
            assert result["status"] == "pass"
            assert parse_network_interfaces(spine_config) == ["Ethernet4"]

            from netopsbench.platform.session.scoring import build_episode_ground_truth

            ground_truth = build_episode_ground_truth(
                {"fault_type": "link_down", "target_device": "leaf1", "target_interface": "Ethernet4"},
                topology_dir=tmpdir,
            )
            assert ground_truth["equivalent_locations"] == [{"device": "spine2", "interface": "Ethernet0"}]

    def test_interface_alias_helper_keeps_scale_agnostic_equivalence(self):
        assert are_interfaces_equivalent("e1-1", "Ethernet0") is True
        assert are_interfaces_equivalent("ethernet-1/2", "Ethernet4") is True
        assert are_interfaces_equivalent("eth3", "Ethernet8") is True

    def test_score_scenario_episode_accepts_link_peer_equivalence(self):
        scenario = parse_scenario_file(_generated_scenario_path("generated_link_down_xs_001.yaml"))
        evaluator = Evaluator()
        scenario_result = {
            "episode": {
                "episode": {
                    "episode_id": "diagnosis",
                    "fault_type": "link_down",
                    "target_device": "spine1",
                    "target_interface": "Ethernet0",
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
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            generate_topology("xs", tmpdir)

            scored = score_scenario_episode(
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
            generate_topology("xs", tmpdir)

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
