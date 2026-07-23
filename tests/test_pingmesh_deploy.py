"""Unit tests for bind-mounted Pingmesh deployment."""

from __future__ import annotations

import json
import subprocess
from concurrent.futures import Future
from pathlib import Path

import pytest

import netopsbench.platform.pingmesh.deploy as deploy_mod


def _write_topology(topology_dir: Path, *, clients: int = 3, include_bind: bool = True) -> list[str]:
    client_names = [f"client{i}" for i in range(1, clients + 1)]
    topology_dir.mkdir(parents=True, exist_ok=True)
    (topology_dir / "topology.json").write_text(
        json.dumps(
            {
                "schema_version": "3",
                "topology_id": "demo",
                "name": "demo",
                "scale": "test",
                "family": "clos",
                "management": {"network": "clab-mgmt-demo", "ipv4_subnet": "172.20.20.0/24"},
                "collector": {"ipv4": "172.20.20.200"},
                "defaults": {},
                "facts": {
                    "num_leafs": 1,
                    "clients_per_attached_switch": max(1, clients),
                    "total_clients": clients,
                    "total_switches": 1,
                },
                "routing": {"ecmp_hash_policy_by_role": {"leaf": 1}},
                "devices": [
                    {"name": "leaf1", "role": "leaf"},
                    *[
                        {
                            "name": name,
                            "role": "client",
                            "attached_switch": "leaf1",
                            "data_ip": f"192.168.101.{index + 1}",
                        }
                        for index, name in enumerate(client_names, start=1)
                    ],
                ],
                "links": [],
                "pingmesh": {"destination_batch_size": 16},
            }
        ),
        encoding="utf-8",
    )
    linux_kind = {"image": "client"}
    if include_bind:
        linux_kind["binds"] = ["configs/pingmesh:/tmp/pingmesh:ro"]
    (topology_dir / "demo.clab.yaml").write_text(
        json.dumps({"name": "demo", "topology": {"kinds": {"linux": linux_kind}}}),
        encoding="utf-8",
    )
    return client_names


class _RecordingExecutor:
    max_workers_seen: list[int] = []
    submitted: list[tuple] = []

    def __init__(self, max_workers: int):
        self.max_workers_seen.append(max_workers)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def submit(self, fn, *args, **kwargs):
        self.submitted.append((fn, args, kwargs))
        future: Future = Future()
        future.set_result(fn(*args, **kwargs))
        return future


@pytest.fixture(autouse=True)
def _reset_executor():
    _RecordingExecutor.max_workers_seen.clear()
    _RecordingExecutor.submitted.clear()


def _fake_pinglist_generator(calls: list[Path]):
    def _generate(metadata_file: str, output_file: str, topology_id: str | None = None):
        output_path = Path(output_file)
        calls.append(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps({"probes": [], "total_probes": 0, "topology_id": topology_id}),
            encoding="utf-8",
        )
        return []

    return _generate


