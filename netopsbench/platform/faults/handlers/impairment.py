"""Impairment-oriented fault handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..context import FaultContext
    from ..services.command_runner import CommandRunner
    from ..services.interface_runtime import InterfaceRuntime
    from ..services.sonic_runtime import SonicRuntime
    from ..services.tracking import FaultTracker


class ImpairmentHandler:
    """Handles MTU mismatch, packet corruption/loss, and high latency faults."""

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

    def inject_mtu_mismatch(self, device: str, interface: str, mtu: int = 1400) -> dict[str, Any]:
        container = self._ctx.container_names.get(device)
        if not container:
            raise ValueError(f"Unknown device: {device}")

        sonic_if = self._iface.resolve_sonic(interface)
        linux_if = self._iface.resolve_linux(interface)
        original_mtu = self._iface.get_interface_mtu(device, sonic_if)
        result = self._sonic.config_cmd(device, ["interface", "mtu", sonic_if, str(mtu)])
        if result.returncode != 0:
            result = self._cmd.docker_exec(container, ["ip", "link", "set", linux_if, "mtu", str(mtu)])

        fault_info = {
            "type": "mtu_mismatch",
            "device": device,
            "interface": sonic_if,
            "linux_interface": linux_if,
            "mtu": mtu,
            "original_mtu": original_mtu,
            "success": result.returncode == 0,
            "error": result.stderr if result.returncode != 0 else None,
        }
        if fault_info["success"]:
            self._tracker.track(fault_info)
        return fault_info

    def recover_mtu_mismatch(
        self,
        device: str,
        interface: str,
        original_mtu: int | None = None,
    ) -> dict[str, Any]:
        container = self._ctx.container_names.get(device)
        if not container:
            raise ValueError(f"Unknown device: {device}")

        sonic_if = self._iface.resolve_sonic(interface)
        linux_if = self._iface.resolve_linux(interface)
        target_mtu = self._iface.resolve_recovery_mtu(device, sonic_if, original_mtu)
        result = self._sonic.config_cmd(device, ["interface", "mtu", sonic_if, str(target_mtu)])
        if result.returncode != 0:
            result = self._cmd.docker_exec(container, ["ip", "link", "set", linux_if, "mtu", str(target_mtu)])

        self._tracker.remove_faults(
            lambda fault: fault["type"] == "mtu_mismatch"
            and fault["device"] == device
            and fault["interface"] == sonic_if
        )

        return {
            "type": "mtu_mismatch",
            "device": device,
            "interface": sonic_if,
            "restored_mtu": target_mtu,
            "recovered": result.returncode == 0,
            "error": result.stderr if result.returncode != 0 else None,
        }

    def inject_packet_corruption(
        self,
        device: str,
        interface: str = "Ethernet0",
        corruption_pct: int = 20,
    ) -> dict[str, Any]:
        container = self._ctx.container_names.get(device)
        if not container:
            raise ValueError(f"Unknown device: {device}")

        linux_if = self._iface.resolve_linux(interface)
        result = self._cmd.docker_exec(
            container,
            ["tc", "qdisc", "replace", "dev", linux_if, "root", "netem", "corrupt", f"{corruption_pct}%"],
        )

        fault_info = {
            "type": "packet_corruption",
            "device": device,
            "interface": linux_if,
            "corruption_pct": corruption_pct,
            "success": result.returncode == 0,
            "error": result.stderr if result.returncode != 0 else None,
        }
        if fault_info["success"]:
            self._tracker.track(fault_info)
        return fault_info

    def inject_packet_loss(
        self,
        device: str,
        interface: str = "Ethernet0",
        loss_pct: int = 10,
    ) -> dict[str, Any]:
        container = self._ctx.container_names.get(device)
        if not container:
            raise ValueError(f"Unknown device: {device}")

        linux_if = self._iface.resolve_linux(interface)
        result = self._cmd.docker_exec(
            container,
            ["tc", "qdisc", "replace", "dev", linux_if, "root", "netem", "loss", f"{loss_pct}%"],
        )

        fault_info = {
            "type": "packet_loss",
            "device": device,
            "interface": linux_if,
            "loss_pct": loss_pct,
            "success": result.returncode == 0,
            "error": result.stderr if result.returncode != 0 else None,
        }
        if fault_info["success"]:
            self._tracker.track(fault_info)
        return fault_info

    def inject_high_latency(
        self,
        device: str,
        interface: str = "Ethernet0",
        latency_ms: int = 100,
    ) -> dict[str, Any]:
        container = self._ctx.container_names.get(device)
        if not container:
            raise ValueError(f"Unknown device: {device}")

        linux_if = self._iface.resolve_linux(interface)
        result = self._cmd.docker_exec(
            container,
            ["tc", "qdisc", "replace", "dev", linux_if, "root", "netem", "delay", f"{latency_ms}ms"],
        )

        fault_info = {
            "type": "high_latency",
            "device": device,
            "interface": linux_if,
            "latency_ms": latency_ms,
            "success": result.returncode == 0,
            "error": result.stderr if result.returncode != 0 else None,
        }
        if fault_info["success"]:
            self._tracker.track(fault_info)
        return fault_info

    def recover_tc_rules(self, device: str, interface: str = "Ethernet0") -> dict[str, Any]:
        container = self._ctx.container_names.get(device)
        if not container:
            raise ValueError(f"Unknown device: {device}")

        linux_if = self._iface.resolve_linux(interface)
        self._cmd.docker_exec(container, ["tc", "qdisc", "del", "dev", linux_if, "root"])

        self._tracker.remove_faults(
            lambda fault: fault["device"] == device
            and fault.get("interface") == linux_if
            and fault["type"] in ["packet_corruption", "packet_loss", "high_latency"]
        )

        return {"device": device, "interface": linux_if, "recovered": True}
