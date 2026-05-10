"""Topology-aware selection and config loading helpers."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..context import FaultContext
    from .interface_runtime import InterfaceRuntime
    from .sonic_runtime import SonicRuntime


class TopologyRuntime:
    """Topology-aware config loading and device selection."""

    def __init__(self, sonic: SonicRuntime, iface: InterfaceRuntime, ctx: FaultContext) -> None:
        self._sonic = sonic
        self._iface = iface
        self._ctx = ctx

    def is_client_device(self, device: str) -> bool:
        if not device:
            return False
        if device.startswith("client"):
            return True
        return any(c.get("name") == device for c in self._ctx.clients)

    def pick_client_pair(self) -> dict[str, Any] | None:
        if not self._ctx.clients or len(self._ctx.clients) < 2:
            return None
        clients = sorted(self._ctx.clients, key=lambda c: c.get("name", ""))
        for i, c1 in enumerate(clients):
            for c2 in clients[i + 1 :]:
                if c1.get("leaf") and c2.get("leaf") and c1.get("leaf") != c2.get("leaf"):
                    return {"client1": c1, "client2": c2}
        return {"client1": clients[0], "client2": clients[1]}

    def device_config_path(self, device: str) -> str:
        return os.path.join(self._ctx.clab_dir, "configs", f"{device}.sh")

    def load_device_config_lines(self, device: str) -> list[str]:
        config_path = self.device_config_path(device)
        if os.path.exists(config_path):
            with open(config_path, encoding="utf-8") as handle:
                return handle.read().splitlines()

        result = self._sonic.vtysh(device, ["show running-config"])
        if result.returncode == 0:
            return (result.stdout or "").splitlines()
        return []

    def configured_device_interfaces(self, device: str) -> list[str]:
        interfaces: list[str] = []
        seen = set()
        for raw_line in self.load_device_config_lines(device):
            line = raw_line.strip()
            parts = line.split()
            if line.startswith("config interface startup") and len(parts) >= 4:
                interface = self._iface.resolve_sonic(parts[3])
            elif line.startswith("config interface ip add") and len(parts) >= 5:
                interface = self._iface.resolve_sonic(parts[4])
            else:
                continue
            if interface not in seen:
                seen.add(interface)
                interfaces.append(interface)
        return interfaces
