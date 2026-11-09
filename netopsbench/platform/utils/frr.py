"""Parse BGP configuration for scenario generation and live fault injection."""

from __future__ import annotations

import re
from typing import Any


def parse_bgp_config(text: str) -> dict[str, Any]:
    local_as = None
    neighbors = {}
    networks = {}
    for raw in text.splitlines():
        line = raw.strip()
        if match := re.fullmatch(r"router bgp (\d+)", line):
            local_as = int(match[1])
        elif match := re.fullmatch(r"neighbor (\S+) (remote-as|password|update-source) (\S+)", line):
            address, option, value = match.groups()
            neighbor = neighbors.setdefault(address, {"peer_ip": address, "remote_as": None, "route_maps": []})
            if option == "remote-as":
                if value.isdigit():
                    neighbor["remote_as"] = int(value)
            else:
                neighbor[option.replace("-", "_")] = value
        elif match := re.fullmatch(r"neighbor (\S+) route-map (\S+) (in|out)", line):
            address, name, direction = match.groups()
            neighbor = neighbors.setdefault(address, {"peer_ip": address, "remote_as": None, "route_maps": []})
            attachment = {"name": name, "direction": direction}
            if attachment not in neighbor["route_maps"]:
                neighbor["route_maps"].append(attachment)
        elif match := re.fullmatch(r"network (\S+)(?: route-map (\S+))?", line):
            prefix, route_map = match.groups()
            networks[prefix, route_map] = {"prefix": prefix, "route_map": route_map}
    return {
        "local_as": local_as,
        "neighbors": sorted(neighbors.values(), key=lambda peer: peer["peer_ip"]),
        "networks": sorted(networks.values(), key=lambda network: network["prefix"]),
    }
