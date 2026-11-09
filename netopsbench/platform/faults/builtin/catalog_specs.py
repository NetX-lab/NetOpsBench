"""Registry adapters for the explicit configuration fault catalog."""

from __future__ import annotations

import ipaddress
from functools import partial

from ..catalog import FAULT_MECHANISMS
from ..models import FaultSpec

CONFIGURATION_TYPES = {
    "interface_ip_misconfig",
    "bgp_process_down",
    "routing_loop",
    "bgp_as_mismatch",
    "bgp_auth_mismatch",
    "bgp_update_source_mismatch",
    "bgp_neighbor_shutdown",
    "bgp_max_prefix_misconfig",
    "route_origination_misconfig",
    "route_filter_misconfig",
    "route_flapping",
    "ecmp_misconfig",
    "acl_source_misconfig",
    "acl_destination_misconfig",
    "acl_l4_port_misconfig",
}
INTERFACE_TYPES = {
    "interface_ip_misconfig",
    "routing_loop",
    "route_flapping",
    "acl_source_misconfig",
    "acl_destination_misconfig",
    "acl_l4_port_misconfig",
}
PREFIX_TYPES = {
    "routing_loop",
    "route_origination_misconfig",
    "route_filter_misconfig",
    "route_flapping",
    "ecmp_misconfig",
    "acl_source_misconfig",
    "acl_destination_misconfig",
    "acl_l4_port_misconfig",
}


def validate_parameters(fault_type, episode):
    p = episode.parameters or {}
    errors = []
    mechanisms = FAULT_MECHANISMS[fault_type]
    if len(mechanisms) > 1 and p.get("mechanism") not in mechanisms:
        errors.append(f"{fault_type} requires explicit parameters.mechanism in {mechanisms}")
    if len(mechanisms) == 1 and "mechanism" in p and p["mechanism"] not in mechanisms:
        errors.append(f"{fault_type} does not support mechanism {p['mechanism']!r}")
    if "misconfig_kind" in p or "misconfig_kind" in (episode.metadata or {}):
        errors.append("misconfig_kind is obsolete; regenerate the scenario")
    required = []
    if fault_type.startswith("bgp_") and fault_type != "bgp_process_down":
        required.append("peer_ip")
    if fault_type == "routing_loop":
        required.append("next_hop")
    if fault_type in {"route_filter_misconfig", "route_flapping"}:
        if not p.get("peer_ips") and not p.get("peer_ip"):
            errors.append(f"{fault_type} requires parameters.peer_ip or peer_ips")
    for name in required:
        if not p.get(name):
            errors.append(f"{fault_type} requires parameters.{name}")
    for name in ("peer_ip", "next_hop"):
        if p.get(name):
            try:
                ipaddress.IPv4Address(p[name])
            except ValueError:
                errors.append(f"Invalid parameters.{name}")
    if "peer_ips" in p:
        peers = p["peer_ips"]
        if not isinstance(peers, list) or not peers:
            errors.append("parameters.peer_ips must be a nonempty address list")
        else:
            for peer in peers:
                try:
                    ipaddress.IPv4Address(peer)
                except (ValueError, TypeError):
                    errors.append("Invalid parameters.peer_ips address")
    if fault_type.startswith("acl_") and p.get("direction", "in") not in ("in", "out"):
        errors.append("ACL direction must be in or out")
    if fault_type == "acl_l4_port_misconfig":
        try:
            port = int(p.get("destination_port", 33434))
            if not 1 <= port <= 65535:
                raise ValueError
        except (ValueError, TypeError):
            errors.append("destination_port must be in 1..65535")
    return errors


def inject(injector, episode):
    return injector.inject_network_fault(episode)


def recover(injector, fault):
    return injector.recover_network_fault(fault)


def build_catalog_fault_specs():
    return [
        FaultSpec(
            name=name,
            inject_episode=inject,
            recover_active_fault=recover,
            requires_interface=name in INTERFACE_TYPES,
            requires_prefix=name in PREFIX_TYPES,
            episode_validator=partial(validate_parameters, name),
        )
        for name in sorted(CONFIGURATION_TYPES)
    ]
