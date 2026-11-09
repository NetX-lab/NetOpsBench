import json
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from netopsbench.models.runtime import RuntimeIdentity
from netopsbench.platform.runtime import health
from netopsbench.platform.topology.topology_utils import coerce_topology_manifest


def test_k18_expectations_distinguish_client_links_and_bgp_fabric():
    from netopsbench.platform.topology.config import config_for_scale
    from netopsbench.platform.topology.fat_tree_builder import build_fat_tree_plan

    topo = build_fat_tree_plan(config_for_scale("fat-tree-k18")).manifest
    counts = health.expected_network_counts(topo)
    assert counts["core81"] == {"active_interfaces": 18, "bgp_neighbors": 18}
    assert counts["agg162"] == {"active_interfaces": 18, "bgp_neighbors": 18}
    assert counts["edge162"] == {"active_interfaces": 11, "bgp_neighbors": 9}
    assert counts["client324"] == {"active_interfaces": 1, "bgp_neighbors": 0}
    assert sum(row["bgp_neighbors"] for row in counts.values()) == 5832
    assert sum(row["active_interfaces"] for row in counts.values()) == 6480


def _canonical_topology_dict(*, scale: str, spines: int, leafs: int, clients: int) -> dict:
    from netopsbench.platform.topology.clos_builder import build_clos_plan
    from netopsbench.platform.topology.config import TopologyConfig

    return build_clos_plan(
        TopologyConfig(
            name=scale,
            scale_name=scale,
            num_spines=spines,
            num_leafs=leafs,
            clients_per_leaf=clients // leafs,
            mgmt_ipv4_subnet="172.20.20.0/23",
        )
    ).manifest.model_dump(mode="json")


def _runtime_identity(topology_dir: Path, name: str) -> RuntimeIdentity:
    return RuntimeIdentity.create(
        runtime_id=name,
        worker_id="worker-1",
        worker_index=1,
        lab_name=name,
        topology_dir=topology_dir,
        mgmt_subnet="172.20.20.0/24",
        mgmt_network=f"clab-mgmt-{name}",
        bucket=f"network_data_{name}",
    )


def test_active_interface_parser_exposes_incomplete_xlarge_spine():
    topo = coerce_topology_manifest(_canonical_topology_dict(scale="xlarge", spines=16, leafs=128, clients=256))
    output = "\n".join(f"Ethernet{idx * 4} 1,2,3,4 100G 9100 N/A up up QSFP" for idx in range(32))

    active = health._parse_active_interfaces(output)
    assert len(active) == 32
    counts = health.expected_network_counts(topo)
    assert counts["spine1"]["active_interfaces"] == 128
    assert counts["leaf128"]["active_interfaces"] == 18


def test_deployment_convergence_checks_every_routing_device():
    topo = coerce_topology_manifest(_canonical_topology_dict(scale="xlarge", spines=16, leafs=128, clients=256))

    assert len(health._convergence_targets(topo, all_routing_devices=True)) == 144
    assert len(health._convergence_targets(topo, all_routing_devices=False)) == 3


def test_fat_tree_sparse_expected_active_interface_counts():
    import tempfile

    from netopsbench.platform.topology.generator import generate_topology
    from netopsbench.platform.topology.topology_utils import load_topology_manifest

    with tempfile.TemporaryDirectory() as raw_dir:
        generate_topology("fat-tree-k12", raw_dir)
        topo = load_topology_manifest(raw_dir)

        counts = health.expected_network_counts(topo)
        assert counts["core1"]["active_interfaces"] == 12
        assert counts["agg1"]["active_interfaces"] == 12
        assert counts["edge72"]["active_interfaces"] == 8


