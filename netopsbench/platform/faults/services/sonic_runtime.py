"""SONiC device interaction helpers."""

from __future__ import annotations

import re
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..context import FaultContext
    from .command_runner import CommandRunner


class SonicRuntime:
    """SONiC device command execution and BGP readiness checks."""

    def __init__(self, cmd: CommandRunner, ctx: FaultContext) -> None:
        self._cmd = cmd
        self._ctx = ctx

    def supervisord_ready(self, container: str) -> bool:
        result = self._cmd.docker_exec(container, ["supervisorctl", "pid"], timeout=10)
        return result.returncode == 0 and (result.stdout or "").strip().isdigit()

    def vtysh(self, device: str, commands: list[str]) -> subprocess.CompletedProcess:
        container = self._ctx.container_names.get(device)
        if not container:
            raise ValueError(f"Unknown device: {device}")
        cmd = ["vtysh"]
        for command in commands:
            cmd.extend(["-c", command])
        return self._cmd.docker_exec(container, cmd)

    def config_cmd(self, device: str, args: list[str]) -> subprocess.CompletedProcess:
        container = self._ctx.container_names.get(device)
        if not container:
            raise ValueError(f"Unknown device: {device}")
        return self._cmd.docker_exec(container, ["config"] + args)

    def services_ready(self, device: str) -> bool:
        result = self.vtysh(device, ["show ip bgp summary"])
        output = f"{result.stdout or ''}\n{result.stderr or ''}".lower()
        return (
            result.returncode == 0
            and "bgpd is not running" not in output
            and "failed to connect to any daemons" not in output
            and "% bgp instance not found" not in output
        )

    def reload_bgp_config(self, device: str) -> subprocess.CompletedProcess:
        container = self._ctx.container_names.get(device)
        if not container:
            raise ValueError(f"Unknown device: {device}")
        return self._cmd.docker_exec(container, ["vtysh", "-b"], timeout=60)

    def bgp_neighbor_states(self, output: str):
        states = []
        for raw_line in (output or "").splitlines():
            line = raw_line.strip()
            if not re.match(r"^\d+\.\d+\.\d+\.\d+", line):
                continue
            parts = line.split()
            if len(parts) < 10:
                continue
            states.append(parts[9])
        return states

    def bgp_neighbors_established(self, device: str) -> bool:
        result = self.vtysh(device, ["show ip bgp summary"])
        output = f"{result.stdout or ''}\n{result.stderr or ''}"
        output_lower = output.lower()
        if (
            result.returncode != 0
            or "bgpd is not running" in output_lower
            or "failed to connect to any daemons" in output_lower
            or "% bgp instance not found" in output_lower
        ):
            return False
        states = self.bgp_neighbor_states(output)
        return bool(states) and all(state.isdigit() for state in states)
