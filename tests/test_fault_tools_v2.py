"""Optional tool views preserve the registry, output scope and transport limits."""

import json
import subprocess
import time

import pytest

from netopsbench.platform.toolkit._core.control import tool_deadline_scope
from netopsbench.platform.toolkit._core.device.validators import docker_exec
from netopsbench.platform.toolkit.mcp.registry import load_tool_specs, tool_schemas, validate_tool_call
from netopsbench.platform.toolkit.toolkit import AgentToolkit
from netopsbench.platform.topology.clos_builder import build_clos_plan
from netopsbench.platform.topology.config import config_for_scale


@pytest.fixture
def toolkit():
    return AgentToolkit(topology_metadata=build_clos_plan(config_for_scale("medium")).manifest.model_dump(mode="json"))


def test_only_one_new_tool_and_optional_contracts():
    names = {tool.name for tool in load_tool_specs()}
    assert len(names) == 16
    assert "get_device_status" in names
    assert "get_pingmesh_flows" not in names
    assert validate_tool_call("traceroute", {"src": "client1", "dst_ip": "192.168.2.2", "max_hops": 32})
    assert validate_tool_call("get_route_table", {"device": "leaf1", "view": "kernel"})
    assert validate_tool_call("get_bgp_neighbor", {"device": "leaf1", "peer": "10.0.0.1", "view": "advertised_routes"})


def test_acl_collection_failure_is_not_an_empty_healthy_ruleset(toolkit, monkeypatch):
    calls = []

    def failed(container, args, timeout):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="permission denied")

    monkeypatch.setattr(toolkit, "_docker_exec", failed)
    result = toolkit.get_device_acl("leaf1")
    assert not result.success
    assert len(calls) == 3
    assert len(result.data["collection_errors"]) == 3
    assert result.data["collected_at"]


def test_interface_schema_does_not_expose_injection_state():
    schema = next(tool for tool in tool_schemas() if tool["name"] == "get_device_interfaces")
    assert "include_tc" not in schema["input_schema"]["properties"]
    assert validate_tool_call("get_device_interfaces", {"device": "leaf1"})
    assert not validate_tool_call(
        "get_device_interfaces", {"device": "leaf1", "interface": "Ethernet0", "include_tc": True}
    )


@pytest.mark.parametrize("interface", [None, "Ethernet0"])
def test_legacy_tc_request_fails_before_device_access(toolkit, monkeypatch, interface):
    def unexpected(*args, **kwargs):
        pytest.fail("A rejected injection-state query must not access the device")

    monkeypatch.setattr(toolkit, "_docker_exec", unexpected)
    with pytest.raises(TypeError, match="include_tc"):
        toolkit.get_device_interfaces("leaf1", interface=interface, include_tc=True)


@pytest.mark.parametrize("format", ["summary", "structured", "raw", "both"])
def test_interface_views_preserve_observations_without_tc(toolkit, monkeypatch, format):
    def run(container, args, timeout):
        if args == ["ip", "-j", "-4", "addr", "show"]:
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=json.dumps([{"ifname": "eth1", "addr_info": [{"local": "192.168.1.2", "prefixlen": 30}]}]),
            )
        assert args == ["ip", "-s", "link", "show"]
        return subprocess.CompletedProcess(
            args,
            0,
            stdout="2: eth1: <UP> mtu 1500 qdisc netem state UP\n"
            "    RX: bytes packets errors dropped\n    1000 10 2 1\n",
        )

    monkeypatch.setattr(toolkit, "_docker_exec", run)
    result = toolkit.get_device_interfaces("client1", format=format)
    assert result.success
    assert result.data["addresses"] == {"eth1": ["192.168.1.2/30"]}
    assert "qdisc" not in json.dumps(result.data)
    assert "netem" not in json.dumps(result.data)
    assert result.data["collected_at"]
    if format in {"summary", "structured"}:
        assert result.data["interfaces"][0]["rx_errors"] == 2