def test_deploy_pingmesh_stages_runtime_once_and_starts_clients_in_parallel(tmp_path, monkeypatch):
    topology_dir = tmp_path / "topology"
    clients = _write_topology(topology_dir, clients=3)
    generator_calls: list[Path] = []
    docker_calls: list[tuple[str, ...]] = []

    monkeypatch.setattr(deploy_mod, "ThreadPoolExecutor", _RecordingExecutor, raising=False)
    monkeypatch.setattr(deploy_mod, "generate_pinglist_from_topology", _fake_pinglist_generator(generator_calls))
    monkeypatch.setattr(
        deploy_mod,
        "_running_containers",
        lambda: [f"clab-demo-{client}" for client in clients],
    )

    def _docker(*args: str, check: bool = True, capture: bool = False, **kwargs):
        docker_calls.append(args)
        assert args[0] != "cp", "Pingmesh deploy should use bind-mounted runtime, not docker cp"
        if args[0] == "exec" and args[2:4] == ("sh", "-c"):
            command = args[4]
            assert "mkdir -p /var/log/pingmesh" in command
            assert "test -r /tmp/pingmesh/netopsbench/platform/pingmesh/cli.py" in command
            assert "test -r /tmp/pingmesh/pinglist.json" in command
            assert "pgrep -f netopsbench.platform.pingmesh.cli" in command
            assert '[ "$pid" = "$$" ] && continue' in command
            assert 'kill "$pid"' in command
            assert "nohup python3 -m netopsbench.platform.pingmesh.cli" in command
            assert "NETOPSBENCH_INFLUXDB_URL=http://telegraf:8186" in command
            return subprocess.CompletedProcess(["docker", *args], 0, stdout="", stderr="")
        if args[0] == "exec" and args[2:] == ("ps", "aux"):
            return subprocess.CompletedProcess(
                ["docker", *args], 0, stdout="netopsbench.platform.pingmesh.cli", stderr=""
            )
        raise AssertionError(f"unexpected docker call: {args}")

    monkeypatch.setattr(deploy_mod, "_docker", _docker)

    result = deploy_mod.deploy_pingmesh(str(topology_dir), verify=False, parallelism=7)

    runtime_dir = topology_dir / "configs" / "pingmesh"
    assert generator_calls == [runtime_dir / "pinglist.json"]
    assert (runtime_dir / "pinglist.json").is_file()
    assert not (runtime_dir / "run_pingmesh_agent.py").exists()
    for module in deploy_mod._AGENT_MODULES:
        assert (runtime_dir / "netopsbench" / "platform" / "pingmesh" / module).is_file()
    for module in deploy_mod._PACKAGE_MODULES:
        assert (runtime_dir / "netopsbench" / module).is_file()
    assert not (runtime_dir / "agent.py").exists()

    start_calls = [args for args in docker_calls if args[0] == "exec"]
    assert len(start_calls) == len(clients)
    assert all(args[2:4] == ("sh", "-c") for args in start_calls)
    assert _RecordingExecutor.max_workers_seen == [7]
    assert len(_RecordingExecutor.submitted) == len(clients)
    assert result.deployed == len(clients)
    assert result.failed == []


def test_deploy_pingmesh_records_failed_client_when_bind_runtime_is_unreadable(tmp_path, monkeypatch):
    topology_dir = tmp_path / "topology"
    clients = _write_topology(topology_dir, clients=2)
    generator_calls: list[Path] = []

    monkeypatch.setattr(deploy_mod, "ThreadPoolExecutor", _RecordingExecutor, raising=False)
    monkeypatch.setattr(deploy_mod, "generate_pinglist_from_topology", _fake_pinglist_generator(generator_calls))
    monkeypatch.setattr(
        deploy_mod,
        "_running_containers",
        lambda: [f"clab-demo-{client}" for client in clients],
    )

    def _docker(*args: str, check: bool = True, capture: bool = False, **kwargs):
        assert args[0] != "cp", "Pingmesh deploy should not fall back to docker cp"
        if args[0] == "exec" and args[2:4] == ("sh", "-c"):
            if args[1].endswith("client2"):
                return subprocess.CompletedProcess(["docker", *args], 1, stdout="", stderr="missing bind")
            return subprocess.CompletedProcess(["docker", *args], 0, stdout="", stderr="")
        raise AssertionError(f"unexpected docker call: {args}")

    monkeypatch.setattr(deploy_mod, "_docker", _docker)

    result = deploy_mod.deploy_pingmesh(str(topology_dir), verify=False)

    assert generator_calls == [topology_dir / "configs" / "pingmesh" / "pinglist.json"]
    assert result.deployed == 1
    assert result.failed == ["client2"]


def test_deploy_pingmesh_rejects_topology_without_pingmesh_bind(tmp_path):
    topology_dir = tmp_path / "topology"
    _write_topology(topology_dir, clients=1, include_bind=False)

    with pytest.raises(RuntimeError, match="configs/pingmesh:/tmp/pingmesh:ro"):
        deploy_mod.deploy_pingmesh(str(topology_dir), verify=False)
