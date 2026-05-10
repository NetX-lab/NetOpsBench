"""Routing and BGP config parsing helpers."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..context import FaultContext
    from .topology_runtime import TopologyRuntime


class RoutingRuntime:
    """BGP config parsing, neighbor selection, and route-policy helpers."""

    def __init__(self, topo_rt: TopologyRuntime, ctx: FaultContext) -> None:
        self._topo_rt = topo_rt
        self._ctx = ctx

    def get_bgp_config_snapshot(self, device: str) -> dict[str, Any]:
        local_as = None
        neighbors: dict[str, dict[str, Any]] = {}
        networks: list[dict[str, Any]] = []
        seen_networks = set()

        for raw_line in self._topo_rt.load_device_config_lines(device):
            line = raw_line.strip()

            match = re.match(r"^router bgp (\d+)$", line)
            if match:
                local_as = int(match.group(1))
                continue

            match = re.match(r"^neighbor (\S+) remote-as (\d+)$", line)
            if match:
                peer_ip, remote_as = match.groups()
                peer = neighbors.setdefault(
                    peer_ip,
                    {"peer_ip": peer_ip, "remote_as": None, "password": None, "update_source": None, "route_maps": []},
                )
                peer["remote_as"] = int(remote_as)
                continue

            match = re.match(r"^neighbor (\S+) password (\S+)$", line)
            if match:
                peer_ip, password = match.groups()
                peer = neighbors.setdefault(
                    peer_ip,
                    {"peer_ip": peer_ip, "remote_as": None, "password": None, "update_source": None, "route_maps": []},
                )
                peer["password"] = password
                continue

            match = re.match(r"^neighbor (\S+) update-source (\S+)$", line)
            if match:
                peer_ip, update_source = match.groups()
                peer = neighbors.setdefault(
                    peer_ip,
                    {"peer_ip": peer_ip, "remote_as": None, "password": None, "update_source": None, "route_maps": []},
                )
                peer["update_source"] = update_source
                continue

            match = re.match(r"^neighbor (\S+) route-map (\S+) (in|out)$", line)
            if match:
                peer_ip, route_map, direction = match.groups()
                peer = neighbors.setdefault(
                    peer_ip,
                    {"peer_ip": peer_ip, "remote_as": None, "password": None, "update_source": None, "route_maps": []},
                )
                attachment = {"name": route_map, "direction": direction}
                if attachment not in peer["route_maps"]:
                    peer["route_maps"].append(attachment)
                continue

            match = re.match(r"^network (\S+)(?: route-map (\S+))?$", line)
            if match:
                prefix, route_map = match.groups()
                identity = (prefix, route_map or "")
                if identity in seen_networks:
                    continue
                seen_networks.add(identity)
                networks.append({"prefix": prefix, "route_map": route_map})

        return {
            "local_as": local_as,
            "neighbors": sorted(neighbors.values(), key=lambda item: item["peer_ip"]),
            "networks": sorted(networks, key=lambda item: item["prefix"]),
        }

    def get_device_asn(self, device: str) -> int | None:
        devices = (self._ctx.topology_metadata or {}).get("devices", {})
        for group in ("spines", "leafs"):
            for entry in devices.get(group, []):
                if entry.get("name") == device and entry.get("asn") is not None:
                    return int(entry["asn"])
        snapshot = self.get_bgp_config_snapshot(device)
        if snapshot.get("local_as") is not None:
            return int(snapshot["local_as"])
        return None

    @staticmethod
    def normalize_bgp_neighbor_kind(misconfig_kind: str | None) -> str:
        normalized = str(misconfig_kind or "peer_as_mismatch").strip().lower().replace("-", "_")
        aliases = {
            "peer_as": "peer_as_mismatch",
            "remote_as_mismatch": "peer_as_mismatch",
            "auth_mismatch": "password_mismatch",
            "md5_mismatch": "password_mismatch",
            "update_source": "update_source_mismatch",
        }
        return aliases.get(normalized, normalized)

    @staticmethod
    def normalize_route_policy_kind(misconfig_kind: str | None) -> str:
        normalized = str(misconfig_kind or "network_statement_missing").strip().lower().replace("-", "_")
        aliases = {
            "route_origination_missing": "network_statement_missing",
            "outbound_prefix_filter": "route_map_deny_prefix",
            "prefix_filter_misconfig": "route_map_deny_prefix",
        }
        return aliases.get(normalized, normalized)

    def pick_bgp_neighbor(self, device: str, peer_ip: str | None = None) -> dict[str, Any] | None:
        snapshot = self.get_bgp_config_snapshot(device)
        neighbors = list(snapshot.get("neighbors") or [])
        if peer_ip:
            for neighbor in neighbors:
                if neighbor.get("peer_ip") == peer_ip:
                    return {"local_as": snapshot.get("local_as"), **neighbor}
            return None
        if not neighbors:
            return None
        selected = sorted(neighbors, key=lambda item: item.get("peer_ip", ""))[0]
        return {"local_as": snapshot.get("local_as"), **selected}

    def pick_advertised_network(self, device: str, prefix: str | None = None) -> dict[str, Any] | None:
        snapshot = self.get_bgp_config_snapshot(device)
        networks = list(snapshot.get("networks") or [])
        if prefix:
            for network in networks:
                if network.get("prefix") == prefix:
                    return {"local_as": snapshot.get("local_as"), **network}
            return None
        if not networks:
            return None
        selected = sorted(networks, key=lambda item: item.get("prefix", ""))[0]
        return {"local_as": snapshot.get("local_as"), **selected}

    @staticmethod
    def make_policy_artifact_name(device: str, prefix: str) -> str:
        raw = f"netopsbench_{device}_{prefix}"
        token = re.sub(r"[^A-Za-z0-9]+", "_", raw).strip("_").upper()
        return token[:60] or "NETOPSBENCH_ROUTE_POLICY"

    @staticmethod
    def format_network_statement(prefix: str, route_map: str | None = None) -> str:
        statement = f"network {prefix}"
        if route_map:
            statement += f" route-map {route_map}"
        return statement
