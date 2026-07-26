"""Focused transaction tests for built-in fault handlers."""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass

from netopsbench.platform.faults.injector import FaultInjector
from netopsbench.platform.faults.services.command_runner import CommandRunner
from netopsbench.platform.faults.services.tracking import FaultTracker
from netopsbench.platform.topology.generator import generate_topology


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


def test_acl_readback_failure_is_compensated(monkeypatch):
    injector = FaultInjector(topology_metadata=_metadata())
    calls: list[tuple[str, ...]] = []
    rule_present = False
    table_present = False
    acl_rule_present = False

    def docker_exec(_container, command, **_kwargs):
        nonlocal rule_present, table_present, acl_rule_present
        calls.append(tuple(command))
        if command[:2] == ["iptables", "-I"]:
            rule_present = True
        elif command[:2] == ["iptables", "-D"]:
            rule_present = False
        elif command[:2] == ["iptables", "-C"]:
            return _Result(returncode=0 if rule_present else 1)
        elif command[:3] == ["sonic-db-cli", "CONFIG_DB", "hset"]:
            if "ACL_TABLE|" in command[3]:
                table_present = True
            else:
                # Simulate a partially applied ACL breadcrumb.
                acl_rule_present = False
        elif command[:3] == ["sonic-db-cli", "CONFIG_DB", "del"]:
            if "ACL_TABLE|" in command[3]:
                table_present = False
            else:
                acl_rule_present = False
        elif command[:3] == ["sonic-db-cli", "CONFIG_DB", "exists"]:
            present = table_present if "ACL_TABLE|" in command[3] else acl_rule_present
            return _Result(stdout="1" if present else "0")
        return _Result()

    monkeypatch.setattr(injector._acl._cmd, "docker_exec", docker_exec)

    result = injector.inject_acl_misconfig(
        "leaf1",
        target_prefix="192.168.102.0/30",
        interface="Ethernet0",
    )

    assert result["success"] is False
    assert any(command[:2] == ("iptables", "-D") for command in calls)
    assert injector.active_faults == []


def test_netem_compensation_readback_error_tracks_residual(monkeypatch):
    injector = FaultInjector(topology_metadata=_metadata())
    qdisc_reads = 0

    def docker_exec(_container, command, **_kwargs):
        nonlocal qdisc_reads
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


def test_bgp_unreadable_state_is_compensated(monkeypatch):
    injector = FaultInjector(topology_metadata=_metadata())
    commands: list[list[str]] = []
    states = iter([None] * 10 + [True])
    monkeypatch.setattr(
        injector._bgp._routing,
        "pick_bgp_neighbor",
        lambda _device, peer_ip=None: {
            "peer_ip": peer_ip or "192.168.11.1",
            "local_as": 65101,
            "remote_as": 65000,
        },
    )
    monkeypatch.setattr(
        injector._bgp._routing,
        "get_device_asn",
        lambda _device: 65101,
    )
    monkeypatch.setattr(
        injector._bgp._routing,
        "normalize_bgp_neighbor_kind",
        lambda kind: kind,
    )
    monkeypatch.setattr(
        injector._bgp._sonic,
        "vtysh",
        lambda _device, command: commands.append(command) or _Result(),
    )
    monkeypatch.setattr(
        injector._bgp._sonic,
        "bgp_neighbor_state",
        lambda _device, _peer: next(states),
    )
    monkeypatch.setattr(
        "netopsbench.platform.faults.handlers.routing_bgp.time.sleep",
        lambda _seconds: None,
    )

    result = injector.inject_bgp_neighbor_misconfig(
        "leaf1",
        peer_ip="192.168.11.1",
        misconfig_kind="peer_as_mismatch",
    )

    assert result["success"] is False
    assert any("neighbor 192.168.11.1 remote-as 65000" in command for command in commands)
    assert injector.active_faults == []