@pytest.mark.parametrize("budgeted", [False, True])
@pytest.mark.parametrize("fib_delay", [False, True])
def test_worker_health_retries_observability_until_collector_is_ready(tmp_path, monkeypatch, budgeted, fib_delay):
    from netopsbench.platform.observability import influxdb, validation
    from netopsbench.platform.topology.generator import generate_topology

    metadata = generate_topology("xs", str(tmp_path), name="health-xs")["metadata"]

    monkeypatch.setattr(
        health,
        "safe_run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout="telegraf-health-xs\n", stderr=""),
    )
    monkeypatch.setattr(health, "_running_container_count", lambda _lab_name, **kw: 6)

    exec_calls = []
    fib_polls = {}

    def fake_docker_exec(_container, *command, **_kwargs):
        exec_calls.append(command)
        joined = " ".join(command)
        if command[:2] == ("python3", "-c"):
            fib_polls[_container] = fib_polls.get(_container, 0) + 1
            hops = [{"gateway": "10.0.0.1"}]
            if fib_polls[_container] > 1:
                hops.append({"gateway": "10.0.0.2"})
            fib = [{"dst": "192.168.12.0/30", "protocol": "bgp", "nexthops": hops}]
            output = json.dumps(
                {
                    "version": 1,
                    "checks": {
                        "bgp": {
                            "returncode": 0,
                            "stdout": fake_docker_exec(_container, "vtysh", "-c", "show ip bgp summary").stdout,
                        },
                        "interfaces": {
                            "returncode": 0,
                            "stdout": fake_docker_exec(_container, "bash", "-lc", "show interfaces status").stdout,
                        },
                        "fib": {"returncode": 0, "stdout": json.dumps(fib)},
                    },
                }
            )
        elif "show ip bgp summary" in joined:
            output = "".join(f"10.0.0.{index} 4 65001 0 0 0 0 0 00:10:00 1\n" for index in range(1, 5))
        elif "show interfaces status" in joined:
            output = "".join(f"Ethernet{index * 4} 1,2,3,4 100G 9100 N/A up up QSFP\n" for index in range(4))
        else:
            output = ""
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    monkeypatch.setattr(health, "_docker_exec", fake_docker_exec)
    monkeypatch.setattr(
        health,
        "request_agent",
        lambda *_args, **_kwargs: {
            "protocol_version": 1,
            "ok": True,
            "status": {
                "ready": True,
                "heartbeat_unix_ns": time.time_ns(),
                "plan_id": metadata["pingmesh_plan"]["settings"]["plan_id"],
                "config_schema_version": 2,
            },
        },
    )
    attempts = []

    def fake_check_observability(*_args, **_kwargs):
        check = next(iter(_kwargs["checks"]))
        attempts.append(check)
        if budgeted:
            time.sleep(0.02)
            return ["collector still unavailable"]
        return (
            ["collector has not written its first sample"] if check == "syslog" and attempts.count(check) == 1 else []
        )

    monkeypatch.setattr(validation, "check_observability", fake_check_observability)
    monkeypatch.setattr(
        influxdb, "query_flux", lambda *_args, **_kwargs: type("Result", (), {"status": "ok", "text": ""})()
    )

    started = time.monotonic()
    expected_routes = {
        "nodes": {
            device["name"]: {
                "fib": [
                    {
                        "dst": "192.168.12.0/30",
                        "protocol": "bgp",
                        "nexthops": [{"gateway": "10.0.0.1"}, {"gateway": "10.0.0.2"}],
                    }
                ]
            }
            for device in metadata["devices"]
            if device["role"] != "client"
        }
    }
    errors = health.check_worker_health(
        _runtime_identity(tmp_path, "health-xs"),
        health_retries=100 if budgeted else 2,
        health_delay=0.01 if budgeted else 0,
        deadline=started + 0.15 if budgeted else None,
        route_snapshot={} if fib_delay else None,
        expected_routes=expected_routes if fib_delay else None,
    )

    if budgeted:
        assert errors == ["Worker health check deadline exceeded"]
        assert time.monotonic() - started < 1
        assert attempts
        return
    assert errors == []
    if fib_delay:
        assert all(polls >= 2 for polls in fib_polls.values())
    assert "logger -n" in " ".join(exec_calls[0])
    assert sum(c == ("bash", "-lc", "show interfaces status") for c in exec_calls) == (6 if fib_delay else 3)
    assert sorted(attempts) == ["bgp", "interfaces", "pingmesh", "syslog", "syslog"]


def test_network_probe_preserves_commands_order_and_deadlines():
    from netopsbench.platform.observability._routing_probe import probe

    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="\n".join(command))

    result = probe(run)
    assert [c[0] for c in calls] == [
        ["vtysh", "-c", "show ip bgp summary"],
        ["bash", "-lc", "show interfaces status"],
        ["ip", "-j", "-4", "route", "show"],
    ]
    assert all(c[1]["timeout"] == 60 for c in calls)
    assert result["checks"]["bgp"]["stdout"] == "vtysh\n-c\nshow ip bgp summary"


@pytest.mark.parametrize("failure", [subprocess.TimeoutExpired("vtysh", 60), OSError("missing")])
def test_network_probe_preserves_independent_check_after_failure(failure):
    from netopsbench.platform.observability._routing_probe import probe

    def run(command, **kwargs):
        if command[0] == "vtysh":
            raise failure
        return subprocess.CompletedProcess(command, 0, stdout="Ethernet0 up up")

    result = probe(run)
    assert result["checks"]["bgp"]["returncode"] == -1
    assert result["checks"]["interfaces"]["returncode"] == 0


