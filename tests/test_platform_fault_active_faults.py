"""Regression tests for structured active fault tracking."""

import json
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from netopsbench.platform.faults.injector import FaultInjector
from netopsbench.platform.faults.models import ActiveFault
from netopsbench.platform.topology.generator import generate_topology


@pytest.fixture(autouse=True)
def mocked_runtime_tools(monkeypatch, tmp_path):
    # These transaction tests mock every command; toolchain validation has its own tests.
    monkeypatch.setattr("netopsbench.platform.faults.handlers.system.runtime_tools", lambda: tmp_path)


def _metadata() -> dict:
    with tempfile.TemporaryDirectory() as tmpdir:
        return generate_topology("xs", tmpdir)["metadata"]


def _stub_link_state(injector, *, fail_device=None):
    result_type = type("R", (), {})
    states = {}
    commands = []

    def result(returncode=0, stdout="", stderr=""):
        value = result_type()
        value.returncode = returncode
        value.stdout = stdout
        value.stderr = stderr
        return value

    def config_cmd(device, command):
        commands.append((device, tuple(command)))
        container = injector.container_names[device]
        interface = injector._iface.resolve_linux(command[2])
        if device == fail_device:
            return result(1, stderr="injected failure")
        states[(container, interface)] = "up" if command[1] == "startup" else "down"
        return result()

    def docker_exec(container, command, **_kwargs):
        if command[:5] == ["ip", "-o", "link", "show", "dev"]:
            interface = command[5]
            flags = (
                "BROADCAST,MULTICAST,UP" if states.get((container, interface), "up") == "up" else "BROADCAST,MULTICAST"
            )
            return result(stdout=f"1: {interface}: <{flags}>")
        if command[:3] == ["ip", "link", "set"]:
            if fail_device and container == injector.container_names[fail_device]:
                return result(1, stderr="injected failure")
            states[(container, command[3])] = command[4]
            return result()
        return result()

    injector._sonic.config_cmd = config_cmd
    injector._cmd.docker_exec = docker_exec
    return states, commands


def test_active_fault_dataclass_roundtrips_metadata_to_dict():
    fault = ActiveFault(
        type="packet_loss",
        device="leaf1",
        interface="eth1",
        metadata={"loss_pct": 10, "linux_interface": "eth1"},
    )

    payload = fault.to_dict()

    assert payload["type"] == "packet_loss"
    assert payload["device"] == "leaf1"
    assert payload["interface"] == "eth1"
    assert payload["loss_pct"] == 10
    assert payload["linux_interface"] == "eth1"


def test_active_fault_tracker_preserves_domain_objects():
    injector = FaultInjector(topology_metadata=_metadata())
    injector.active_faults = [
        ActiveFault(
            type="static_route_misconfig",
            device="leaf1",
            metadata={"target_ip": "192.168.1.2/32", "wrong_nexthop": "192.168.1.1"},
        )
    ]

    assert len(injector.active_faults) == 1
    assert isinstance(injector.active_faults[0], ActiveFault)
    assert injector.active_faults[0].metadata["target_ip"] == "192.168.1.2/32"


def test_link_flapping_uses_python_background_control_metadata():
    injector = FaultInjector(topology_metadata=_metadata())

    states, _commands = _stub_link_state(injector)

    fault = injector.inject_link_flapping(device="spine1", interface="Ethernet0", down_time=0.01, up_time=0.01)
    time.sleep(0.015)
    injector.recover_all()

    assert fault["type"] == "link_flapping"
    assert fault["orchestration"] == "python"
    assert fault["peer_device"] == "leaf1"
    assert "task_id" in fault
    assert "pid" not in fault
    assert states[("clab-dcn-spine1", "eth1")] == "up"
    assert states[("clab-dcn-leaf1", "eth1")] == "up"


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("converges", [False, True])
def test_link_admin_readback_waits_for_async_sonic_state_with_a_deadline(monkeypatch, enabled, converges):
    from netopsbench.platform.faults.handlers import link

    injector = FaultInjector(topology_metadata=_metadata())
    clock, reads, mutations = [0.0], [], []

    def sleep(delay):
        clock[0] += delay

    def configure(device, commands):
        mutations.append(commands)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def read(container, commands, *, timeout):
        assert commands == ["ip", "-o", "link", "show", "dev", "eth1"]
        assert clock[0] < 3
        assert 0 < timeout <= 3 - clock[0]
        reads.append(timeout)
        applied = enabled if converges and len(reads) >= 3 else not enabled
        flags = "BROADCAST,MULTICAST,UP" if applied else "BROADCAST,MULTICAST"
        return SimpleNamespace(returncode=0, stdout=f"1: eth1: <{flags}>", stderr="")

    monkeypatch.setattr(link, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep))
    monkeypatch.setattr(injector._sonic, "config_cmd", configure)
    monkeypatch.setattr(injector._cmd, "docker_exec", read)
    result = injector._link._set_link_admin_state("spine1", "clab-dcn-spine1", "Ethernet0", "eth1", enabled=enabled)
    assert result["success"] is converges
    assert mutations == [["interface", "startup" if enabled else "shutdown", "Ethernet0"]]
    if converges:
        assert len(reads) == 3
        assert clock[0] < 3
    else:
        assert clock[0] == 3
        assert result["error"]


