"""Contract tests for native client-agent deployment."""

from __future__ import annotations

import json
import subprocess
from concurrent.futures import Future
from pathlib import Path

import pytest

import netopsbench.platform.client_agent.deploy as deploy_mod


def _write_topology(
    topology_dir: Path,
    *,
    clients: int = 2,
    include_bind: bool = True,
) -> list[str]:
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
                "management": {
                    "network": "clab-mgmt-demo",
                    "ipv4_subnet": "172.20.20.0/24",
                },
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
                    {
                        "name": "leaf1",
                        "role": "leaf",
                        "mgmt_ip": "172.20.20.11",
                    },
                    *[
                        {
                            "name": name,
                            "role": "client",
                            "attached_switch": "leaf1",
                            "data_ip": f"192.168.101.{index + 1}",
                            "mgmt_ip": f"172.20.20.{100 + index}",
                            "metadata": {"rack": "rack1"},
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
        linux_kind["binds"] = [deploy_mod.CLIENT_AGENT_BIND]
    (topology_dir / "demo.clab.yaml").write_text(
        json.dumps(
            {
                "name": "demo",
                "topology": {"kinds": {"linux": linux_kind}},
            }
        ),
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


def test_deploy_starts_exactly_two_native_processes_per_client(tmp_path, monkeypatch):
    topology_dir = tmp_path / "topology"
    clients = _write_topology(topology_dir)
    docker_calls: list[tuple[str, ...]] = []

    monkeypatch.setattr(
        deploy_mod,
        "ThreadPoolExecutor",
        _RecordingExecutor,
        raising=False,
    )
    monkeypatch.setattr(
        deploy_mod,
        "_running_containers",
        lambda: {f"clab-demo-{client}" for client in clients},
    )

    def _docker(*args: str, check: bool = True, capture: bool = False, **kwargs):
        docker_calls.append(args)
        assert args[0] != "cp"
        if args[0] == "exec" and args[2:4] == ("sh", "-c"):
            command = args[4]
            assert "test -x /usr/local/bin/netopsbench-client-agent" in command
            assert "test -r /etc/netopsbench/client-agent.json" in command
            assert "netopsbench-client-agent pingmesh" in command
            assert "netopsbench-client-agent traffic" in command
            assert "/run/netopsbench/pingmesh.pid" in command
            assert "/run/netopsbench/traffic.pid" in command
            assert "for proc in /proc/[0-9]*" in command
            assert 'readlink "/proc/$1/exe"' in command
            assert 'if ! kill "$pid"' in command
            assert "/usr/local/bin/netopsbench-client-agent (deleted)" in command
            assert 'while same_live_pid_instance "$pid" "$start_time"' in command
            assert "awk '{print $22}' \"/proc/$1/stat\"" in command
            assert "awk '{print $3}' \"/proc/$1/stat\"" in command
            assert "python" not in command
            assert "iperf3" not in command
            assert ". /etc/netopsbench/client-agent.env" in command
            assert "NETOPSBENCH_INFLUXDB_TOKEN=" not in command
            assert "> /dev/null 2>&1" in command
            return subprocess.CompletedProcess(
                ["docker", *args],
                0,
                stdout="",
                stderr="",
            )
        raise AssertionError(f"unexpected docker call: {args}")

    monkeypatch.setattr(deploy_mod, "_docker", _docker)
    monkeypatch.setattr(
        deploy_mod,
        "request_agent",
        lambda _host, port, _operation: {
            "protocol_version": 1,
            "ok": True,
            "status": {
                "ready": port == deploy_mod.PINGMESH_CONTROL_PORT,
                "heartbeat_unix_ns": deploy_mod.time.time_ns(),
            },
        },
    )

    result = deploy_mod.deploy_client_agents(
        str(topology_dir),
        influxdb_token="test-token",
        influxdb_org="test-org",
        influxdb_bucket="test-bucket",
        parallelism=7,
    )

    config_path = topology_dir / "configs" / "client-agent" / "client-agent.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    env_path = config_path.with_name("client-agent.env")
    assert config["schema_version"] == 1
    assert len(config["clients"]) == len(clients)
    assert "probes" not in config
    assert "NETOPSBENCH_INFLUXDB_URL=http://telegraf:8186" in env_path.read_text()
    assert "NETOPSBENCH_INFLUXDB_BUCKET=test-bucket" in env_path.read_text()
    assert env_path.stat().st_mode & 0o777 == 0o600
    assert _RecordingExecutor.max_workers_seen == [7]
    assert len(_RecordingExecutor.submitted) == len(clients)
    assert len([args for args in docker_calls if args[0] == "exec"]) == len(clients)
    assert result.deployed == len(clients)
    assert result.failed == []


def test_deploy_is_all_or_nothing(tmp_path, monkeypatch):
    topology_dir = tmp_path / "topology"
    clients = _write_topology(topology_dir)
    monkeypatch.setattr(
        deploy_mod,
        "ThreadPoolExecutor",
        _RecordingExecutor,
        raising=False,
    )
    monkeypatch.setattr(
        deploy_mod,
        "_running_containers",
        lambda: {f"clab-demo-{client}" for client in clients},
    )

    commands: list[tuple[str, str]] = []

    def _docker(*args: str, check: bool = True, capture: bool = False, **kwargs):
        container = args[1]
        command = args[4]
        commands.append((container, command))
        if container.endswith("client2") and "nohup" in command:
            return subprocess.CompletedProcess(
                ["docker", *args],
                1,
                stdout="",
                stderr="missing binary",
            )
        return subprocess.CompletedProcess(
            ["docker", *args],
            0,
            stdout="",
            stderr="",
        )

    monkeypatch.setattr(deploy_mod, "_docker", _docker)
    monkeypatch.setattr(
        deploy_mod,
        "request_agent",
        lambda _host, port, _operation: {
            "protocol_version": 1,
            "ok": True,
            "status": {
                "ready": port == deploy_mod.PINGMESH_CONTROL_PORT,
                "heartbeat_unix_ns": deploy_mod.time.time_ns(),
            },
        },
    )

    with pytest.raises(RuntimeError, match="1/2.*client2"):
        deploy_mod.deploy_client_agents(
            str(topology_dir),
            influxdb_token="test-token",
            influxdb_org="test-org",
            influxdb_bucket="test-bucket",
        )

    cleanup = [(container, command) for container, command in commands if "nohup" not in command]
    assert {container for container, _ in cleanup} == {f"clab-demo-{client}" for client in clients}
    assert all("readlink" in command and "kill -KILL" in command for _, command in cleanup)
    assert all("pkill" not in command for _, command in cleanup)


def test_deploy_worker_exception_still_cleans_every_scheduled_client(tmp_path, monkeypatch):
    topology_dir = tmp_path / "topology"
    clients = _write_topology(topology_dir)
    monkeypatch.setattr(
        deploy_mod,
        "_running_containers",
        lambda: {f"clab-demo-{client}" for client in clients},
    )
    cleanup_containers: list[str] = []

    def _start_client(**kwargs):
        if kwargs["client_name"] == "client2":
            raise subprocess.TimeoutExpired("docker exec", 30)
        return kwargs["client_name"], True, ""

    monkeypatch.setattr(deploy_mod, "_start_client", _start_client)
    monkeypatch.setattr(
        deploy_mod,
        "_stop_client_agent",
        lambda container: cleanup_containers.append(container),
    )

    with pytest.raises(RuntimeError, match="1/2.*client2"):
        deploy_mod.deploy_client_agents(
            str(topology_dir),
            influxdb_token="test-token",
            influxdb_org="test-org",
            influxdb_bucket="test-bucket",
        )

    assert set(cleanup_containers) == {f"clab-demo-{client}" for client in clients}


def test_readiness_allows_slow_start_within_fifteen_seconds(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(deploy_mod.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(deploy_mod.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))

    def _status(_host, port, _operation):
        if clock[0] < 10.0:
            raise OSError("connection refused")
        return {
            "protocol_version": 1,
            "ok": True,
            "status": {
                "ready": port == deploy_mod.PINGMESH_CONTROL_PORT,
                "heartbeat_unix_ns": deploy_mod.time.time_ns(),
            },
        }

    monkeypatch.setattr(
        deploy_mod,
        "request_agent",
        _status,
    )

    deploy_mod._wait_until_ready("172.20.20.101")

    assert 10.0 <= clock[0] < deploy_mod.READINESS_TIMEOUT_SECONDS


def test_readiness_failure_is_explicit(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(deploy_mod.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(deploy_mod.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(
        deploy_mod,
        "request_agent",
        lambda _host, _port, _operation: (_ for _ in ()).throw(OSError("connection refused")),
    )

    with pytest.raises(RuntimeError, match="readiness failed: connection refused"):
        deploy_mod._wait_until_ready("172.20.20.101")


def test_custom_image_without_native_bind_is_rejected(tmp_path):
    topology_dir = tmp_path / "topology"
    _write_topology(topology_dir, include_bind=False)

    with pytest.raises(RuntimeError, match="configs/client-agent:/etc/netopsbench:ro"):
        deploy_mod.deploy_client_agents(
            str(topology_dir),
            influxdb_token="test-token",
            influxdb_org="test-org",
            influxdb_bucket="test-bucket",
        )
