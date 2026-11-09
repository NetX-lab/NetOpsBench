"""Public evidence remains useful without runtime injection metadata."""

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

import pytest

from netopsbench.platform.incident.context import build_public_symptoms
from netopsbench.platform.toolkit._core.common import ToolResult
from netopsbench.platform.toolkit.mcp.registry import load_tool_specs
from netopsbench.platform.toolkit.public import public_tool_result
from netopsbench.platform.toolkit.toolkit import AgentToolkit
from netopsbench.platform.topology.clos_builder import build_clos_plan
from netopsbench.platform.topology.config import config_for_scale
from netopsbench.platform.utils.measurements import count_operation, measurement_scope


@pytest.fixture
def toolkit():
    return AgentToolkit(topology_metadata=build_clos_plan(config_for_scale("xs")).manifest.model_dump(mode="json"))


@pytest.mark.parametrize("spec", load_tool_specs(), ids=lambda spec: spec.name)
def test_every_mcp_handler_projects_private_fields_and_errors(monkeypatch, spec):
    import inspect
    from types import SimpleNamespace

    from netopsbench.platform.toolkit.mcp import context

    evidence = {"state": "down", "mtu": 1400, "config": "neighbor 10.0.0.1 shutdown"}
    payload = {
        "evidence": [dict(evidence, injection={"corrupt": 20}, recovery_journal={"private": True})],
        "collection_errors": [{"error": "/private/campaign/packet_corruption.yml"}],
        "service_collection_error": "/private/runtime/packet_corruption",
    }
    fake = SimpleNamespace(**{spec.name: lambda **kwargs: ToolResult(True, payload)})
    monkeypatch.setattr(context, "_toolkit", fake)
    args = {
        name: "device"
        for name, parameter in inspect.signature(spec.handler).parameters.items()
        if parameter.default is inspect.Parameter.empty
    }
    result = spec.handler(**args)
    assert result["evidence"] == [evidence]
    assert "private" not in json.dumps(result)
    assert "corrupt" not in json.dumps(result)
    assert result["collection_errors"][0]["error"] == "Observation unavailable"


@pytest.mark.parametrize(
    "returncode,stdout,stderr,success",
    [
        (1, "", "Error response from daemon: /private/runtime", False),
        (126, "OCI runtime exec failed: /private/runtime", "", False),
        (1, "5 packets transmitted, 0 received, 100% packet loss", "", True),
        (2, "", "ping: local error: message too long, mtu=1400", True),
    ],
)
def test_ping_keeps_probe_evidence_but_not_runtime_errors(toolkit, monkeypatch, returncode, stdout, stderr, success):
    monkeypatch.setattr(
        toolkit, "_docker_exec", lambda *a, **kw: subprocess.CompletedProcess([], returncode, stdout, stderr)
    )
    result = toolkit.ping_test("client1", "192.168.1.1")
    assert result.success is success
    assert "/private" not in json.dumps(public_tool_result(result))
    if success:
        assert result.data["output"] == (stdout or stderr)


