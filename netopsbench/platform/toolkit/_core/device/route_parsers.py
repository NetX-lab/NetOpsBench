"""Route parsing helpers for device toolkit internals."""

from __future__ import annotations

import re
from typing import Any


def parse_route_table(text: str) -> list[dict[str, Any]]:
    if not text:
        return []
    if "Network not in table" in text:
        return []
    routes: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    def protocol_from_code(code: str) -> str:
        if not code:
            return "unknown"
        return {"B": "bgp", "C": "connected", "S": "static", "O": "ospf", "R": "rip", "K": "kernel", "L": "local"}.get(
            code[0], "other"
        )

    def parse_nexthops(rest: str) -> list[dict[str, str | None]]:
        hops: list[dict[str, str | None]] = []
        for match in re.finditer(r"via\s+([^,\s]+)(?:,\s*([^,\s]+))?", rest):
            via = match.group(1)
            iface = match.group(2)
            if iface == "weight":
                iface = None
            hops.append({"via": via, "interface": iface})
        return hops

    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    if lines and lines[0].startswith("Routing entry for "):
        prefix = lines[0].split("Routing entry for ", 1)[1].strip()
        route: dict[str, Any] = {"prefix": prefix, "code": None, "protocol": "unknown", "nexthops": []}
        for raw in lines[1:]:
            line = raw.strip()
            known_match = re.match(r'^Known via "([^"]+)", distance (\d+), metric (\d+)', line)
            if known_match:
                protocol, distance, metric = known_match.groups()
                route["protocol"] = protocol.lower().replace(" ", "_")
                route["admin_distance"] = int(distance)
                route["metric"] = int(metric)
                continue
            if not line.startswith("*"):
                continue
            line = line.lstrip("* ").strip()
            if line.startswith("directly connected"):
                iface_match = re.search(r"directly connected,\s*([^,\s]+)", line)
                route["nexthops"].append({"via": None, "interface": iface_match.group(1) if iface_match else None})
                continue
            nh_match = re.match(r"^([^,\s]+)(?:,\s*via\s+([^,\s]+))?", line)
            if nh_match:
                via, iface = nh_match.groups()
                route["nexthops"].append({"via": via, "interface": iface})
        return [route]
    for raw in text.splitlines():
        if not raw.strip():
            continue
        if raw.startswith(" "):
            if current:
                current["nexthops"].extend(parse_nexthops(raw))
            continue
        match = re.match(r"^([A-Z*>]+)\s+([0-9.]+/\d+)\s*(.*)$", raw.strip())
        if not match:
            continue
        code, prefix, rest = match.groups()
        route = {"prefix": prefix, "code": code, "protocol": protocol_from_code(code), "nexthops": []}
        metric_match = re.search(r"\[(\d+)/(\d+)\]", rest)
        if metric_match:
            route["admin_distance"] = int(metric_match.group(1))
            route["metric"] = int(metric_match.group(2))
        if "directly connected" in rest:
            iface_match = re.search(r"directly connected,\s*([^,\s]+)", rest)
            if iface_match:
                route["nexthops"].append({"via": None, "interface": iface_match.group(1)})
        route["nexthops"].extend(parse_nexthops(rest))
        routes.append(route)
        current = route
    return routes
