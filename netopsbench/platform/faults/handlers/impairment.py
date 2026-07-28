"""Impairment-oriented fault handlers."""

from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..context import FaultRuntimeContext
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
        ctx: FaultRuntimeContext,
    ) -> None:
        self._cmd = cmd
        self._sonic = sonic
        self._iface = iface
        self._tracker = tracker
        self._ctx = ctx

    def _qdisc_matches(
        self,
        container: str,
        linux_if: str,
        impairment: str,
        expected_value: float,
        unit: str,
    ) -> bool:
        result = self._cmd.docker_exec(container, ["tc", "qdisc", "show", "dev", linux_if])
        output = (result.stdout or "").lower()
        match = re.search(
            rf"\b{re.escape(impairment)}(?:\s+random)?\s+([0-9]+(?:\.[0-9]+)?)" rf"{re.escape(unit)}(?:\s|$)",
            output,
        )
        return (
            result.returncode == 0
            and "netem" in output
            and match is not None
            and math.isclose(float(match.group(1)), float(expected_value), rel_tol=0.0, abs_tol=0.001)
        )

    def _has_netem_qdisc(self, container: str, linux_if: str) -> bool | None:
        result = self._cmd.docker_exec(container, ["tc", "qdisc", "show", "dev", linux_if])
        if result.returncode != 0:
            return None
        return "netem" in (result.stdout or "").lower()

    def _finish_netem_injection(
        self,
        *,
        container: str,
        fault_info: dict[str, Any],
    ) -> dict[str, Any]:
        if fault_info["success"]:
            self._tracker.track(fault_info)
            return fault_info

        rollback = self._cmd.docker_exec(
            container,
            ["tc", "qdisc", "del", "dev", str(fault_info["interface"]), "root"],
        )
        compensated = (
            rollback.returncode in {0, 2}
            and self._has_netem_qdisc(
                container,
                str(fault_info["interface"]),
            )
            is False
        )
        if not compensated:
            error = "; ".join(
                filter(
                    None,
                    [
                        str(fault_info.get("error") or ""),
                        rollback.stderr or rollback.stdout or "netem compensation failed",
                    ],
                )
            )
            fault_info["error"] = error
            self._tracker.track_residual(fault_info, error)
        return fault_info

    def _apply_mtu(
        self,
        device: str,
        container: str,
        sonic_if: str,
        linux_if: str,
        target_mtu: int,
    ) -> tuple[Any, int]:
        result = self._sonic.config_cmd(device, ["interface", "mtu", sonic_if, str(target_mtu)])
        effective_mtu = self._iface.get_interface_mtu(device, sonic_if)
        if result.returncode != 0 or effective_mtu != target_mtu:
            result = self._cmd.docker_exec(
                container,
                ["ip", "link", "set", linux_if, "mtu", str(target_mtu)],
            )
            effective_mtu = self._iface.get_interface_mtu(device, sonic_if)
        return result, effective_mtu

    def inject_mtu_mismatch(self, device: str, interface: str, mtu: int = 1400) -> dict[str, Any]:
        container = self._ctx.container_names.get(device)
        if not container:
            raise ValueError(f"Unknown device: {device}")

        sonic_if = self._iface.resolve_sonic(interface)
        linux_if = self._iface.resolve_linux(interface)
        original_mtu = self._iface.get_interface_mtu(device, sonic_if)
        result, effective_mtu = self._apply_mtu(device, container, sonic_if, linux_if, int(mtu))
        success = result.returncode == 0 and effective_mtu == int(mtu)
        fault_info = {
            "type": "mtu_mismatch",
            "device": device,
            "interface": sonic_if,
            "linux_interface": linux_if,
            "mtu": mtu,
            "original_mtu": original_mtu,
            "success": success,
            "error": (None if success else result.stderr or f"effective MTU is {effective_mtu}, expected {int(mtu)}"),
        }
        if success:
            self._tracker.track(fault_info)
            return fault_info

        rollback_result, restored_mtu = self._apply_mtu(
            device,
            container,
            sonic_if,
            linux_if,
            original_mtu,
        )
        compensated = rollback_result.returncode == 0 and restored_mtu == original_mtu
        if not compensated:
            error = "; ".join(
                filter(
                    None,
                    [
                        str(fault_info.get("error") or ""),
                        rollback_result.stderr or f"MTU compensation read back {restored_mtu}, expected {original_mtu}",
                    ],
                )
            )
            fault_info["error"] = error
            self._tracker.track_residual(fault_info, error)
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
        result, effective_mtu = self._apply_mtu(
            device,
            container,
            sonic_if,
            linux_if,
            target_mtu,
        )
        recovered = result.returncode == 0 and effective_mtu == target_mtu
        if recovered:
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
            "recovered": recovered,
            "error": (
                None if recovered else result.stderr or f"effective MTU is {effective_mtu}, expected {target_mtu}"
            ),
        }

    def inject_packet_corruption(
        self,
        device: str,
        interface: str = "Ethernet0",
        corruption_pct: float = 20,
    ) -> dict[str, Any]:
        container = self._ctx.container_names.get(device)
        if not container:
            raise ValueError(f"Unknown device: {device}")

        linux_if = self._iface.resolve_linux(interface)
        result = self._cmd.docker_exec(
            container,
            ["tc", "qdisc", "replace", "dev", linux_if, "root", "netem", "corrupt", f"{corruption_pct}%"],
        )

        success = result.returncode == 0 and self._qdisc_matches(container, linux_if, "corrupt", corruption_pct, "%")
        fault_info = {
            "type": "packet_corruption",
            "device": device,
            "interface": linux_if,
            "corruption_pct": corruption_pct,
            "success": success,
            "error": None if success else result.stderr or "netem corruption rule was not observed",
        }
        return self._finish_netem_injection(container=container, fault_info=fault_info)

    def inject_packet_loss(
        self,
        device: str,
        interface: str = "Ethernet0",
        loss_pct: float = 30,
    ) -> dict[str, Any]:
        container = self._ctx.container_names.get(device)
        if not container:
            raise ValueError(f"Unknown device: {device}")

        linux_if = self._iface.resolve_linux(interface)
        result = self._cmd.docker_exec(
            container,
            ["tc", "qdisc", "replace", "dev", linux_if, "root", "netem", "loss", f"{loss_pct}%"],
        )

        success = result.returncode == 0 and self._qdisc_matches(container, linux_if, "loss", loss_pct, "%")
        fault_info = {
            "type": "packet_loss",
            "device": device,
            "interface": linux_if,
            "loss_pct": loss_pct,
            "success": success,
            "error": None if success else result.stderr or "netem loss rule was not observed",
        }
        return self._finish_netem_injection(container=container, fault_info=fault_info)

    def inject_high_latency(
        self,
        device: str,
        interface: str = "Ethernet0",
        latency_ms: float = 100,
    ) -> dict[str, Any]:
        container = self._ctx.container_names.get(device)
        if not container:
            raise ValueError(f"Unknown device: {device}")

        linux_if = self._iface.resolve_linux(interface)
        result = self._cmd.docker_exec(
            container,
            ["tc", "qdisc", "replace", "dev", linux_if, "root", "netem", "delay", f"{latency_ms}ms"],
        )

        success = result.returncode == 0 and self._qdisc_matches(container, linux_if, "delay", latency_ms, "ms")
        fault_info = {
            "type": "high_latency",
            "device": device,
            "interface": linux_if,
            "latency_ms": latency_ms,
            "success": success,
            "error": None if success else result.stderr or "netem delay rule was not observed",
        }
        return self._finish_netem_injection(container=container, fault_info=fault_info)

    def recover_tc_rules(self, device: str, interface: str = "Ethernet0") -> dict[str, Any]:
        container = self._ctx.container_names.get(device)
        if not container:
            raise ValueError(f"Unknown device: {device}")

        linux_if = self._iface.resolve_linux(interface)
        result = self._cmd.docker_exec(container, ["tc", "qdisc", "del", "dev", linux_if, "root"])
        recovered = result.returncode in {0, 2} and self._has_netem_qdisc(container, linux_if) is False

        if recovered:
            self._tracker.remove_faults(
                lambda fault: fault["device"] == device
                and fault.get("interface") == linux_if
                and fault["type"] in ["packet_corruption", "packet_loss", "high_latency"]
            )

        return {
            "device": device,
            "interface": linux_if,
            "recovered": recovered,
            "error": None if recovered else result.stderr or "netem qdisc remained installed",
        }
