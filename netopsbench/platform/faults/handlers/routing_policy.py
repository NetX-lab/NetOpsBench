"""Route-policy and network origination fault handlers."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..context import FaultRuntimeContext
    from ..services.routing_runtime import RoutingRuntime
    from ..services.sonic_runtime import SonicRuntime
    from ..services.tracking import FaultTracker


class RoutePolicyHandler:
    """Handles route-policy / network-origination fault injection and recovery."""

    def __init__(
        self,
        sonic: SonicRuntime,
        routing: RoutingRuntime,
        tracker: FaultTracker,
        ctx: FaultRuntimeContext,
    ) -> None:
        self._sonic = sonic
        self._routing = routing
        self._tracker = tracker
        self._ctx = ctx

    def _running_config(self, device: str) -> str | None:
        result = self._sonic.vtysh(device, ["show running-config"])
        if result.returncode != 0:
            return None
        return result.stdout or ""

    def _bgp_prefix_present(self, device: str, prefix: str) -> bool | None:
        result = self._sonic.vtysh(device, [f"show ip bgp {prefix}"])
        if result.returncode != 0:
            return None
        output = result.stdout or ""
        lowered = output.lower()
        if "network not in table" in lowered or "not found" in lowered:
            return False
        return prefix in output

    def _wait_for_bgp_prefix(self, device: str, prefix: str, *, present: bool) -> bool:
        for attempt in range(10):
            state = self._bgp_prefix_present(device, prefix)
            if state is present:
                return True
            if attempt < 9:
                time.sleep(1)
        return False

    def _track_failed_compensation(
        self,
        fault_info: dict[str, Any],
        rollback: dict[str, Any],
    ) -> None:
        if rollback.get("recovered") is True:
            return
        error = "; ".join(
            filter(
                None,
                [
                    str(fault_info.get("error") or ""),
                    str(rollback.get("error") or "route-policy compensation failed"),
                ],
            )
        )
        fault_info["error"] = error
        self._tracker.track_residual(fault_info, error)

    def inject_route_policy_misconfig(
        self,
        device: str,
        target_prefix: str | None = None,
        misconfig_kind: str = "network_statement_missing",
        route_map: str | None = None,
    ) -> dict[str, Any]:
        """Inject a route-policy/origination error using real FRR policy state."""
        container = self._ctx.container_names.get(device)
        if not container:
            raise ValueError(f"Unknown device: {device}")

        misconfig_kind = self._routing.normalize_route_policy_kind(misconfig_kind)
        network = self._routing.pick_advertised_network(device, prefix=target_prefix)
        if not network:
            raise RuntimeError(
                f"Unable to determine advertised network from device config: device={device} prefix={target_prefix}"
            )

        prefix = str(network["prefix"])
        local_as = network.get("local_as") or self._routing.get_device_asn(device)
        if local_as is None:
            raise RuntimeError(f"Unable to determine local BGP ASN for target device: device={device}")

        effective_route_map = route_map or network.get("route_map")
        fault_info: dict[str, Any] = {
            "type": "route_policy_misconfig",
            "device": device,
            "target_prefix": prefix,
            "local_as": int(local_as),
            "misconfig_kind": misconfig_kind,
            "route_map": effective_route_map,
            "success": False,
            "error": None,
        }

        if misconfig_kind == "network_statement_missing":
            network_statement = self._routing.format_network_statement(prefix, effective_route_map)
            commands = [
                "configure terminal",
                f"router bgp {local_as}",
                "address-family ipv4 unicast",
                f"no {network_statement}",
                "exit-address-family",
                "end",
                "write memory",
            ]
            fault_info["network_statement"] = network_statement
        elif misconfig_kind == "route_map_deny_prefix":
            route_map_name = effective_route_map or "RM-ALLOW"
            prefix_list_name = self._routing.make_policy_artifact_name(device, prefix)
            sequence = 5
            commands = [
                "configure terminal",
                f"ip prefix-list {prefix_list_name} seq 5 permit {prefix}",
                f"route-map {route_map_name} deny {sequence}",
                f"match ip address prefix-list {prefix_list_name}",
                "exit",
                "end",
                "write memory",
            ]
            fault_info["route_map"] = route_map_name
            fault_info["prefix_list_name"] = prefix_list_name
            fault_info["sequence"] = sequence
        else:
            fault_info["error"] = f"Unsupported route_policy misconfig_kind: {misconfig_kind}"
            return fault_info

        result = self._sonic.vtysh(device, commands)
        running = self._running_config(device)
        if misconfig_kind == "network_statement_missing":
            state_matches = (
                running is not None
                and network_statement not in running
                and self._wait_for_bgp_prefix(device, prefix, present=False)
            )
        else:
            state_matches = (
                running is not None
                and f"route-map {fault_info['route_map']} deny {fault_info['sequence']}" in running
                and f"ip prefix-list {fault_info['prefix_list_name']}" in running
                and self._wait_for_bgp_prefix(device, prefix, present=True)
            )
        fault_info["success"] = result.returncode == 0 and state_matches
        fault_info["error"] = (
            None
            if fault_info["success"]
            else result.stderr or "route policy state did not match the injected configuration"
        )

        if fault_info["success"]:
            self._tracker.track(fault_info)
            return fault_info

        rollback = self.recover_route_policy_misconfig(
            device,
            prefix,
            misconfig_kind,
            route_map=fault_info.get("route_map"),
            network_statement=fault_info.get("network_statement"),
            prefix_list_name=fault_info.get("prefix_list_name"),
            sequence=fault_info.get("sequence"),
        )
        self._track_failed_compensation(fault_info, rollback)
        return fault_info

    def recover_route_policy_misconfig(
        self,
        device: str,
        target_prefix: str,
        misconfig_kind: str,
        route_map: str | None = None,
        network_statement: str | None = None,
        prefix_list_name: str | None = None,
        sequence: int | None = None,
    ) -> dict[str, Any]:
        """Recover one route-policy/origination error."""
        container = self._ctx.container_names.get(device)
        if not container:
            raise ValueError(f"Unknown device: {device}")

        local_as = self._routing.get_device_asn(device)
        if local_as is None:
            raise ValueError(f"Unable to determine local BGP ASN for {device}")

        misconfig_kind = self._routing.normalize_route_policy_kind(misconfig_kind)
        if misconfig_kind == "network_statement_missing":
            statement = network_statement or self._routing.format_network_statement(target_prefix, route_map)
            commands = [
                "configure terminal",
                f"router bgp {local_as}",
                "address-family ipv4 unicast",
                statement,
                "exit-address-family",
                "end",
                "write memory",
            ]
        elif misconfig_kind == "route_map_deny_prefix":
            if not route_map:
                raise ValueError("recover_route_policy_misconfig requires route_map")
            if not prefix_list_name:
                prefix_list_name = self._routing.make_policy_artifact_name(device, target_prefix)
            commands = [
                "configure terminal",
                f"no route-map {route_map} deny {int(sequence or 5)}",
                f"no ip prefix-list {prefix_list_name}",
                "end",
                "write memory",
            ]
        else:
            raise ValueError(f"Unsupported route_policy misconfig_kind: {misconfig_kind}")

        result = self._sonic.vtysh(device, commands)
        running = self._running_config(device)
        if misconfig_kind == "network_statement_missing":
            recovered = (
                result.returncode == 0
                and running is not None
                and statement in running
                and self._wait_for_bgp_prefix(device, target_prefix, present=True)
            )
        else:
            recovered = (
                result.returncode == 0
                and running is not None
                and (
                    f"route-map {route_map} deny {int(sequence or 5)}" not in running
                    and f"ip prefix-list {prefix_list_name}" not in running
                    and self._wait_for_bgp_prefix(device, target_prefix, present=True)
                )
            )
        if recovered:
            self._tracker.remove_faults(
                lambda fault: fault["type"] == "route_policy_misconfig"
                and fault["device"] == device
                and fault.get("target_prefix") == target_prefix
            )

        return {
            "type": "route_policy_misconfig",
            "device": device,
            "target_prefix": target_prefix,
            "misconfig_kind": misconfig_kind,
            "recovered": recovered,
            "error": None if recovered else result.stderr or "route policy recovery was not observed",
        }
