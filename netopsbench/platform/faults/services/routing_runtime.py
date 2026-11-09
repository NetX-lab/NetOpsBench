"""Routing and BGP config parsing helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from netopsbench.platform.utils.frr import parse_bgp_config

if TYPE_CHECKING:
    from ..context import FaultRuntimeContext
    from .sonic_runtime import SonicRuntime


class RoutingRuntime:
    """BGP config parsing, neighbor selection, and route-policy helpers."""

    def __init__(self, sonic: SonicRuntime, ctx: FaultRuntimeContext) -> None:
        self._sonic = sonic
        self._ctx = ctx

    def get_bgp_config_snapshot(self, device: str) -> dict[str, Any]:
        result = self._sonic.vtysh(device, ["show running-config"])
        text = result.stdout if result.returncode == 0 else ""
        return parse_bgp_config(text or "")

    def get_device_asn(self, device: str) -> int | None:
        for entry in self._ctx.manifest.routing_devices():
            if entry.name == device and entry.asn is not None:
                return int(entry.asn)
        snapshot = self.get_bgp_config_snapshot(device)
        if snapshot.get("local_as") is not None:
            return int(snapshot["local_as"])
        return None

    def pick_bgp_neighbor(self, device: str, peer_ip: str) -> dict[str, Any] | None:
        snapshot = self.get_bgp_config_snapshot(device)
        return next((peer for peer in snapshot["neighbors"] if peer["peer_ip"] == peer_ip), None)

    def pick_advertised_network(self, device: str, prefix: str) -> dict[str, Any] | None:
        snapshot = self.get_bgp_config_snapshot(device)
        return next((network for network in snapshot["networks"] if network["prefix"] == prefix), None)

    @staticmethod
    def format_network_statement(prefix: str, route_map: str | None = None) -> str:
        statement = f"network {prefix}"
        if route_map:
            statement += f" route-map {route_map}"
        return statement
