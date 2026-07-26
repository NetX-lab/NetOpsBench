"""Link fault handlers."""

from __future__ import annotations

import json
import threading
import time
from typing import TYPE_CHECKING, Any

from netopsbench.models.topology import DeviceRole

if TYPE_CHECKING:
    from ..context import FaultRuntimeContext
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
        ctx: FaultRuntimeContext,
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
        state = self._cmd.docker_exec(container, ["ip", "-o", "link", "show", "dev", linux_if])
        flags = ""
        if state.returncode == 0 and "<" in (state.stdout or "") and ">" in (state.stdout or ""):
            flags = (state.stdout or "").split("<", 1)[1].split(">", 1)[0]
        state_matches = state.returncode == 0 and bool(flags) and (("UP" in flags.split(",")) is enabled)
        success = result.returncode == 0 and state_matches
        return {
            "success": success,
            "error": (
                None
                if success
                else (result.stderr or result.stdout or state.stderr or state.stdout or "").strip()
                or f"{device}:{sonic_if} did not reach {'up' if enabled else 'down'} state"
            ),
        }

    def _endpoint(self, device: str, interface: str) -> tuple[str, str, str, str]:
        container = self._ctx.container_names.get(device)
        if not container:
            raise ValueError(f"Unknown device: {device}")
        return device, container, self._iface.resolve_sonic(interface), self._iface.resolve_linux(interface)

    @staticmethod
    def _route_identity(route: dict[str, Any]) -> tuple[str, str, str, str]:
        return (
            str(route.get("dst") or "default"),
            str(route.get("gateway") or ""),
            str(route.get("dev") or ""),
            str(route.get("table") or "main"),
        )

    def _snapshot_client_routes(self, endpoint: tuple[str, str, str, str]) -> list[dict[str, Any]]:
        device, container, _sonic_if, linux_if = endpoint
        manifest_device = self._ctx.manifest.device(device)
        if manifest_device is None or manifest_device.role != DeviceRole.CLIENT:
            return []
        result = self._cmd.docker_exec(container, ["ip", "-j", "route", "show", "dev", linux_if])
        if result.returncode != 0:
            raise RuntimeError(
                (result.stderr or result.stdout or "").strip() or f"failed to snapshot routes for {device}:{linux_if}"
            )
        try:
            routes = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"invalid route snapshot for {device}:{linux_if}: {exc}") from exc
        return [route for route in routes if isinstance(route, dict) and str(route.get("protocol") or "") != "kernel"]

    def _restore_client_routes(
        self,
        endpoint: tuple[str, str, str, str],
        routes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not routes:
            return {"success": True, "error": None}
        device, container, _sonic_if, linux_if = endpoint
        current_result = self._cmd.docker_exec(
            container,
            ["ip", "-j", "route", "show", "dev", linux_if],
        )
        if current_result.returncode != 0:
            return {
                "success": False,
                "error": (current_result.stderr or current_result.stdout or "").strip()
                or f"failed to read routes for {device}:{linux_if}",
            }
        try:
            current = json.loads(current_result.stdout or "[]")
        except json.JSONDecodeError as exc:
            return {"success": False, "error": f"invalid route readback for {device}:{linux_if}: {exc}"}
        current_keys = {self._route_identity(route) for route in current if isinstance(route, dict)}
        errors: list[str] = []
        for route in routes:
            if self._route_identity(route) in current_keys:
                continue
            command = ["ip", "route", "replace", str(route.get("dst") or "default")]
            gateway = str(route.get("gateway") or "")
            if gateway:
                command.extend(["via", gateway])
            command.extend(["dev", linux_if])
            if route.get("metric") is not None:
                command.extend(["metric", str(route["metric"])])
            result = self._cmd.docker_exec(container, command)
            if result.returncode != 0:
                errors.append(
                    (result.stderr or result.stdout or "").strip()
                    or f"failed to restore {route.get('dst') or 'default'}"
                )
        verify = self._cmd.docker_exec(container, ["ip", "-j", "route", "show", "dev", linux_if])
        try:
            restored = json.loads(verify.stdout or "[]") if verify.returncode == 0 else []
        except json.JSONDecodeError:
            restored = []
        restored_keys = {self._route_identity(route) for route in restored if isinstance(route, dict)}
        missing = [route for route in routes if self._route_identity(route) not in restored_keys]
        if missing:
            errors.append(
                "missing route readback: " + ", ".join(str(route.get("dst") or "default") for route in missing)
            )
        return {"success": not errors, "error": "; ".join(errors) or None}

    def _link_endpoints(
        self, device: str, interface: str
    ) -> tuple[tuple[str, str, str, str], tuple[str, str, str, str]]:
        target_linux = self._iface.resolve_linux(interface)
        for link in self._ctx.manifest.links:
            endpoints = link.endpoints
            for index, endpoint in enumerate(endpoints):
                if endpoint.device != device or self._iface.resolve_linux(endpoint.interface) != target_linux:
                    continue
                peer = endpoints[1 - index]
                return self._endpoint(device, endpoint.interface), self._endpoint(peer.device, peer.interface)
        raise ValueError(f"Interface is not a topology link endpoint: {device}:{interface}")

    def _set_physical_link(
        self,
        endpoints: tuple[tuple[str, str, str, str], tuple[str, str, str, str]],
        *,
        enabled: bool,
        route_snapshots: dict[str, list[dict[str, Any]]] | None = None,
    ) -> dict[str, Any]:
        attempted: list[tuple[str, str, str, str]] = []
        errors: list[str] = []
        for endpoint in endpoints:
            attempted.append(endpoint)
            device, container, sonic_if, linux_if = endpoint
            result = self._set_link_admin_state(
                device,
                container,
                sonic_if,
                linux_if,
                enabled=enabled,
            )
            if result["success"]:
                if enabled:
                    restored = self._restore_client_routes(
                        endpoint,
                        (route_snapshots or {}).get(f"{device}:{linux_if}", []),
                    )
                    if not restored["success"]:
                        errors.append(f"{device}:{sonic_if}: {restored['error']}")
                        continue
                continue
            errors.append(f"{device}:{sonic_if}: {result['error'] or 'state change failed'}")
            if not enabled:
                break

        compensation_errors: list[str] = []
        if errors and not enabled:
            # A failed readback can still mean the mutation was applied. Bring
            # every attempted endpoint back up, including the endpoint whose
            # readback failed, before reporting injection failure.
            for device, container, sonic_if, linux_if in reversed(attempted):
                compensation = self._set_link_admin_state(
                    device,
                    container,
                    sonic_if,
                    linux_if,
                    enabled=True,
                )
                if not compensation["success"]:
                    compensation_errors.append(f"compensation {device}:{sonic_if}: {compensation['error']}")
                    continue
                restored = self._restore_client_routes(
                    (device, container, sonic_if, linux_if),
                    (route_snapshots or {}).get(f"{device}:{linux_if}", []),
                )
                if not restored["success"]:
                    compensation_errors.append(f"compensation {device}:{sonic_if}: {restored['error']}")
        all_errors = [*errors, *compensation_errors]
        return {
            "success": not errors,
            "error": "; ".join(all_errors) or None,
            "compensated": bool(errors) and not compensation_errors if not enabled else None,
            "residual_mutation": bool(compensation_errors) if not enabled else False,
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
        endpoints = self._link_endpoints(device, interface)
        route_snapshots = {
            f"{endpoint[0]}:{endpoint[3]}": self._snapshot_client_routes(endpoint) for endpoint in endpoints
        }
        target, peer = endpoints
        _, container, sonic_if, linux_if = target
        result = self._set_physical_link(
            endpoints,
            enabled=False,
            route_snapshots=route_snapshots,
        )

        fault_info = {
            "type": "link_down",
            "device": device,
            "interface": sonic_if,
            "linux_interface": linux_if,
            "container": container,
            "peer_device": peer[0],
            "peer_interface": peer[2],
            "peer_linux_interface": peer[3],
            "route_snapshots": route_snapshots,
            "success": result["success"],
            "error": result["error"],
        }

        if fault_info["success"]:
            self._tracker.track(fault_info)
        elif result.get("residual_mutation"):
            self._tracker.track_residual(
                fault_info,
                str(fault_info.get("error") or "link compensation failed"),
            )

        return fault_info

    def recover_link_down(
        self,
        device: str,
        interface: str,
        *,
        peer_device: str | None = None,
        peer_interface: str | None = None,
        route_snapshots: dict[str, list[dict[str, Any]]] | None = None,
    ) -> dict[str, Any]:
        """Recover from link down by enabling the interface."""
        target, discovered_peer = self._link_endpoints(device, interface)
        peer = self._endpoint(peer_device, peer_interface) if peer_device and peer_interface else discovered_peer
        sonic_if = target[2]
        result = self._set_physical_link(
            (target, peer),
            enabled=True,
            route_snapshots=route_snapshots,
        )

        if result["success"]:
            self._tracker.remove_faults(
                lambda fault: fault["type"] == "link_down"
                and fault["device"] == device
                and fault["interface"] == sonic_if
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
        endpoints = self._link_endpoints(device, interface)
        route_snapshots = {
            f"{endpoint[0]}:{endpoint[3]}": self._snapshot_client_routes(endpoint) for endpoint in endpoints
        }
        target, peer = endpoints
        sonic_if = target[2]
        control_id = f"link-flap:{device}:{sonic_if}:{time.time_ns()}"
        stop_event = threading.Event()
        initial = self._set_physical_link(
            endpoints,
            enabled=False,
            route_snapshots=route_snapshots,
        )
        if not initial["success"]:
            fault_info = {
                "type": "link_flapping",
                "device": device,
                "interface": sonic_if,
                "success": False,
                "error": initial["error"],
            }
            if initial.get("residual_mutation"):
                self._tracker.track_residual(
                    fault_info,
                    str(fault_info.get("error") or "link-flapping compensation failed"),
                )
            return fault_info

        def _run_flap_loop() -> None:
            for index in range(max(int(iterations), 1)):
                if stop_event.wait(max(float(down_time), 0.0)):
                    break
                self._set_physical_link(
                    endpoints,
                    enabled=True,
                    route_snapshots=route_snapshots,
                )
                if stop_event.wait(max(float(up_time), 0.0)):
                    break
                if index + 1 < max(int(iterations), 1):
                    self._set_physical_link(
                        endpoints,
                        enabled=False,
                        route_snapshots=route_snapshots,
                    )
            self._set_physical_link(
                endpoints,
                enabled=True,
                route_snapshots=route_snapshots,
            )
            self._tracker.stop_background(control_id, join_timeout=0)

        thread = threading.Thread(target=_run_flap_loop, name=f"{control_id}-worker", daemon=True)
        self._tracker.register_background_control(control_id, stop_event=stop_event, thread=thread)
        thread.start()

        fault_info = {
            "type": "link_flapping",
            "device": device,
            "interface": sonic_if,
            "linux_interface": target[3],
            "peer_device": peer[0],
            "peer_interface": peer[2],
            "peer_linux_interface": peer[3],
            "route_snapshots": route_snapshots,
            "iterations": iterations,
            "down_time": down_time,
            "up_time": up_time,
            "task_id": control_id,
            "orchestration": "python",
            "success": True,
        }

        self._tracker.track(fault_info)
        return fault_info

    def recover_link_flapping(
        self,
        device: str,
        interface: str,
        *,
        task_id: str | None = None,
        peer_device: str | None = None,
        peer_interface: str | None = None,
        route_snapshots: dict[str, list[dict[str, Any]]] | None = None,
    ) -> dict[str, Any]:
        """Stop link flapping and verify that both physical endpoints recover."""
        stopped = self._tracker.stop_background(task_id, join_timeout=5.0) if task_id else True
        target, discovered_peer = self._link_endpoints(device, interface)
        peer = self._endpoint(peer_device, peer_interface) if peer_device and peer_interface else discovered_peer
        result = self._set_physical_link(
            (target, peer),
            enabled=True,
            route_snapshots=route_snapshots,
        )
        recovered = stopped and result["success"]
        if recovered:
            self._tracker.remove_faults(
                lambda fault: fault["type"] == "link_flapping"
                and fault["device"] == device
                and fault["interface"] == target[2]
            )
        error = result["error"]
        if not stopped:
            error = "; ".join(filter(None, ["failed to stop link flapping task", error]))
        return {
            "type": "link_flapping",
            "device": device,
            "interface": target[2],
            "recovered": recovered,
            "error": error,
        }
