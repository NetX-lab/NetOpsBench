"""Versioned benchmark fault taxonomy and necessary coverage units.

Mechanisms are execution details; they never extend the diagnosis contract.
"""

from __future__ import annotations

from dataclasses import dataclass

FAULT_CATALOG_VERSION = 2

# Each singleton uses the type itself as its injection mechanism.
FAULT_MECHANISMS = {
    name: (name,)
    for name in (
        "link_down",
        "link_flapping",
        "interface_ip_misconfig",
        "device_down",
        "bgp_process_down",
        "packet_loss",
        "packet_corruption",
        "high_latency",
        "mtu_mismatch",
        "blackhole_route",
        "static_route_misconfig",
        "routing_loop",
        "bgp_as_mismatch",
        "bgp_auth_mismatch",
        "bgp_update_source_mismatch",
        "bgp_neighbor_shutdown",
        "bgp_max_prefix_misconfig",
        "route_flapping",
        "acl_source_misconfig",
        "acl_destination_misconfig",
        "acl_l4_port_misconfig",
    )
}
FAULT_MECHANISMS.update(
    {
        "route_origination_misconfig": ("network_statement_missing", "origination_policy_deny"),
        "route_filter_misconfig": ("export_filter", "import_filter"),
        "ecmp_misconfig": ("max_paths_limit", "multipath_relax_disabled"),
    }
)


@dataclass(frozen=True)
class CoverageUnit:
    fault_type: str
    role: str
    position: str | None = None
    mechanism: str | None = None
    scope: str | None = None

    @property
    def id(self) -> str:
        return ":".join(
            value for value in (self.fault_type, self.mechanism, self.role, self.position, self.scope) if value
        )


def coverage_units(family: str, *, validation: bool = False) -> tuple[CoverageUnit, ...]:
    """Expand only necessary coverage, independent of the number of switches."""
    if validation:
        return (
            CoverageUnit("link_down", "leaf", "leaf-spine"),
            CoverageUnit("device_down", "spine"),
            CoverageUnit("bgp_auth_mismatch", "leaf", "leaf-spine"),
        )
    if family == "clos":
        access, roles = "leaf", ("leaf", "spine")
        positions = (("leaf", "leaf-client"), ("leaf", "leaf-spine"), ("spine", "spine-leaf"))
    elif family == "fat-tree":
        access, roles = "edge", ("edge", "agg", "core")
        positions = (
            ("edge", "edge-client"),
            ("edge", "edge-agg"),
            ("agg", "agg-edge"),
            ("agg", "agg-core"),
            ("core", "core-agg"),
        )
    else:
        raise ValueError(f"Unsupported coverage family: {family}")
    fabric = positions[1:]
    units = []
    for fault in (
        "link_down",
        "link_flapping",
        "interface_ip_misconfig",
        "packet_loss",
        "packet_corruption",
        "high_latency",
        "mtu_mismatch",
        "acl_source_misconfig",
        "acl_destination_misconfig",
        "acl_l4_port_misconfig",
    ):
        units.extend(CoverageUnit(fault, role, position) for role, position in positions)
    for fault in ("device_down", "bgp_process_down"):
        units.extend(CoverageUnit(fault, role) for role in roles)
    units.extend(CoverageUnit("blackhole_route", access, scope=side) for side in ("source", "destination"))
    units.extend(CoverageUnit("blackhole_route", role) for role in roles[1:])
    units.append(CoverageUnit("static_route_misconfig", access))
    for fault in (
        "routing_loop",
        "bgp_as_mismatch",
        "bgp_auth_mismatch",
        "bgp_update_source_mismatch",
        "bgp_neighbor_shutdown",
        "bgp_max_prefix_misconfig",
        "route_flapping",
    ):
        units.extend(CoverageUnit(fault, role, position) for role, position in fabric)
    units.extend(
        CoverageUnit("route_origination_misconfig", access, mechanism=mechanism)
        for mechanism in FAULT_MECHANISMS["route_origination_misconfig"]
    )
    units.extend(CoverageUnit("route_filter_misconfig", role, position, "export_filter") for role, position in fabric)
    imports = ((access, "missing"), (access, "reduced"), (roles[-1], "missing"))
    if family == "fat-tree":
        imports += (("agg", "missing"), ("agg", "reduced_remote_pod"))
    units.extend(
        CoverageUnit("route_filter_misconfig", role, mechanism="import_filter", scope=scope) for role, scope in imports
    )
    units.append(CoverageUnit("ecmp_misconfig", access, mechanism="max_paths_limit"))
    if family == "fat-tree":
        units.extend(
            CoverageUnit("ecmp_misconfig", role, mechanism=mechanism, scope="remote_pod" if role == "agg" else None)
            for role, mechanism in (
                ("edge", "multipath_relax_disabled"),
                ("agg", "max_paths_limit"),
                ("agg", "multipath_relax_disabled"),
            )
        )
    return tuple(units)
