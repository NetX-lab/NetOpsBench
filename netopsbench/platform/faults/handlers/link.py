"""Link fault handlers."""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..context import FaultContext
    from ..services.command_runner import CommandRunner
    from ..services.interface_runtime import InterfaceRuntime
    from ..services.sonic_runtime import SonicRuntime
    from ..services.tracking import FaultTracker


class LinkHandler:
    """Handles link_down and link_flapping fault injection and recovery."""

    def __init__(
        self,
        cmd: CommandRunner,
        sonic: SonicRuntime,
        iface: InterfaceRuntime,
        tracker: FaultTracker,
        ctx: FaultContext,
    ) -> None:
        self._cmd = cmd
        self._sonic = sonic
        self._iface = iface
        self._tracker = tracker
        self._ctx = ctx

    def _set_link_admin_state(
        self,
        device: str,
        container: str,
        sonic_if: str,
        linux_if: str,
        *,
        enabled: bool,
    ) -> dict[str, Any]:
        action = "startup" if enabled else "shutdown"
        fallback_state = "up" if enabled else "down"
        result = self._sonic.config_cmd(device, ["interface", action, sonic_if])
        if result.returncode != 0:
            result = self._cmd.docker_exec(container, ["ip", "link", "set", linux_if, fallback_state])
        return {
            "success": result.returncode == 0,
            "error": None if result.returncode == 0 else (result.stderr or result.stdout or "").strip() or None,
        }

    def inject_link_down(self, device: str, interface: str) -> dict[str, Any]:
        """
        Inject link down fault by disabling an interface.

        Args:
            device: Device name (e.g., 'spine1')
            interface: Interface name (e.g., 'Ethernet0' or 'eth1')

        Returns:
            Injection result with recovery info
        """
        container = self._ctx.container_names.get(device)
        if not container:
            raise ValueError(f"Unknown device: {device}")

        sonic_if = self._iface.resolve_sonic(interface)
        linux_if = self._iface.resolve_linux(interface)
        result = self._set_link_admin_state(device, container, sonic_if, linux_if, enabled=False)

        fault_info = {
            "type": "link_down",
            "device": device,
            "interface": sonic_if,
            "linux_interface": linux_if,
            "container": container,
            "success": result["success"],
            "error": result["error"],
        }

        if fault_info["success"]:
            self._tracker.track(fault_info)

        return fault_info

    def recover_link_down(self, device: str, interface: str) -> dict[str, Any]:
        """Recover from link down by enabling the interface."""
        container = self._ctx.container_names.get(device)
        if not container:
            raise ValueError(f"Unknown device: {device}")

        sonic_if = self._iface.resolve_sonic(interface)
        linux_if = self._iface.resolve_linux(interface)
        result = self._set_link_admin_state(device, container, sonic_if, linux_if, enabled=True)

        self._tracker.remove_faults(
            lambda fault: fault["type"] == "link_down" and fault["device"] == device and fault["interface"] == sonic_if
        )

        return {
            "type": "link_down",
            "device": device,
            "interface": sonic_if,
            "recovered": result["success"],
            "error": result["error"],
        }

    def inject_link_flapping(
        self,
        device: str = "spine1",
        interface: str = "Ethernet0",
        iterations: int = 10,
        down_time: int = 2,
        up_time: int = 3,
    ) -> dict[str, Any]:
        """Inject link flapping using Python orchestration instead of a shell helper."""
        container = self._ctx.container_names.get(device)
        if not container:
            raise ValueError(f"Unknown device: {device}")

        sonic_if = self._iface.resolve_sonic(interface)
        linux_if = self._iface.resolve_linux(interface)
        control_id = f"link-flap:{device}:{sonic_if}:{time.time_ns()}"
        stop_event = threading.Event()

        def _run_flap_loop() -> None:
            for _ in range(max(int(iterations), 0)):
                if stop_event.is_set():
                    break
                self._set_link_admin_state(device, container, sonic_if, linux_if, enabled=False)
                if stop_event.wait(max(float(down_time), 0.0)):
                    break
                self._set_link_admin_state(device, container, sonic_if, linux_if, enabled=True)
                if stop_event.wait(max(float(up_time), 0.0)):
                    break
            self._set_link_admin_state(device, container, sonic_if, linux_if, enabled=True)
            self._tracker.stop_background(control_id, join_timeout=0)

        thread = threading.Thread(target=_run_flap_loop, name=f"{control_id}-worker", daemon=True)
        self._tracker.register_background_control(control_id, stop_event=stop_event, thread=thread)
        thread.start()

        fault_info = {
            "type": "link_flapping",
            "device": device,
            "interface": sonic_if,
            "linux_interface": linux_if,
            "iterations": iterations,
            "down_time": down_time,
            "up_time": up_time,
            "task_id": control_id,
            "orchestration": "python",
            "success": True,
        }

        self._tracker.track(fault_info)
        return fault_info
