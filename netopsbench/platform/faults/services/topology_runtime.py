"""Topology-aware selection and config loading helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from netopsbench.platform.topology.configdb_payload import interface_names_for_config

if TYPE_CHECKING:
    from ..context import FaultRuntimeContext
    from .interface_runtime import InterfaceRuntime
    from .sonic_runtime import SonicRuntime


class TopologyRuntime:
    """Topology-aware config loading and device selection."""

    def __init__(self, sonic: SonicRuntime, iface: InterfaceRuntime, ctx: FaultRuntimeContext) -> None:
        self._sonic = sonic
        self._iface = iface
        self._ctx = ctx

    def device_config_path(self, device: str) -> str:
        return str(self._ctx.clab_dir / "configs" / "sonic" / device / "config_db.json")

    def load_device_config_lines(self, device: str) -> list[str]:
        result = self._sonic.vtysh(device, ["show running-config"])
        if result.returncode == 0:
            return (result.stdout or "").splitlines()
        return []

    def configured_device_interfaces(self, device: str) -> list[str]:
        configdb_interfaces = interface_names_for_config(self.device_config_path(device))
        return [self._iface.resolve_sonic(interface) for interface in configdb_interfaces]
