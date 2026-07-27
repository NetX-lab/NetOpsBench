"""System-level fault handlers."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from netopsbench.platform.runtime.apply_configs import activate_device
from netopsbench.platform.utils.proc import docker_prefix, sudo_prefix

if TYPE_CHECKING:
    from ..context import FaultRuntimeContext
    from ..services.command_runner import CommandRunner
    from ..services.sonic_runtime import SonicRuntime
    from ..services.tracking import FaultTracker


class SystemHandler:
    """Handles device-level (system) fault injection and recovery."""

    _CONTAINERLAB_TIMEOUT = "20s"
    _ACTIVATION_MAX_TRIES = 36
    _BGP_MAX_TRIES = 20

    def __init__(
        self,
        cmd: CommandRunner,
        sonic: SonicRuntime,
        tracker: FaultTracker,
        ctx: FaultRuntimeContext,
    ) -> None:
        self._cmd = cmd
        self._sonic = sonic
        self._tracker = tracker
        self._ctx = ctx

    def _containerlab_node_command(self, operation: str, device: str) -> list[str]:
        return [
            *sudo_prefix(),
            "containerlab",
            operation,
            "-t",
            str(self._ctx.topology_file),
            "--node",
            device,
            "--timeout",
            self._CONTAINERLAB_TIMEOUT,
        ]

    def _start_and_wait(self, device: str, container: str) -> tuple[bool, str]:
        start = self._cmd.run_cmd(self._containerlab_node_command("start", device), timeout=60)
        running_state = self._cmd.container_is_running(container)
        if start.returncode != 0 and running_state is not True:
            return False, (start.stderr or start.stdout or "").strip() or "containerlab node start failed"

        if not self._sonic.supervisord_ready(container):
            supervisor = self._cmd.run_cmd(
                [
                    *docker_prefix(),
                    "docker",
                    "exec",
                    "-d",
                    container,
                    "/usr/local/bin/supervisord",
                ],
                timeout=30,
            )
            if supervisor.returncode != 0:
                return False, (supervisor.stderr or supervisor.stdout or "").strip() or "supervisord start failed"

        manifest_device = self._ctx.manifest.device(device)
        if manifest_device is None:
            return False, f"device {device!r} is missing from topology manifest"
        ecmp_hash_policy = self._ctx.manifest.routing.ecmp_hash_policy_by_role[manifest_device.role]
        activated, activation_error = activate_device(
            device,
            str(self._ctx.topology_dir),
            self._ctx.manifest.name,
            ecmp_hash_policy,
            readiness_max_tries=self._ACTIVATION_MAX_TRIES,
        )
        if not activated:
            return False, activation_error

        last_error = ""
        for _attempt in range(self._BGP_MAX_TRIES):
            running = self._cmd.container_is_running(container)
            supervisor_ready = running is True and self._sonic.supervisord_ready(container)
            if supervisor_ready and self._sonic.bgp_neighbors_established(device):
                return True, ""
            if running is True:
                bgp_result = self._sonic.vtysh(device, ["show ip bgp summary"])
                last_error = (bgp_result.stderr or bgp_result.stdout or "").strip() or last_error
            elif running is None:
                last_error = "unable to read container running state"
            time.sleep(5)
        return False, last_error or "device did not recover after containerlab node start"

    def inject_device_down(self, device: str) -> dict[str, Any]:
        container = self._ctx.container_names.get(device)
        if not container:
            raise ValueError(f"Unknown device: {device}")

        result = self._cmd.run_cmd(self._containerlab_node_command("stop", device), timeout=60)
        running_state = self._cmd.container_is_running(container)
        success = result.returncode == 0 and running_state is False
        fault_info = {
            "type": "device_down",
            "device": device,
            "container": container,
            "mode": "containerlab_node_stop",
            "success": success,
            "error": (
                None
                if success
                else (result.stderr or result.stdout or "").strip()
                or "container remained running after containerlab node stop"
            ),
        }
        if success:
            self._tracker.track(fault_info)
            return fault_info

        compensated, compensation_error = self._start_and_wait(device, container)
        if not compensated:
            error = "; ".join(
                filter(
                    None,
                    [
                        str(fault_info.get("error") or ""),
                        compensation_error or "device-down compensation failed",
                    ],
                )
            )
            fault_info["error"] = error
            self._tracker.track_residual(fault_info, error)
        return fault_info

    def recover_device_down(self, device: str) -> dict[str, Any]:
        container = self._ctx.container_names.get(device)
        if not container:
            raise ValueError(f"Unknown device: {device}")

        ready, last_error = self._start_and_wait(device, container)

        if ready:
            self._tracker.remove_faults(lambda fault: fault["type"] == "device_down" and fault["device"] == device)

        return {
            "type": "device_down",
            "device": device,
            "recovered": ready,
            "container_running": self._cmd.container_is_running(container),
            "sonic_ready": ready,
            "error": None if ready else last_error,
        }
