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

    def add_route_state(route: dict[str, Any], raw_text: str) -> None:
        code = str(route.get("code") or "")
        # Detailed FRR output marks an installed/active next hop with ``*``.
        # It does not consistently include the word ``best`` (notably for
        # static routes), so preserve that structured signal while parsing and
        # consume it here rather than making callers inspect raw CLI text.
        active_nexthop = bool(route.pop("_active_nexthop", False))
        route["selected"] = ">" in code or "best" in raw_text.lower() or active_nexthop
        discard_match = re.search(r"\b(Null0|blackhole|reject)\b", raw_text, re.IGNORECASE)
        discard_hop = next(
            (
                hop.get("interface")
                for hop in route.get("nexthops", [])
                if str(hop.get("interface") or "").lower() == "null0"
            ),
            None,
        )
        route["is_discard"] = bool(discard_match or discard_hop)
        route["discard_interface"] = discard_hop or (discard_match.group(1) if discard_match else None)

    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    if lines and lines[0].startswith("Routing entry for "):
        # FRR can print several entries for the same prefix (for example a
        # selected static route followed by a BGP alternative).  Treat each
        # ``Known via`` section as a separate route.  Folding the sections
        # into one mapping lets the last protocol overwrite the selected one
        # and loses the exact route semantics needed by callers.
        prefix = lines[0].split("Routing entry for ", 1)[1].strip()
        detailed: list[dict[str, Any]] = []
        route: dict[str, Any] | None = None
        route_raw: list[str] = []

        def finish_detailed() -> None:
            nonlocal route, route_raw
            if route is None:
                return
            add_route_state(route, "\n".join(route_raw))
            detailed.append(route)
            route = None
            route_raw = []

        for raw in lines[1:]:
            line = raw.strip()
            known_match = re.match(r'^Known via "([^"]+)", distance (\d+), metric (\d+)', line)
            if known_match:
                finish_detailed()
                protocol, distance, metric = known_match.groups()
                route = {
                    "prefix": prefix,
                    "code": None,
                    "protocol": protocol.lower().replace(" ", "_"),
                    "nexthops": [],
                    "admin_distance": int(distance),
                    "metric": int(metric),
                }
                route_raw = [line]
                continue
            if route is None:
                continue
            route_raw.append(line)
            # FRR uses ``*`` for an active next hop and may use lower-case
            # status markers such as ``q`` for a queued FIB install.  A
            # section explicitly marked ``best`` is selected even when its
            # next-hop line uses the latter form.
            status_match = re.match(r"^(?P<status>[*a-z]+)\s+(?P<body>.+)$", line)
            if status_match is None:
                continue
            status = status_match.group("status")
            if "*" in status or "best" in " ".join(route_raw).lower():
                route["_active_nexthop"] = True
            body = status_match.group("body").strip()
            if body.startswith("directly connected"):
                iface_match = re.search(r"directly connected,\s*([^,\s]+)", body)
                route["nexthops"].append({"via": None, "interface": iface_match.group(1) if iface_match else None})
                continue
            nh_match = re.match(r"^([^,\s]+)(?:,\s*via\s+([^,\s]+))?", body)
            if nh_match:
                via, iface = nh_match.groups()
                route["nexthops"].append({"via": via, "interface": iface})
        finish_detailed()
        return detailed
    for raw in text.splitlines():
        if not raw.strip():
            continue
        if raw.startswith(" "):
            if current:
                current["nexthops"].extend(parse_nexthops(raw))
                current["_raw"] = f"{current.get('_raw', '')} {raw.strip()}"
            continue
        # FRR appends lower-case route status flags (for example ``q`` for a
        # queued FIB install) to the protocol/selection code. Rejecting those
        # lines made a selected ``S>q`` route disappear from structured data.
        match = re.match(r"^([A-Za-z*>]+)\s+([0-9.]+/\d+)\s*(.*)$", raw.strip())
        if not match:
            continue
        code, prefix, rest = match.groups()
        route = {
            "prefix": prefix,
            "code": code,
            "protocol": protocol_from_code(code),
            "nexthops": [],
            "_raw": rest,
        }
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
    for route in routes:
        add_route_state(route, str(route.pop("_raw", "")))
    return routes
