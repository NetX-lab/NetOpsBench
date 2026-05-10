"""Interface naming and MTU helpers."""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Any

from netopsbench.platform.utils.interface_names import to_linux_interface, to_sonic_interface

if TYPE_CHECKING:
    from ..context import FaultContext
    from .command_runner import CommandRunner
    from .sonic_runtime import SonicRuntime

SONIC_MIN_INTERFACE_MTU = 68
SONIC_MAX_INTERFACE_MTU = 9216
SONIC_DEFAULT_INTERFACE_MTU = 9100


class InterfaceRuntime:
    """Interface naming resolution and MTU management."""

    def __init__(self, cmd: CommandRunner, sonic: SonicRuntime, ctx: FaultContext) -> None:
        self._cmd = cmd
        self._sonic = sonic
        self._ctx = ctx

    def resolve_linux(self, interface: str) -> str:
        return to_linux_interface(interface)

    def resolve_sonic(self, interface: str) -> str:
        return to_sonic_interface(interface)

    @staticmethod
    def is_valid_sonic_mtu(mtu: Any) -> bool:
        try:
            value = int(mtu)
        except (TypeError, ValueError):
            return False
        return SONIC_MIN_INTERFACE_MTU <= value <= SONIC_MAX_INTERFACE_MTU

    @staticmethod
    def parse_link_mtu(output: str) -> int | None:
        parts = (output or "").split()
        for token_index, token in enumerate(parts):
            if token == "mtu" and token_index + 1 < len(parts):
                candidate = parts[token_index + 1]
                if InterfaceRuntime.is_valid_sonic_mtu(candidate):
                    return int(candidate)
                break
        return None

    def get_common_port_mtu(self, device: str, exclude_interface: str | None = None) -> int:
        container = self._ctx.container_names.get(device)
        if not container:
            raise ValueError(f"Unknown device: {device}")

        result = self._cmd.docker_exec(container, ["sonic-db-cli", "CONFIG_DB", "keys", "PORT|*"])
        mtu_counts: Counter[int] = Counter()
        if result.returncode == 0:
            excluded_key = f"PORT|{exclude_interface}" if exclude_interface else None
            for raw_key in (result.stdout or "").splitlines():
                key = raw_key.strip()
                if not key.startswith("PORT|") or key == excluded_key:
                    continue
                mtu_result = self._cmd.docker_exec(container, ["sonic-db-cli", "CONFIG_DB", "hget", key, "mtu"])
                mtu = (mtu_result.stdout or "").strip()
                if self.is_valid_sonic_mtu(mtu):
                    mtu_counts[int(mtu)] += 1

        if mtu_counts:
            return max(mtu_counts.items(), key=lambda item: (item[1], item[0]))[0]
        return SONIC_DEFAULT_INTERFACE_MTU

    def get_interface_mtu(self, device: str, sonic_interface: str) -> int:
        container = self._ctx.container_names.get(device)
        if not container:
            raise ValueError(f"Unknown device: {device}")
        result = self._cmd.docker_exec(
            container, ["sonic-db-cli", "CONFIG_DB", "hget", f"PORT|{sonic_interface}", "mtu"]
        )
        raw = (result.stdout or "").strip()
        if self.is_valid_sonic_mtu(raw):
            return int(raw)

        result = self._cmd.docker_exec(container, ["ip", "-o", "link", "show", "dev", sonic_interface])
        live_mtu = self.parse_link_mtu(result.stdout if result.returncode == 0 else "")
        if live_mtu is not None:
            return live_mtu
        return self.get_common_port_mtu(device, exclude_interface=sonic_interface)

    def resolve_recovery_mtu(self, device: str, sonic_interface: str, original_mtu: int | None) -> int:
        if self.is_valid_sonic_mtu(original_mtu):
            return int(original_mtu)
        return self.get_common_port_mtu(device, exclude_interface=sonic_interface)