def test_kernel_fib_has_actual_multipath_and_prefix_filter(toolkit, monkeypatch):
    calls = []

    def run(container, args, timeout):
        calls.append(args)
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=json.dumps(
                [{"dst": "192.168.2.0/30", "nexthops": [{"gateway": "10.0.0.1"}, {"gateway": "10.0.0.2"}]}]
            ),
        )

    monkeypatch.setattr(toolkit, "_docker_exec", run)
    result = toolkit.get_route_table("leaf1", prefix="192.168.2.0/30", view="kernel")
    assert result.success
    assert len(result.data["routes"][0]["nexthops"]) == 2
    assert calls == [["ip", "-j", "-4", "route", "show", "exact", "192.168.2.0/30"]]


def test_advertised_routes_filtered_locally_with_one_command(toolkit, monkeypatch):
    calls = []

    def run(container, args, timeout):
        calls.append(args)
        return subprocess.CompletedProcess(
            args, 0, stdout=json.dumps({"advertisedRoutes": {"192.168.1.0/30": {}, "192.168.2.0/30": {}}})
        )

    monkeypatch.setattr(toolkit, "_docker_exec", run)
    result = toolkit.get_bgp_neighbor("leaf1", "10.0.0.1", view="advertised_routes", prefix="192.168.2.0/30")
    assert result.success
    assert set(result.data["advertised_routes"]) == {"192.168.2.0/30"}
    assert len(calls) == 1


def test_pingmesh_client_filter_preserves_aggregation_and_window(toolkit, monkeypatch):
    calls = []
    monkeypatch.setattr(toolkit, "_query_influx_rows", lambda query, **kw: calls.append(query) or [])
    toolkit.set_pingmesh_time_window("2026-10-12T00:00:00Z", "2026-10-12T00:01:00Z")
    result = toolkit.get_pingmesh_summary(src_client="client1", dst_client="client2")
    assert result.success
    assert 'r.src_name == "client1"' in calls[0]
    assert 'r.dst_name == "client2"' in calls[0]
    assert 'group(columns: ["path_type", "_field"])' in calls[0]
    assert "2026-10-12T00:00:00Z" in calls[0]
    assert not toolkit.get_pingmesh_summary(src_client="leaf1").success


def test_prefix_count_mode_ignores_session_state_filter(toolkit, monkeypatch):
    queries = []
    monkeypatch.setattr(toolkit, "_query_influx_rows", lambda query, **kw: queries.append(query) or [])
    result = toolkit.query_bgp_events(event_type="prefix_count", state="not_a_session_state")
    assert result.success
    assert "bgp_prefix_events" in queries[0]
    assert "session_state ==" not in queries[0]
    assert not toolkit.query_bgp_events(state="not_a_session_state").success


def test_traceroute_hop_limit(toolkit, monkeypatch):
    calls = []

    def run(container, args, timeout):
        calls.append((args, timeout))
        return subprocess.CompletedProcess(args, 0, stdout="trace")

    monkeypatch.setattr(toolkit, "_docker_exec", run)
    assert toolkit.traceroute("client1", "192.168.2.2").success
    assert calls[0][0][-2] == "8" and calls[0][1] == 12
    assert toolkit.traceroute("client1", "192.168.2.2", max_hops=32).success
    assert not toolkit.traceroute("client1", "192.168.2.2", max_hops=33).success


def test_transport_receives_total_session_deadline(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "netopsbench.platform.toolkit._core.device.validators.safe_run", lambda *a, **kw: calls.append(kw)
    )
    deadline = time.monotonic() + 0.2
    with tool_deadline_scope(deadline):
        docker_exec("container", ["traceroute"], timeout=36)
    assert calls[0]["control"].deadline == deadline
    with pytest.raises(TimeoutError):
        with tool_deadline_scope(time.monotonic() - 1):
            docker_exec("container", ["true"], timeout=30)


