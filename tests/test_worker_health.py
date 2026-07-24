import subprocess
import time
from pathlib import Path

from netopsbench.models.runtime import RuntimeIdentity
from netopsbench.platform.runtime import health


def _canonical_topology_dict(*, scale: str, spines: int, leafs: int, clients: int) -> dict:
    devices = [
        *[{"name": f"spine{i}", "role": "spine", "mgmt_ip": f"172.20.20.{10 + i}"} for i in range(1, spines + 1)],
        *[{"name": f"leaf{i}", "role": "leaf", "mgmt_ip": f"172.20.20.{10 + spines + i}"} for i in range(1, leafs + 1)],
        *[
            {
                "name": f"client{i}",
                "role": "client",
                "attached_switch": f"leaf{((i - 1) % leafs) + 1}",
                "data_ip": f"192.168.{100 + i}.2",
            }
            for i in range(1, clients + 1)
        ],
    ]
    return {
        "schema_version": "3",
        "topology_id": scale,
        "name": scale,
        "scale": scale,
        "family": "clos",
        "management": {"network": f"clab-{scale}", "ipv4_subnet": "172.20.20.0/23"},
        "collector": {"ipv4": "172.20.20.200"},
        "defaults": {"link_mtu": 9232, "sonic_port_mtu": 9100},
        "facts": {
            "num_spines": spines,
            "num_leafs": leafs,
            "clients_per_attached_switch": 1,
            "total_clients": clients,
            "total_switches": spines + leafs,
        },
        "routing": {"ecmp_hash_policy_by_role": {"spine": 1, "leaf": 1}},
        "devices": devices,
        "links": [],
    }


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


def test_active_interface_coverage_flags_xlarge_spine_with_only_32_ports_up():
    topo = _canonical_topology_dict(scale="xlarge", spines=16, leafs=128, clients=128)
    output = "\n".join(f"Ethernet{idx * 4} 1,2,3,4 100G 9100 N/A up up QSFP" for idx in range(32))

    active = health._parse_active_interfaces(output)
    assert len(active) == 32
    assert health._expected_active_interface_count(topo, "spine1") == 128
    assert health._expected_active_interface_count(topo, "leaf128") == 17

    error = health._active_interface_coverage_error(
        container="clab-xlarge-spine1",
        device="spine1",
        active_interfaces=active,
        expected_count=128,
    )
    assert error == "active interface coverage too low on clab-xlarge-spine1: active=32 expected>=128"


def test_active_interface_coverage_accepts_required_count():
    output = "\n".join(f"Ethernet{idx * 4} 1,2,3,4 100G 9100 N/A up up QSFP" for idx in range(128))

    error = health._active_interface_coverage_error(
        container="clab-xlarge-spine1",
        device="spine1",
        active_interfaces=health._parse_active_interfaces(output),
        expected_count=128,
    )
    assert error is None


def test_fat_tree_sparse_expected_active_interface_counts():
    import tempfile

    from netopsbench.platform.topology.generator import generate_topology
    from netopsbench.platform.topology.topology_utils import load_topology_manifest

    with tempfile.TemporaryDirectory() as raw_dir:
        generate_topology("fat-tree-k12", raw_dir)
        topo = load_topology_manifest(raw_dir)

        assert health._expected_active_interface_count(topo, "core1") == 12
        assert health._expected_active_interface_count(topo, "agg1") == 12
        assert health._expected_active_interface_count(topo, "edge72") == 8


def test_worker_health_retries_observability_until_collector_is_ready(tmp_path, monkeypatch):
    from netopsbench.platform.observability import influxdb, validation
    from netopsbench.platform.topology.generator import generate_topology

    generate_topology("xs", str(tmp_path), name="health-xs")

    monkeypatch.setattr(
        health,
        "safe_run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout="telegraf-health-xs\n", stderr=""),
    )
    monkeypatch.setattr(health, "_running_container_count", lambda _lab_name: 6)

    def fake_docker_exec(_container, *command, **_kwargs):
        joined = " ".join(command)
        if "show ip bgp summary" in joined:
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
            },
        },
    )
    attempts = []

    def fake_check_observability(*_args, **_kwargs):
        attempts.append(1)
        return ["collector has not written its first sample"] if len(attempts) == 1 else []

    monkeypatch.setattr(validation, "check_observability", fake_check_observability)
    monkeypatch.setattr(
        influxdb, "query_flux", lambda *_args, **_kwargs: type("Result", (), {"status": "ok", "text": ""})()
    )

    errors = health.check_worker_health(
        _runtime_identity(tmp_path, "health-xs"),
        health_retries=2,
        health_delay=0,
    )

    assert errors == []
    assert len(attempts) == 2
