"""Focused transaction tests for built-in fault handlers."""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass

import pytest

from netopsbench.platform.faults.injector import FaultInjector
from netopsbench.platform.faults.services.command_runner import CommandRunner
from netopsbench.platform.faults.services.tracking import FaultTracker
from netopsbench.platform.topology.generator import generate_topology


@pytest.fixture(autouse=True)
def mocked_runtime_tools(monkeypatch, tmp_path):
    # These transaction tests mock every command; toolchain validation has its own tests.
    monkeypatch.setattr("netopsbench.platform.faults.handlers.system.runtime_tools", lambda: tmp_path)


def _metadata() -> dict:
    with tempfile.TemporaryDirectory() as tmpdir:
        return generate_topology("xs", tmpdir)["metadata"]


@dataclass
class _Result:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


def _assert_single_residual(injector: FaultInjector, fault_type: str) -> None:
    assert len(injector.active_faults) == 1
    residual = injector.active_faults[0]
    assert residual.type == fault_type
    assert residual.success is False
    assert residual.metadata["residual_mutation"] is True


def test_command_timeout_is_returned_as_a_transaction_failure(monkeypatch):
    monkeypatch.setattr(
        "netopsbench.platform.faults.services.command_runner.safe_run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(["docker", "exec"], timeout=7, output=b"partial")
        ),
    )

    result = CommandRunner().run_cmd(["docker", "exec"], timeout=7)

    assert result.returncode == 124
    assert result.stdout == "partial"
    assert "timed out after 7s" in result.stderr


def test_background_recovery_fails_when_worker_thread_does_not_stop():
    class Thread:
        def join(self, *, timeout):
            del timeout
            pass

        @staticmethod
        def is_alive():
            return True

    tracker = FaultTracker()
    tracker.register_background_control(control_id="control", stop_event=None, thread=Thread())

    assert tracker.stop_background("control") is False
    assert tracker.stop_background("control") is False


def test_netem_compensation_readback_error_tracks_residual(monkeypatch):
    injector = FaultInjector(topology_metadata=_metadata())
    qdisc_reads = 0

    def docker_exec(_container, command, **_kwargs):
        nonlocal qdisc_reads
        if command[:4] == ["tc", "-j", "qdisc", "show"]:
            return _Result(stdout='[ {"kind": "pfifo_fast"} ]')
        if command[:4] == ["tc", "qdisc", "replace", "dev"]:
            return _Result()
        if command[:4] == ["tc", "qdisc", "del", "dev"]:
            return _Result()
        if command[:4] == ["tc", "qdisc", "show", "dev"]:
            qdisc_reads += 1
            if qdisc_reads == 1:
                return _Result(stdout="qdisc pfifo_fast 0: root")
            return _Result(returncode=1, stderr="qdisc readback failed")
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(injector._impairment._cmd, "docker_exec", docker_exec)

    result = injector.inject_packet_loss("leaf1", "Ethernet0", loss_pct=10)

    assert result["success"] is False
    _assert_single_residual(injector, "packet_loss")


def test_mtu_failed_readback_and_compensation_tracks_residual(monkeypatch):
    injector = FaultInjector(topology_metadata=_metadata())
    attempts = iter(
        [
            (_Result(), 1500),
            (_Result(returncode=1, stderr="restore failed"), 1400),
        ]
    )
    monkeypatch.setattr(
        injector._impairment._iface,
        "get_interface_mtu",
        lambda _device, _interface: 9100,
    )
    monkeypatch.setattr(
        injector._impairment,
        "_apply_mtu",
        lambda *_args, **_kwargs: next(attempts),
    )

    result = injector.inject_mtu_mismatch("leaf1", "Ethernet0", mtu=1400)

    assert result["success"] is False
    _assert_single_residual(injector, "mtu_mismatch")


def test_blackhole_requires_operational_rib_and_compensates(monkeypatch):
    injector = FaultInjector(topology_metadata=_metadata())
    prefix = "192.168.102.2/32"
    configured = False
    commands: list[list[str]] = []

    def vtysh(_device, command):
        nonlocal configured
        commands.append(command)
        if command[0] == "configure terminal":
            configured = not any(item.startswith("no ip route") for item in command)
            return _Result()
        if command == ["show running-config"]:
            return _Result(stdout=f"ip route {prefix} Null0\n" if configured else "")
        if command == [f"show ip route {prefix}"]:
            return _Result(stdout=f"% Network {prefix} not in table\n")
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(injector._static_route._sonic, "vtysh", vtysh)

    result = injector.inject_blackhole_route("leaf1", prefix)

    assert result["success"] is False
    assert any(f"no ip route {prefix} Null0" in command for command in commands)
    assert injector.active_faults == []


