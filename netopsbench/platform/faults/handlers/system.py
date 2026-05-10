"""System-level fault handlers."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from netopsbench.platform.utils.proc import sudo_prefix

if TYPE_CHECKING:
    from ..context import FaultContext
    from ..services.command_runner import CommandRunner
    from ..services.interface_runtime import InterfaceRuntime
    from ..services.sonic_runtime import SonicRuntime
    from ..services.topology_runtime import TopologyRuntime
    from ..services.tracking import FaultTracker


class SystemHandler:
    """Handles device-level (system) fault injection and recovery."""

    def __init__(
        self,
        cmd: CommandRunner,
        sonic: SonicRuntime,
        iface: InterfaceRuntime,
        topo_rt: TopologyRuntime,
        tracker: FaultTracker,
        ctx: FaultContext,
    ) -> None:
        self._cmd = cmd
        self._sonic = sonic
        self._iface = iface
        self._topo_rt = topo_rt
        self._tracker = tracker
        self._ctx = ctx

    def inject_device_down(self, device: str) -> dict[str, Any]:
        container = self._ctx.container_names.get(device)
        if not container:
            raise ValueError(f"Unknown device: {device}")

        interfaces = self._topo_rt.configured_device_interfaces(device)
        interface_errors: list[str] = []
        for interface in interfaces:
            result = self._sonic.config_cmd(device, ["interface", "shutdown", interface])
            if result.returncode != 0:
                linux_if = self._iface.resolve_linux(interface)
                result = self._cmd.docker_exec(container, ["ip", "link", "set", linux_if, "down"])
            if result.returncode != 0:
                interface_errors.append(f"{interface}: {(result.stderr or result.stdout or '').strip()}")

        bgpd_result = self._cmd.docker_exec(container, ["supervisorctl", "stop", "bgpd"], timeout=30)
        bgpd_error = None
        if bgpd_result.returncode != 0:
            bgpd_error = (bgpd_result.stderr or bgpd_result.stdout or "").strip()

        success = not interface_errors and bgpd_result.returncode == 0
        fault_info = {
            "type": "device_down",
            "device": device,
            "container": container,
            "mode": "service_shutdown",
            "interfaces": interfaces,
            "bgpd_stopped": bgpd_result.returncode == 0,
            "success": success,
            "error": "; ".join(filter(None, interface_errors + ([bgpd_error] if bgpd_error else []))) or None,
        }
        if fault_info["success"]:
            self._tracker.track(fault_info)
        return fault_info

    def recover_device_down(self, device: str, interfaces: list[str] | None = None) -> dict[str, Any]:
        container = self._ctx.container_names.get(device)
        if not container:
            raise ValueError(f"Unknown device: {device}")

        interfaces = interfaces or self._topo_rt.configured_device_interfaces(device)

        container_started = self._cmd.container_is_running(container)
        bootstrapped_supervisord = False
        restored_runtime_config = False

        if not container_started:
            start_result = self._cmd.run_cmd([*sudo_prefix(), "docker", "start", container], timeout=60)
            if start_result.returncode != 0:
                return {
                    "type": "device_down",
                    "device": device,
                    "recovered": False,
                    "container_started": False,
                    "sonic_ready": False,
                    "bootstrapped_supervisord": False,
                    "restored_runtime_config": False,
                    "error": start_result.stderr if start_result.stderr else start_result.stdout,
                }
            container_started = True

        ready = False
        last_error = ""
        for attempt in range(30):
            if not self._cmd.container_is_running(container):
                last_error = "container exited during recovery"
                break

            supervisord_ready = self._sonic.supervisord_ready(container)
            if attempt >= 2 and not bootstrapped_supervisord and not supervisord_ready:
                bootstrap = self._cmd.docker_exec_detached(container, ["/usr/local/bin/supervisord"], timeout=10)
                if bootstrap.returncode == 0:
                    bootstrapped_supervisord = True
                else:
                    last_error = (bootstrap.stderr or bootstrap.stdout or "").strip()
            elif supervisord_ready and not restored_runtime_config:
                restore_errors: list[str] = []
                for interface in interfaces:
                    restore = self._sonic.config_cmd(device, ["interface", "startup", interface])
                    if restore.returncode != 0:
                        linux_if = self._iface.resolve_linux(interface)
                        restore = self._cmd.docker_exec(container, ["ip", "link", "set", linux_if, "up"])
                    if restore.returncode != 0:
                        restore_errors.append(f"{interface}: {(restore.stderr or restore.stdout or '').strip()}")

                bgpd_start = self._cmd.docker_exec(container, ["supervisorctl", "start", "bgpd"], timeout=30)
                bgpd_output = (bgpd_start.stderr or bgpd_start.stdout or "").strip()
                if bgpd_start.returncode != 0 and "already started" not in bgpd_output.lower():
                    restore_errors.append(bgpd_output)

                if not restore_errors:
                    reload_result = self._sonic.reload_bgp_config(device)
                    if reload_result.returncode != 0:
                        restore_errors.append((reload_result.stderr or reload_result.stdout or "").strip())

                if not restore_errors:
                    restored_runtime_config = True
                else:
                    last_error = "; ".join(filter(None, restore_errors)) or last_error

            if restored_runtime_config and self._sonic.bgp_neighbors_established(device):
                ready = True
                break

            bgp_result = self._sonic.vtysh(device, ["show ip bgp summary"])
            last_error = (bgp_result.stderr or bgp_result.stdout or "").strip() or last_error
            time.sleep(5)

        if ready:
            self._tracker.remove_faults(lambda fault: fault["type"] == "device_down" and fault["device"] == device)

        return {
            "type": "device_down",
            "device": device,
            "recovered": ready,
            "container_started": True,
            "sonic_ready": ready,
            "bootstrapped_supervisord": bootstrapped_supervisord,
            "restored_runtime_config": restored_runtime_config,
            "error": None if ready else (last_error or "SONiC services not ready after docker start"),
        }
