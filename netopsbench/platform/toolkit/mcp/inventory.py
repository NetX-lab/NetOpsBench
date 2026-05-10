from .context import as_payload, get_toolkit
from .contracts import ToolSpec


def get_topology():
    """Get network topology information including devices and links."""
    return as_payload(get_toolkit().get_topology())


def get_device_interfaces(device: str, format: str = "summary"):
    """Get interface state and counters for a device."""
    return as_payload(get_toolkit().get_device_interfaces(device=device, format=format))


def get_bgp_neighbors(device: str, format: str = "structured"):
    """Get BGP neighbor status for a network device."""
    return as_payload(get_toolkit().get_bgp_neighbors(device=device, format=format))


def get_route_table(
    device: str,
    prefix: str | None = None,
    format: str = "structured",
    max_routes: int = 100,
    max_lines: int = 500,
):
    """Get route table for a device, optionally filtered by prefix."""
    return as_payload(
        get_toolkit().get_route_table(
            device=device,
            prefix=prefix,
            format=format,
            max_routes=max_routes,
            max_lines=max_lines,
        )
    )


def get_device_config(device: str, section: str = "", max_lines: int = 500):
    """Get running configuration from a SONiC/FRR device.  Optionally filter by section (e.g. 'router bgp', 'interface', 'route-map')."""
    return as_payload(get_toolkit().get_device_config(device=device, section=section, max_lines=max_lines))


def get_bgp_rib(device: str, prefix: str = "", max_lines: int = 500):
    """Get BGP RIB entries showing AS path, origin, next-hop, local-pref and communities. Optionally filter by prefix."""
    return as_payload(get_toolkit().get_bgp_rib(device=device, prefix=prefix or None, max_lines=max_lines))


def get_device_acl(device: str, view: str = "summary", max_lines: int = 300):
    """Get access-list and iptables ACL rules from a SONiC/FRR device. Returns both FRR access-list config and iptables FORWARD chain rules."""
    return as_payload(get_toolkit().get_device_acl(device=device, view=view, max_lines=max_lines))


TOOL_SPECS = [
    ToolSpec(name="get_topology", group="inventory", handler=get_topology),
    ToolSpec(name="get_device_interfaces", group="inventory", handler=get_device_interfaces),
    ToolSpec(name="get_bgp_neighbors", group="inventory", handler=get_bgp_neighbors),
    ToolSpec(name="get_route_table", group="inventory", handler=get_route_table),
    ToolSpec(name="get_device_config", group="inventory", handler=get_device_config),
    ToolSpec(name="get_bgp_rib", group="inventory", handler=get_bgp_rib),
    ToolSpec(name="get_device_acl", group="inventory", handler=get_device_acl),
]