def test_static_route_failed_compensation_tracks_residual(monkeypatch):
    injector = FaultInjector(topology_metadata=_metadata())
    target = "192.168.102.2/32"
    nexthop = "192.168.1.2"

    def vtysh(_device, command):
        if command == ["show running-config"]:
            return _Result(stdout=f"ip route {target} {nexthop}\n")
        if command == [f"show ip route {target}"]:
            return _Result(returncode=1, stderr="RIB unavailable")
        return _Result()

    monkeypatch.setattr(injector._static_route._sonic, "vtysh", vtysh)

    monkeypatch.setattr(injector._static_route, "_running_config_has_route", lambda *_: False)
    monkeypatch.setattr(injector._static_route._sonic._cmd, "docker_exec", lambda *_: _Result(stdout="0\n"))

    result = injector.inject_static_route_misconfig(
        "leaf1",
        target_ip=target,
        wrong_nexthop=nexthop,
    )

    assert result["success"] is False
    _assert_single_residual(injector, "static_route_misconfig")


def test_device_down_unreadable_state_is_terminal_residual(monkeypatch):
    injector = FaultInjector(topology_metadata=_metadata())
    monkeypatch.setattr(injector._system._cmd, "container_is_running", lambda _container: True)
    monkeypatch.setattr(injector._system._cmd, "run_cmd", lambda *_args, **_kwargs: _Result())
    monkeypatch.setattr(
        injector._system._cmd,
        "docker_exec",
        lambda *_args, **_kwargs: _Result(returncode=1),
    )
    monkeypatch.setattr(injector._system, "_settled_stop_state", lambda _container: (None, None))

    result = injector.inject_device_down("spine1")

    assert result["success"] is False
    _assert_single_residual(injector, "device_down")
    assert injector.active_faults[0].metadata["retryable"] is False


def test_device_down_reads_parking_namespace_without_sudo(monkeypatch):
    injector = FaultInjector(topology_metadata=_metadata())
    calls: list[list[str]] = []

    def run_cmd(command, **_kwargs):
        calls.append(command)
        return _Result(stdout="clab-park-clab-dcn-spine1 (id: 7)\n")

    monkeypatch.setattr(injector._system._cmd, "run_cmd", run_cmd)

    assert injector._system._parking_namespace_exists("clab-dcn-spine1") is True
    assert calls == [["ip", "netns", "list"]]


def test_device_down_running_with_restored_links_is_clean_injection_failure(monkeypatch):
    injector = FaultInjector(topology_metadata=_metadata())
    monkeypatch.setattr(injector._system._cmd, "container_is_running", lambda _container: True)
    monkeypatch.setattr(injector._system._cmd, "run_cmd", lambda *_args, **_kwargs: _Result())
    monkeypatch.setattr(injector._system, "_settled_stop_state", lambda _container: (True, False))

    result = injector.inject_device_down("spine1")

    assert result["success"] is False
    assert injector.active_faults == []


def test_device_down_stopped_without_parking_tracks_terminal_residual(monkeypatch):
    injector = FaultInjector(topology_metadata=_metadata())
    monkeypatch.setattr(injector._system._cmd, "container_is_running", lambda _container: True)
    monkeypatch.setattr(injector._system._cmd, "run_cmd", lambda *_args, **_kwargs: _Result(returncode=1))
    monkeypatch.setattr(injector._system, "_settled_stop_state", lambda _container: (False, False))

    result = injector.inject_device_down("spine1")

    assert result["success"] is False
    _assert_single_residual(injector, "device_down")
    assert injector.active_faults[0].metadata["retryable"] is False


def test_device_down_command_error_is_success_when_stopped_and_parked(monkeypatch):
    injector = FaultInjector(topology_metadata=_metadata())
    monkeypatch.setattr(injector._system._cmd, "container_is_running", lambda _container: True)
    monkeypatch.setattr(
        injector._system._cmd,
        "run_cmd",
        lambda *_args, **_kwargs: _Result(returncode=1, stderr="late Docker event"),
    )
    monkeypatch.setattr(injector._system, "_settled_stop_state", lambda _container: (False, True))

    result = injector.inject_device_down("spine1")

    assert result["success"] is True
    assert result["error"] is None
    assert len(injector.active_faults) == 1


def test_device_down_recovery_missing_parking_does_not_call_start(monkeypatch):
    injector = FaultInjector(topology_metadata=_metadata())
    monkeypatch.setattr(injector._system._cmd, "container_is_running", lambda _container: False)
    monkeypatch.setattr(injector._system, "_parking_namespace_exists", lambda _container: False)
    monkeypatch.setattr(
        injector._system._cmd,
        "run_cmd",
        lambda *_args, **_kwargs: pytest.fail("containerlab start must not run without parking state"),
    )

    result = injector.recover_device_down("spine1")

    assert result["recovered"] is False
    assert result["retryable"] is False
    assert "parking namespace is missing" in result["error"]