def test_link_flapping_recovery_verifies_both_physical_endpoints():
    injector = FaultInjector(topology_metadata=_metadata())
    states, _commands = _stub_link_state(injector)

    injector.inject_link_flapping(
        device="spine1",
        interface="Ethernet0",
        down_time=10,
        up_time=10,
    )
    recovery = injector.recover_all()

    assert recovery == [
        {
            "type": "link_flapping",
            "device": "spine1",
            "interface": "Ethernet0",
            "recovered": True,
            "readback_verified": True,
            "error": None,
        }
    ]
    assert states[("clab-dcn-spine1", "eth1")] == "up"
    assert states[("clab-dcn-leaf1", "eth1")] == "up"
    assert injector.active_faults == []


def test_link_down_changes_both_physical_endpoints():
    injector = FaultInjector(topology_metadata=_metadata())
    states, _commands = _stub_link_state(injector)

    fault = injector.inject_link_down("spine1", "Ethernet0")

    assert fault["success"] is True
    assert fault["peer_device"] == "leaf1"
    assert fault["peer_interface"] == "Ethernet0"
    assert states[("clab-dcn-spine1", "eth1")] == "down"
    assert states[("clab-dcn-leaf1", "eth1")] == "down"


def test_link_down_tracks_residual_when_peer_and_compensation_fail():
    injector = FaultInjector(topology_metadata=_metadata())
    states, _commands = _stub_link_state(injector, fail_device="leaf1")

    fault = injector.inject_link_down("spine1", "Ethernet0")

    assert fault["success"] is False
    assert states[("clab-dcn-spine1", "eth1")] == "up"
    assert len(injector.active_faults) == 1
    assert injector.active_faults[0].metadata["residual_mutation"] is True


def test_link_recovery_attempts_peer_even_when_target_fails():
    injector = FaultInjector(topology_metadata=_metadata())
    states, _commands = _stub_link_state(injector)
    fault = injector.inject_link_down("spine1", "Ethernet0")
    original_config = injector._sonic.config_cmd
    injector._sonic.config_cmd = lambda device, command: (
        type("R", (), {"returncode": 1, "stdout": "", "stderr": "target recovery failed"})()
        if device == "spine1"
        else original_config(device, command)
    )
    original_exec = injector._cmd.docker_exec

    def docker_exec(container, command, **kwargs):
        if container == "clab-dcn-spine1" and command[:3] == ["ip", "link", "set"]:
            return type("R", (), {"returncode": 1, "stdout": "", "stderr": "target recovery failed"})()
        return original_exec(container, command, **kwargs)

    injector._cmd.docker_exec = docker_exec

    recovery = injector.recover_link_down(
        fault["device"],
        fault["interface"],
        peer_device=fault["peer_device"],
        peer_interface=fault["peer_interface"],
        route_snapshots=fault["route_snapshots"],
    )

    assert recovery["recovered"] is False
    assert states[("clab-dcn-leaf1", "eth1")] == "up"


