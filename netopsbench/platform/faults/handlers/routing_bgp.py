"""BGP neighbor routing fault handlers."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..context import FaultContext
    from ..services.routing_runtime import RoutingRuntime
    from ..services.sonic_runtime import SonicRuntime
    from ..services.tracking import FaultTracker


class BgpHandler:
    """Handles BGP neighbor misconfiguration fault injection and recovery."""

    def __init__(
        self,
        sonic: SonicRuntime,
        routing: RoutingRuntime,
        tracker: FaultTracker,
        ctx: FaultContext,
    ) -> None:
        self._sonic = sonic
        self._routing = routing
        self._tracker = tracker
        self._ctx = ctx

    def inject_bgp_neighbor_misconfig(
        self,
        device: str,
        peer_ip: str | None = None,
        misconfig_kind: str = "peer_as_mismatch",
        wrong_remote_as: int | None = None,
        password: str | None = None,
        update_source: str | None = None,
    ) -> dict[str, Any]:
        """Inject a realistic BGP neighbor configuration error on one device."""
        container = self._ctx.container_names.get(device)
        if not container:
            raise ValueError(f"Unknown device: {device}")

        misconfig_kind = self._routing.normalize_bgp_neighbor_kind(misconfig_kind)
        neighbor = self._routing.pick_bgp_neighbor(device, peer_ip=peer_ip)
        if not neighbor:
            raise RuntimeError(
                f"Unable to determine target BGP neighbor from device config: device={device} peer_ip={peer_ip}"
            )

        peer_ip = str(neighbor["peer_ip"])
        local_as = neighbor.get("local_as") or self._routing.get_device_asn(device)
        if local_as is None:
            raise RuntimeError(f"Unable to determine local BGP ASN for target device: device={device}")

        commands = ["configure terminal", f"router bgp {local_as}"]
        fault_info = {
            "type": "bgp_neighbor_misconfig",
            "device": device,
            "peer_ip": peer_ip,
            "local_as": int(local_as),
            "misconfig_kind": misconfig_kind,
            "success": False,
            "error": None,
        }

        if misconfig_kind == "peer_as_mismatch":
            original_remote_as = neighbor.get("remote_as")
            if original_remote_as is None:
                fault_info["error"] = "Unable to determine original remote-as for neighbor"
                return fault_info
            wrong_as = int(wrong_remote_as or (int(original_remote_as) + 1000))
            if wrong_as == int(original_remote_as):
                wrong_as += 1000
            commands.append(f"neighbor {peer_ip} remote-as {wrong_as}")
            fault_info["original_remote_as"] = int(original_remote_as)
            fault_info["wrong_remote_as"] = wrong_as
        elif misconfig_kind == "password_mismatch":
            bad_password = str(password or f"netopsbench-{int(time.time())}")
            commands.append(f"neighbor {peer_ip} password {bad_password}")
            fault_info["original_password"] = neighbor.get("password")
            fault_info["bad_password"] = bad_password
        elif misconfig_kind == "update_source_mismatch":
            bad_update_source = str(update_source or "Loopback0")
            commands.append(f"neighbor {peer_ip} update-source {bad_update_source}")
            fault_info["original_update_source"] = neighbor.get("update_source")
            fault_info["bad_update_source"] = bad_update_source
        else:
            fault_info["error"] = f"Unsupported bgp_neighbor misconfig_kind: {misconfig_kind}"
            return fault_info

        commands.extend(["end", "write memory"])
        result = self._sonic.vtysh(device, commands)
        fault_info["success"] = result.returncode == 0
        fault_info["error"] = result.stderr if result.returncode != 0 else None

        if fault_info["success"]:
            self._tracker.track(fault_info)

        return fault_info

    def recover_bgp_neighbor_misconfig(
        self,
        device: str,
        peer_ip: str,
        misconfig_kind: str,
        original_remote_as: int | None = None,
        original_password: str | None = None,
        original_update_source: str | None = None,
        wrong_remote_as: int | None = None,
        bad_update_source: str | None = None,
    ) -> dict[str, Any]:
        """Recover one BGP neighbor configuration error."""
        container = self._ctx.container_names.get(device)
        if not container:
            raise ValueError(f"Unknown device: {device}")

        local_as = self._routing.get_device_asn(device)
        if local_as is None:
            raise ValueError(f"Unable to determine local BGP ASN for {device}")

        misconfig_kind = self._routing.normalize_bgp_neighbor_kind(misconfig_kind)
        commands = ["configure terminal", f"router bgp {local_as}"]
        if misconfig_kind == "peer_as_mismatch":
            if original_remote_as is None:
                raise ValueError("recover_bgp_neighbor_misconfig requires original_remote_as")
            commands.append(f"neighbor {peer_ip} remote-as {int(original_remote_as)}")
        elif misconfig_kind == "password_mismatch":
            if original_password:
                commands.append(f"neighbor {peer_ip} password {original_password}")
            else:
                commands.append(f"no neighbor {peer_ip} password")
        elif misconfig_kind == "update_source_mismatch":
            if original_update_source:
                commands.append(f"neighbor {peer_ip} update-source {original_update_source}")
            else:
                commands.append(f"no neighbor {peer_ip} update-source {bad_update_source or 'Loopback0'}")
        else:
            raise ValueError(f"Unsupported bgp_neighbor misconfig_kind: {misconfig_kind}")

        commands.extend(["end", "write memory"])
        result = self._sonic.vtysh(device, commands)

        self._tracker.remove_faults(
            lambda fault: fault["type"] == "bgp_neighbor_misconfig"
            and fault["device"] == device
            and fault.get("peer_ip") == peer_ip
        )

        return {
            "type": "bgp_neighbor_misconfig",
            "device": device,
            "peer_ip": peer_ip,
            "misconfig_kind": misconfig_kind,
            "wrong_remote_as": wrong_remote_as,
            "recovered": result.returncode == 0,
            "error": result.stderr if result.returncode != 0 else None,
        }