@pytest.mark.parametrize("view", ["raw", "both", "summary", "structured"])
@pytest.mark.parametrize("device", ["client1", "leaf1"])
def test_interface_filter_and_qdisc_projection_on_linux_fallback(toolkit, monkeypatch, view, device):
    def execute(container, args, timeout):
        if args[0] == "bash":
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="unavailable")
        if args == ["ip", "-j", "-4", "addr", "show"]:
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=json.dumps(
                    [
                        {"ifname": "eth1", "addr_info": [{"local": "192.168.1.1", "prefixlen": 30}]},
                        {"ifname": "eth2", "addr_info": [{"local": "192.168.2.1", "prefixlen": 30}]},
                    ]
                ),
            )
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=(
                "2: eth1@if20: <UP> mtu 1400 qdisc netem state UP\n"
                "    RX: bytes packets errors dropped\n    100 10 2 1\n"
                "3: eth2: <UP> mtu 1500 qdisc noqueue state UP\n"
                "    RX: bytes packets errors dropped\n    200 20 0 0\n"
            ),
        )

    monkeypatch.setattr(toolkit, "_docker_exec", execute)
    result = toolkit.get_device_interfaces(device, format=view, interface="Ethernet0")
    assert result.success, result.error
    serialized = json.dumps(result.data)
    assert "netem" not in serialized and "qdisc" not in serialized and "eth2" not in serialized
    assert result.data["addresses"] == {"eth1": ["192.168.1.1/30"]}
    if view in {"summary", "structured"}:
        assert result.data["interfaces"] == [
            {
                "name": "eth1",
                "mtu": 1400,
                "state": "UP",
                "admin": "up",
                **(
                    {"rx_errors": 2, "rx_dropped": 1}
                    if view == "summary"
                    else {"rx_bytes": 100, "rx_packets": 10, "rx_errors": 2, "rx_dropped": 1}
                ),
            }
        ]
    assert not toolkit.get_device_interfaces(device, interface="Ethernet120").success


def test_initial_nested_audits_and_semantic_episode_id_are_private():
    result = build_public_symptoms(
        episode_result={
            "episode": {"episode_id": "packet_corruption_leaf1", "fault_type": "packet_corruption"},
            "observations": {
                "data_source_status": "error: /private/campaign/packet_corruption.yml",
                "pingmesh_metrics": {
                    "summary": {"packet_loss": 20},
                    "extra": {"target_device": "leaf1", "effect_audit": {"private": True}},
                },
            },
        },
        pingmesh_query_window={},
    )
    assert result["episode"] == {}
    assert result["observations"]["pingmesh_metrics"]["summary"] == {"packet_loss": 20}
    assert "leaf1" not in json.dumps(result)
    assert result["observations"]["data_source_status"] == "error"
    assert "/private" not in json.dumps(result)


def test_topology_projects_only_supported_metadata(toolkit):
    toolkit.manifest.devices[0].metadata.update(pod=1, injection={"corrupt": 20}, notes="/private/fault.yml")
    result = toolkit.get_topology().data
    assert "corrupt" not in json.dumps(result)
    assert "/private" not in json.dumps(result)


@pytest.mark.parametrize("view", ["raw", "both", "summary", "structured"])
def test_real_non_port_interface_is_not_mistaken_for_missing(toolkit, monkeypatch, view):
    def execute(container, args, timeout):
        if args[0] == "bash":
            output = "Interface    Admin    Oper\nEthernet0    up    up\n"
        elif args[:3] == ["ip", "-j", "-4"]:
            output = '[{"ifname":"lo", "addr_info":[{"local":"127.0.0.1", "prefixlen":8}]}]'
        else:
            output = "1: lo: <LOOPBACK,UP> mtu 65536 qdisc noqueue state UNKNOWN\n"
        return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")

    monkeypatch.setattr(toolkit, "_docker_exec", execute)
    result = toolkit.get_device_interfaces("leaf1", interface="lo", format=view)
    assert result.success
    assert result.data["addresses"] == {"lo": ["127.0.0.1/8"]}
    assert "Ethernet0" not in json.dumps(result.data)
    assert "qdisc" not in json.dumps(result.data)


def test_failed_tool_does_not_return_internal_exception():
    result = public_tool_result(ToolResult(False, None, "/private/packet_corruption.yaml: corrupt 20%"))
    assert result == {"success": False, "data": None, "error": "Observation unavailable"}


def test_operation_counts_include_propagated_threads_without_cross_case_leakage():
    first, second = {}, {}
    with measurement_scope(first):
        count_operation("influx_queries")
        with ThreadPoolExecutor(1) as pool:
            pool.submit(copy_context().run, count_operation, "docker_commands").result()
    with measurement_scope(second):
        count_operation("influx_queries")
    assert first == {"influx_queries": 1, "docker_commands": 1}
    assert second == {"influx_queries": 1}