def test_access_link_recovery_restores_client_routes_removed_by_link_down():
    injector = FaultInjector(topology_metadata=_metadata())
    states, _commands = _stub_link_state(injector)
    routes = [
        {"dst": "192.168.0.0/16", "gateway": "192.168.101.1", "dev": "eth1"},
        {
            "dst": "192.168.101.0/30",
            "dev": "eth1",
            "protocol": "kernel",
            "scope": "link",
        },
    ]
    original_config = injector._sonic.config_cmd
    original_exec = injector._cmd.docker_exec
    result_type = type("R", (), {})

    def result(returncode=0, stdout="", stderr=""):
        value = result_type()
        value.returncode = returncode
        value.stdout = stdout
        value.stderr = stderr
        return value

    def config_cmd(device, command):
        value = original_config(device, command)
        if device == "client1" and command[1] == "shutdown":
            routes[:] = [route for route in routes if route.get("protocol") == "kernel"]
        return value

    def docker_exec(container, command, **kwargs):
        if command[:5] == ["ip", "-j", "route", "show", "dev"]:
            return result(stdout=json.dumps(routes))
        if command[:3] == ["ip", "route", "replace"]:
            routes.append(
                {
                    "dst": command[3],
                    "gateway": command[5],
                    "dev": command[7],
                }
            )
            return result()
        return original_exec(container, command, **kwargs)

    injector._sonic.config_cmd = config_cmd
    injector._cmd.docker_exec = docker_exec

    fault = injector.inject_link_down("leaf1", "Ethernet8")
    recovery = injector.recover_link_down(
        fault["device"],
        fault["interface"],
        peer_device=fault["peer_device"],
        peer_interface=fault["peer_interface"],
        route_snapshots=fault["route_snapshots"],
    )

    assert recovery["recovered"] is True
    assert any(route.get("dst") == "192.168.0.0/16" for route in routes)
    assert states[("clab-dcn-client1", "eth1")] == "up"


def test_device_down_uses_containerlab_node_stop():
    injector = FaultInjector(topology_metadata=_metadata())
    injector._system._cmd.container_is_running = lambda _container: True
    calls = []
    result = type("R", (), {"returncode": 0, "stderr": "", "stdout": "spine1"})()
    injector._system._cmd.run_cmd = lambda command, **_kwargs: calls.append(command) or result
    injector._system._settled_stop_state = lambda _container: (False, True)

    fault = injector.inject_device_down("spine1")

    assert fault["success"] is True
    assert fault["mode"] == "containerlab_node_stop"
    assert fault["parking_exists"] is True
    assert calls[0][-7:] == [
        "stop",
        "-t",
        str(injector._system._ctx.topology_file),
        "--node",
        "spine1",
        "--timeout",
        "120s",
    ]


def test_device_down_starts_supervisor_activates_and_waits_for_bgp_recovery(monkeypatch):
    injector = FaultInjector(topology_metadata=_metadata())
    calls = []
    result = type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})()
    established = iter([False, True])
    injector._system._cmd.run_cmd = lambda command, **_kwargs: calls.append(command) or result
    running = iter([False, True, True, True, True])
    injector._system._cmd.container_is_running = lambda _container: next(running)
    parking = iter([True, False])
    injector._system._parking_namespace_exists = lambda _container: next(parking)
    injector._system._observed_dataplane_interface_count = lambda _container: 2
    supervisor_ready = iter([False, True, True])
    injector._system._sonic.supervisord_ready = lambda _container: next(supervisor_ready)
    injector._system._sonic.bgp_neighbors_established = lambda _device: next(established)
    injector._system._sonic.vtysh = lambda _device, _commands: result
    activations = []
    monkeypatch.setattr(
        "netopsbench.platform.faults.handlers.system.activate_device",
        lambda *args, **kwargs: activations.append((args, kwargs)) or (True, "activated"),
    )

    with patch("netopsbench.platform.faults.handlers.system.time.sleep"):
        recovery = injector.recover_device_down("spine1")

    assert recovery["recovered"] is True
    assert recovery["sonic_ready"] is True
    assert calls[0][-7:] == [
        "start",
        "-t",
        str(injector._system._ctx.topology_file),
        "--node",
        "spine1",
        "--timeout",
        "120s",
    ]
    assert calls[1][-5:] == [
        "docker",
        "exec",
        "-d",
        "clab-dcn-spine1",
        "/usr/local/bin/supervisord",
    ]
    assert activations