@pytest.mark.parametrize(
    "payload",
    ["not json", "null", "[]", '{"version": 2}', '{"version": 1, "checks": {}}'],
)
def test_routing_state_rejects_invalid_response(monkeypatch, payload):
    monkeypatch.setattr(health, "_docker_exec", lambda *a, **kw: subprocess.CompletedProcess(a, 0, stdout=payload))
    assert health._routing_state("node") == ("", "")


@pytest.mark.parametrize("failed", [None, "bgp", "interfaces", "transport"])
def test_routing_state_uses_one_exec_and_rejects_failed_command(monkeypatch, failed):
    calls = []

    def execute(*args, **kwargs):
        calls.append((args, kwargs))
        payload = {
            "version": 1,
            "checks": {
                name: {"returncode": 7 if name == failed else 0, "stdout": name} for name in ["bgp", "interfaces"]
            },
        }
        return subprocess.CompletedProcess(args, 1 if failed == "transport" else 0, stdout=json.dumps(payload))

    monkeypatch.setattr(health, "_docker_exec", execute)
    assert health._routing_state("node") == (("bgp", "interfaces") if failed is None else ("", ""))
    assert len(calls) == 1
    assert calls[0][0][:3] == ("node", "python3", "-c")
    assert calls[0][1]["timeout"] == 125


def test_large_health_queue_stops_launching_probes_at_shared_deadline():
    import threading

    from netopsbench.platform.utils.control import OperationControl

    control = OperationControl(deadline=time.monotonic() + 0.04)
    started = []
    lock = threading.Lock()

    def probe(device):
        with lock:
            started.append(device)
        control.sleep(0.02)
        return device

    before = time.monotonic()
    with pytest.raises(TimeoutError):
        health._parallel_check(probe, list(range(405)), 16, control)
    assert len(started) < 405
    assert time.monotonic() - before < 1


def test_health_external_deadline_includes_initial_probes(tmp_path, monkeypatch):
    from netopsbench.platform.topology.generator import generate_topology

    generate_topology("xs", str(tmp_path), name="health-xs")
    calls = []
    monkeypatch.setattr(health, "safe_run", lambda *a, **kw: calls.append(a) or pytest.fail("expired probe started"))
    errors = health.check_worker_health(_runtime_identity(tmp_path, "health-xs"), deadline=time.monotonic() - 1)
    assert errors == ["Worker health check deadline exceeded"]
    assert calls == []


def test_warm_records_fresh_complete_health_check(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from netopsbench.platform.runtime import lifecycle

    calls = []
    monkeypatch.setattr(lifecycle, "check_worker_health", lambda *a, **kw: calls.append(kw) or [])
    runtime = SimpleNamespace(workers=[_runtime_identity(tmp_path, "warm-xs")], root_dir=tmp_path, size=1)
    result = lifecycle.RuntimeLifecycle()._warm(runtime)
    assert len(calls) == 1 and calls[0]["all_routing_devices"] is True
    assert result["all_routing_devices"] is True and result["checked_at"]


def test_quarantine_accumulates_workers_without_duplicate_writes(tmp_path, monkeypatch):
    from netopsbench.platform.runtime.manager import RuntimePool

    runtime = RuntimePool("id", "name", "xs", tmp_path, [])
    writes = []
    monkeypatch.setattr(runtime, "_write_metadata", lambda: writes.append(dict(runtime.metadata)))
    runtime.mark_quarantined("scenario_cleanup_failure", ["worker-1"])
    runtime.mark_quarantined("scenario_cleanup_failure", ["worker-1"])
    runtime.mark_quarantined("scenario_cleanup_failure", ["worker-2"])
    assert runtime.state == "quarantined"
    assert runtime.metadata["quarantined_workers"] == ["worker-1", "worker-2"]
    assert len(writes) == 2


def test_routing_probe_subcommands_share_one_deadline(monkeypatch):
    import netopsbench.platform.observability._routing_probe as probe_module

    clock = [0.0]
    monkeypatch.setattr(probe_module.time, "monotonic", lambda: clock[0])
    timeouts = []

    def run(command, **kwargs):
        timeouts.append(kwargs["timeout"])
        clock[0] += 4
        return SimpleNamespace(returncode=0, stdout="[]")

    payload = probe_module.probe(run, budget_seconds=5)
    assert timeouts == [5, 1]
    assert payload["checks"]["fib"]["error"] == "deadline_exceeded"