def test_missing_diagnosis_verdict_is_not_healthy():
    from netopsbench.platform.session.scoring import diagnosis_to_agent_output

    assert diagnosis_to_agent_output({"reasoning": "unfinished"}).verdict == "inconclusive"


def test_historical_logs_and_metrics_share_episode_and_topology_scope(toolkit, monkeypatch):
    from netopsbench.platform.observability.influxdb import FluxQueryResult

    queries = []

    def query(*args, **kwargs):
        queries.append(args[3])
        return FluxQueryResult(status="ok", text="")

    monkeypatch.setattr("netopsbench.platform.toolkit._core.device.log_ops.query_flux", query)
    monkeypatch.setattr("netopsbench.platform.toolkit._core.observability.metrics_ops.query_flux", query)
    monkeypatch.setattr(
        "netopsbench.platform.toolkit._core.device.log_ops.get_device_logs_fallback", lambda *a, **kw: []
    )
    toolkit.set_pingmesh_time_window("2026-11-04T00:00:00Z", "2026-11-04T00:01:00Z")
    logs = toolkit.get_device_logs("leaf1")
    metrics = toolkit.get_interface_metrics("leaf1", "Ethernet0", view="raw")
    assert logs.success and metrics.success
    for result in (logs, metrics):
        assert result.data["time_scope"]["source"] == "toolkit_default"
    for query_text in queries:
        assert "2026-11-04T00:00:00Z" in query_text
        assert f'r.topology_id == "{toolkit.topology_id}"' in query_text


def test_missing_metrics_collect_live_interfaces_only_once(toolkit, monkeypatch):
    from netopsbench.platform.observability.influxdb import FluxQueryResult
    from netopsbench.platform.toolkit._core.observability import metrics_ops

    monkeypatch.setattr(metrics_ops, "query_flux", lambda *a, **kw: FluxQueryResult(status="ok", text=""))
    monkeypatch.setattr(metrics_ops, "get_recent_influx_interface_identities", lambda *a: [])
    calls = []

    def interfaces(*args, **kwargs):
        calls.append(args)
        return ToolResult(
            True, {"interfaces": [{"name": "Ethernet0", "state": "UP", "rx_errors": 3}], "collected_at": "now"}
        )

    monkeypatch.setattr(toolkit, "get_device_interfaces", interfaces)
    result = toolkit.get_interface_metrics("leaf1", "Ethernet0")
    assert result.success
    assert len(calls) == 1
    assert result.data["active_interfaces"] == ["Ethernet0"]
    assert result.data["current_snapshot"]["rx_errors"] == 3
    assert result.data["snapshot_collected_at"] == "now"


@pytest.mark.parametrize("view", ["raw", "both", "summary", "structured"])
def test_sonic_tables_filter_alias_and_keep_real_counters(toolkit, monkeypatch, view):
    def execute(container, args, timeout):
        if args[:3] == ["ip", "-j", "-4"]:
            output = json.dumps(
                [
                    {"ifname": name, "addr_info": [{"local": address, "prefixlen": 30}]}
                    for name, address in (("Ethernet0", "10.0.0.1"), ("Ethernet4", "10.0.0.5"))
                ]
            )
        elif "status" in args[-1]:
            output = (
                "Interface    Admin    Oper    MTU\nEthernet0    up    down    1400\nEthernet4    up    up    1500\n"
            )
        else:
            output = "IFACE    RX_ERR    TX_DRP\nEthernet0    7    2\nEthernet4    0    0\n"
        return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")

    monkeypatch.setattr(toolkit, "_docker_exec", execute)
    result = toolkit.get_device_interfaces("leaf1", interface="eth1", format=view)
    assert result.success
    assert "Ethernet4" not in json.dumps(result.data)
    assert result.data["addresses"] == {"Ethernet0": ["10.0.0.1/30"]}
    if view in {"summary", "structured"}:
        row = result.data["interfaces"][0]
        assert row["mtu"] == 1400 and row["rx_err"] == 7 and row["oper"] == "down"
    assert not toolkit.get_device_interfaces("leaf1", interface="eth30", format=view).success
