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

    _CONTAINERLAB_TIMEOUT = "120s"
    _CONTAINERLAB_COMMAND_TIMEOUT_SECONDS = 150
    _STOP_SETTLE_TIMEOUT_SECONDS = 30
    _STOP_SETTLE_POLL_SECONDS = 1
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

    @staticmethod
    def _parking_namespace(container: str) -> str:
        return f"clab-park-{container}"

    def _parking_namespace_exists(self, container: str) -> bool | None:
        # Listing named namespaces is read-only and does not require root.
        # Using ``sudo -n`` here made a healthy parking namespace unreadable on
        # hosts whose sudoers policy grants Containerlab but not arbitrary
        # ``ip`` commands.
        result = self._cmd.run_cmd(["ip", "netns", "list"], timeout=15)
        if result.returncode != 0:
            return None
        expected = self._parking_namespace(container)
        return any(line.split(maxsplit=1)[0] == expected for line in (result.stdout or "").splitlines() if line.strip())

    def _settled_stop_state(self, container: str) -> tuple[bool | None, bool | None]:
        """Wait for Docker's state and Containerlab's parking state to settle."""
        deadline = time.monotonic() + self._STOP_SETTLE_TIMEOUT_SECONDS
        state: bool | None = None
        parking: bool | None = None
        responsive_polls = 0
        while True:
            state = self._cmd.container_is_running(container)
            parking = self._parking_namespace_exists(container)
            if state is False:
                return state, parking
            if state is True and parking is False:
                responsive = self._cmd.docker_exec(container, ["/bin/true"], timeout=10)
                if responsive.returncode == 0:
                    responsive_polls += 1
                    if responsive_polls >= 3:
                        return state, parking
                else:
                    responsive_polls = 0
            else:
                responsive_polls = 0
            if time.monotonic() >= deadline:
                return state, parking
            time.sleep(self._STOP_SETTLE_POLL_SECONDS)

    def _expected_dataplane_interface_count(self, device: str) -> int:
        return sum(
            1 for link in self._ctx.manifest.links if any(endpoint.device == device for endpoint in link.endpoints)
        )

    def _observed_dataplane_interface_count(self, container: str) -> int | None:
        result = self._cmd.docker_exec(
            container,
            [
                "bash",
                "-lc",
                "count=0; for path in /sys/class/net/eth*; do "
                '[ -e "$path" ] || continue; [ "${path##*/}" = eth0 ] && continue; '
                "count=$((count + 1)); done; "
                "printf '%s\\n' \"$count\"",
            ],
            timeout=15,
        )
        if result.returncode != 0:
            return None
        try:
            return int((result.stdout or "").strip())
        except ValueError:
            return None

    def _start_and_wait(self, device: str, container: str) -> tuple[bool, str, bool]:
        running_before = self._cmd.container_is_running(container)
        parking_before = self._parking_namespace_exists(container)
        if running_before is False and parking_before is False:
            return False, "device container is stopped but its parking namespace is missing", False
        if parking_before is None:
            return False, "unable to inspect the device parking namespace", True
        if running_before is None:
            return False, "unable to inspect the device container state", True
        if running_before is True:
            return False, "device container is already running while the device-down fault is active", False

        start = self._cmd.run_cmd(
            self._containerlab_node_command("start", device),
            timeout=self._CONTAINERLAB_COMMAND_TIMEOUT_SECONDS,
        )
        running_state = self._cmd.container_is_running(container)
        parking_after = self._parking_namespace_exists(container)
        if running_state is not True or parking_after is not False:
            detail = (start.stderr or start.stdout or "").strip() or "containerlab node start failed"
            retryable = running_state is False and parking_after is True
            if parking_after is False and running_state is not True:
                retryable = False
                detail = f"{detail}; parking namespace was lost before the container recovered"
            elif parking_after is True and running_state is True:
                retryable = False
                detail = f"{detail}; container is running while dataplane interfaces remain parked"
            return False, detail, retryable

        expected_interfaces = self._expected_dataplane_interface_count(device)
        observed_interfaces = self._observed_dataplane_interface_count(container)
        if observed_interfaces != expected_interfaces:
            return (
                False,
                f"restored dataplane interface count is {observed_interfaces}, expected {expected_interfaces}",
                False,
            )

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
                return (
                    False,
                    (supervisor.stderr or supervisor.stdout or "").strip() or "supervisord start failed",
                    True,
                )

        manifest_device = self._ctx.manifest.device(device)
        if manifest_device is None:
            return False, f"device {device!r} is missing from topology manifest", False
        ecmp_hash_policy = self._ctx.manifest.routing.ecmp_hash_policy_by_role[manifest_device.role]
        activated, activation_error = activate_device(
            device,
            str(self._ctx.topology_dir),
            self._ctx.manifest.name,
            ecmp_hash_policy,
            readiness_max_tries=self._ACTIVATION_MAX_TRIES,
        )
        if not activated:
            return False, activation_error, True

        last_error = ""
        for _attempt in range(self._BGP_MAX_TRIES):
            running = self._cmd.container_is_running(container)
            supervisor_ready = running is True and self._sonic.supervisord_ready(container)
            if supervisor_ready and self._sonic.bgp_neighbors_established(device):
                return True, "", False
            if running is True:
                bgp_result = self._sonic.vtysh(device, ["show ip bgp summary"])
                last_error = (bgp_result.stderr or bgp_result.stdout or "").strip() or last_error
            elif running is None:
                last_error = "unable to read container running state"
            time.sleep(5)
        return False, last_error or "device did not recover after containerlab node start", True

    def inject_device_down(self, device: str) -> dict[str, Any]:
        container = self._ctx.container_names.get(device)
        if not container:
            raise ValueError(f"Unknown device: {device}")

        result = self._cmd.run_cmd(
            self._containerlab_node_command("stop", device),
            timeout=self._CONTAINERLAB_COMMAND_TIMEOUT_SECONDS,
        )
        running_state, parking_exists = self._settled_stop_state(container)
        success = running_state is False and parking_exists is True
        parking_namespace = self._parking_namespace(container)
        if success:
            error = None
        else:
            detail = (result.stderr or result.stdout or "").strip()
            state_detail = (
                f"container_running={running_state!r}, "
                f"parking_namespace={parking_namespace!r}, parking_exists={parking_exists!r}"
            )
            error = "; ".join(filter(None, [detail, state_detail]))
        fault_info = {
            "type": "device_down",
            "device": device,
            "container": container,
            "mode": "containerlab_node_stop",
            "success": success,
            "parking_namespace": parking_namespace,
            "container_running": running_state,
            "parking_exists": parking_exists,
            "management_unavailable": running_state is False,
            "control_plane_unavailable": running_state is False,
            "data_plane_unavailable": parking_exists is True,
            "error": error,
        }
        if success:
            self._tracker.track(fault_info)
            return fault_info

        clean_failure = running_state is True and parking_exists is False
        if not clean_failure:
            fault_info["retryable"] = False
            self._tracker.track_residual(fault_info, str(error or "device-down state is inconsistent"))
        return fault_info

    def recover_device_down(self, device: str) -> dict[str, Any]:
        container = self._ctx.container_names.get(device)
        if not container:
            raise ValueError(f"Unknown device: {device}")

        ready, last_error, retryable = self._start_and_wait(device, container)

        if ready:
            self._tracker.remove_faults(lambda fault: fault["type"] == "device_down" and fault["device"] == device)

        return {
            "type": "device_down",
            "device": device,
            "recovered": ready,
            "container_running": self._cmd.container_is_running(container),
            "sonic_ready": ready,
            "retryable": retryable,
            "error": None if ready else last_error,
        }