@pytest.mark.parametrize("view", ["advertised_routes", "advertised-routes", "advertised"])
def test_bgp_advertised_aliases_use_canonical_output(toolkit, monkeypatch, view):
    from netopsbench.platform.toolkit.mcp import inventory

    calls = []
    monkeypatch.setattr(inventory, "get_toolkit", lambda: toolkit)

    def run(container, args, timeout):
        calls.append(args)
        return subprocess.CompletedProcess(
            args, 0, stdout=json.dumps({"advertisedRoutes": {"192.168.2.0/30": {}, "192.168.3.0/30": {}}})
        )

    monkeypatch.setattr(toolkit, "_docker_exec", run)
    result = inventory.get_bgp_neighbor("leaf1", "10.0.0.1", view=view, prefix="192.168.2.0/30")
    assert result["view"] == "advertised_routes"
    assert result["advertised_routes"] == {"192.168.2.0/30": {}}
    assert calls == [["vtysh", "-c", "show bgp ipv4 unicast neighbors 10.0.0.1 advertised-routes json"]]


def test_bgp_json_format_is_structured(toolkit, monkeypatch):
    monkeypatch.setattr(toolkit, "_docker_exec", lambda *args, **kwargs: subprocess.CompletedProcess([], 0, stdout=""))
    assert (
        toolkit.get_bgp_neighbors("leaf1", "json").to_dict()
        == toolkit.get_bgp_neighbors("leaf1", "structured").to_dict()
    )


@pytest.mark.parametrize("view", ["received-routes", "received_routes", "/private/fault.yml"])
def test_bgp_invalid_view_has_safe_guidance_without_device_access(toolkit, monkeypatch, view):
    from netopsbench.platform.toolkit.mcp import inventory
    from netopsbench.platform.toolkit.public import public_tool_result

    monkeypatch.setattr(inventory, "get_toolkit", lambda: toolkit)
    monkeypatch.setattr(toolkit, "_resolve_container", lambda *args: pytest.fail("Invalid view accessed device"))
    result = public_tool_result(toolkit.get_bgp_neighbor("leaf1", "10.0.0.1", view=view))
    assert not result["success"]
    assert "use detail or advertised_routes" in result["error"]
    assert "Received routes are not supported" in result["error"]
    assert "/private" not in result["error"]
    assert inventory.get_bgp_neighbor("leaf1", "10.0.0.1", view=view) == {"error": result["error"]}
    assert not validate_tool_call("get_bgp_neighbor", {"device": "leaf1", "peer": "10.0.0.1", "view": view})


def test_bgp_invalid_format_and_collection_error_are_distinct(toolkit, monkeypatch):
    from netopsbench.platform.toolkit.mcp import inventory

    monkeypatch.setattr(inventory, "get_toolkit", lambda: toolkit)
    monkeypatch.setattr(
        toolkit,
        "_docker_exec",
        lambda *args, **kwargs: subprocess.CompletedProcess([], 1, stdout="", stderr="/private/fault.yml corrupt 20%"),
    )
    assert "use structured, raw or both" in inventory.get_bgp_neighbors("leaf1", format="invalid")["error"]
    assert inventory.get_bgp_neighbors("leaf1") == {"error": "Observation unavailable"}
    assert inventory.get_bgp_neighbor("leaf1", "10.0.0.1", view="advertised") == {"error": "Observation unavailable"}


def test_bgp_schema_lists_supported_values():
    schemas = {s["name"]: s for s in tool_schemas()}
    assert schemas["get_bgp_neighbor"]["input_schema"]["properties"]["view"]["enum"] == [
        "detail",
        "advertised_routes",
        "advertised-routes",
        "advertised",
    ]
    assert schemas["get_bgp_neighbors"]["input_schema"]["properties"]["format"]["enum"] == [
        "structured",
        "raw",
        "both",
        "json",
    ]


@pytest.mark.parametrize("with_gateway", [False, True])
def test_bgp_fastmcp_schema_matches_registry(with_gateway):
    import asyncio

    from netopsbench.platform.toolkit.fastmcp_server import create_server

    server = create_server(gateway=object() if with_gateway else None)
    registered = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    schemas = {schema["name"]: schema for schema in tool_schemas()}
    for name, argument in [("get_bgp_neighbor", "view"), ("get_bgp_neighbors", "format")]:
        assert (
            registered[name].parameters["properties"][argument]["enum"]
            == schemas[name]["input_schema"]["properties"][argument]["enum"]
        )