def test_bgp_failed_compensation_tracks_residual(monkeypatch):
    injector = FaultInjector(topology_metadata=_metadata())
    monkeypatch.setattr(
        injector._bgp._routing,
        "pick_bgp_neighbor",
        lambda _device, peer_ip=None: {
            "peer_ip": peer_ip or "192.168.11.1",
            "local_as": 65101,
            "remote_as": 65000,
        },
    )
    monkeypatch.setattr(injector._bgp._routing, "get_device_asn", lambda _device: 65101)
    monkeypatch.setattr(
        injector._bgp._routing,
        "normalize_bgp_neighbor_kind",
        lambda kind: kind,
    )
    monkeypatch.setattr(injector._bgp._sonic, "vtysh", lambda _device, _command: _Result())
    monkeypatch.setattr(
        injector._bgp._sonic,
        "bgp_neighbor_state",
        lambda _device, _peer: None,
    )
    monkeypatch.setattr(
        "netopsbench.platform.faults.handlers.routing_bgp.time.sleep",
        lambda _seconds: None,
    )

    result = injector.inject_bgp_neighbor_misconfig(
        "leaf1",
        peer_ip="192.168.11.1",
        misconfig_kind="peer_as_mismatch",
    )

    assert result["success"] is False
    _assert_single_residual(injector, "bgp_neighbor_misconfig")


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
    nexthop = "192.168.101.2"

    def vtysh(_device, command):
        if command == ["show running-config"]:
            return _Result(stdout=f"ip route {target} {nexthop}\n")
        if command == [f"show ip route {target}"]:
            return _Result(returncode=1, stderr="RIB unavailable")
        return _Result()

    monkeypatch.setattr(injector._static_route._sonic, "vtysh", vtysh)

    result = injector.inject_static_route_misconfig(
        "leaf1",
        target_ip=target,
        wrong_nexthop=nexthop,
    )

    assert result["success"] is False
    _assert_single_residual(injector, "static_route_misconfig")


def test_route_policy_checks_withdrawal_and_recovery(monkeypatch):
    injector = FaultInjector(topology_metadata=_metadata())
    prefix = "192.168.101.0/30"
    statement = f"network {prefix}"
    configured = True
    bgp_reads = 0

    monkeypatch.setattr(
        injector._route_policy._routing,
        "normalize_route_policy_kind",
        lambda kind: kind,
    )
    monkeypatch.setattr(
        injector._route_policy._routing,
        "pick_advertised_network",
        lambda _device, prefix=None: {
            "prefix": prefix or "192.168.101.0/30",
            "local_as": 65101,
            "route_map": None,
        },
    )
    monkeypatch.setattr(
        injector._route_policy._routing,
        "get_device_asn",
        lambda _device: 65101,
    )
    monkeypatch.setattr(
        injector._route_policy._routing,
        "format_network_statement",
        lambda _prefix, _route_map: statement,
    )

    def vtysh(_device, command):
        nonlocal configured, bgp_reads
        if command[0] == "configure terminal":
            configured = not any(item == f"no {statement}" for item in command)
            return _Result()
        if command == ["show running-config"]:
            return _Result(stdout=f" {statement}\n" if configured else "")
        if command == [f"show ip bgp {prefix}"]:
            bgp_reads += 1
            if not configured:
                return _Result(returncode=1, stderr="BGP RIB unavailable")
            return _Result(stdout=f"BGP routing table entry for {prefix}\n")
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(injector._route_policy._sonic, "vtysh", vtysh)
    monkeypatch.setattr(
        "netopsbench.platform.faults.handlers.routing_policy.time.sleep",
        lambda _seconds: None,
    )

    result = injector.inject_route_policy_misconfig(
        "leaf1",
        target_prefix=prefix,
        misconfig_kind="network_statement_missing",
    )

    assert result["success"] is False
    assert configured is True
    assert bgp_reads == 11
    assert injector.active_faults == []


def test_device_down_unreadable_state_is_compensated(monkeypatch):
    injector = FaultInjector(topology_metadata=_metadata())
    compensation_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(injector._system._cmd, "run_cmd", lambda *_args, **_kwargs: _Result())
    monkeypatch.setattr(
        injector._system._cmd,
        "container_is_running",
        lambda _container: None,
    )
    monkeypatch.setattr(
        injector._system,
        "_start_and_wait",
        lambda device, container: compensation_calls.append((device, container)) or (True, ""),
    )

    result = injector.inject_device_down("spine1")

    assert result["success"] is False
    assert compensation_calls == [("spine1", "clab-dcn-spine1")]
    assert injector.active_faults == []


def test_device_down_failed_compensation_tracks_residual(monkeypatch):
    injector = FaultInjector(topology_metadata=_metadata())
    monkeypatch.setattr(injector._system._cmd, "run_cmd", lambda *_args, **_kwargs: _Result())
    monkeypatch.setattr(
        injector._system._cmd,
        "container_is_running",
        lambda _container: None,
    )
    monkeypatch.setattr(
        injector._system,
        "_start_and_wait",
        lambda _device, _container: (False, "start readback failed"),
    )

    result = injector.inject_device_down("spine1")

    assert result["success"] is False
    _assert_single_residual(injector, "device_down")
