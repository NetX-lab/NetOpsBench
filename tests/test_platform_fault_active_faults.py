"""Regression tests for structured active fault tracking."""

import json
import tempfile
import time
from unittest.mock import patch

from netopsbench.platform.faults.injector import FaultInjector
from netopsbench.platform.faults.models import ActiveFault
from netopsbench.platform.topology.generator import generate_topology


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

    fault = injector.inject_link_flapping(device="spine1", interface="Ethernet0", iterations=1, down_time=0, up_time=0)
    time.sleep(0.01)

    assert fault["type"] == "link_flapping"
    assert fault["orchestration"] == "python"
    assert fault["peer_device"] == "leaf1"
    assert "task_id" in fault
    assert "pid" not in fault
    assert states[("clab-dcn-spine1", "eth1")] == "up"
    assert states[("clab-dcn-leaf1", "eth1")] == "up"


def test_link_flapping_recovery_verifies_both_physical_endpoints():
    injector = FaultInjector(topology_metadata=_metadata())
    states, _commands = _stub_link_state(injector)

    injector.inject_link_flapping(
        device="spine1",
        interface="Ethernet0",
        iterations=10,
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


def test_device_down_stops_the_target_container():
    injector = FaultInjector(topology_metadata=_metadata())
    calls = []
    result = type("R", (), {"returncode": 0, "stderr": "", "stdout": "spine1"})()
    injector._system._cmd.run_cmd = lambda command, **_kwargs: calls.append(command) or result
    injector._system._cmd.container_is_running = lambda _container: False

    fault = injector.inject_device_down("spine1")

    assert fault["success"] is True
    assert fault["mode"] == "container_stop"
    assert calls[0][-3:] == ["docker", "stop", "clab-dcn-spine1"]


def test_device_down_starts_and_waits_for_bgp_recovery():
    injector = FaultInjector(topology_metadata=_metadata())
    calls = []
    result = type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})()
    established = iter([False, True])
    injector._system._cmd.run_cmd = lambda command, **_kwargs: calls.append(command) or result
    injector._system._cmd.container_is_running = lambda _container: True
    injector._system._sonic.supervisord_ready = lambda _container: True
    injector._system._sonic.bgp_neighbors_established = lambda _device: next(established)
    injector._system._sonic.vtysh = lambda _device, _commands: result

    with patch("netopsbench.platform.faults.handlers.system.time.sleep"):
        recovery = injector.recover_device_down("spine1")

    assert recovery["recovered"] is True
    assert recovery["sonic_ready"] is True
    assert calls[0][-3:] == ["docker", "start", "clab-dcn-spine1"]
