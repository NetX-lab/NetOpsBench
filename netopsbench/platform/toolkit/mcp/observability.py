from .context import as_payload, get_toolkit
from .contracts import ToolSpec


def get_device_logs(
    device: str,
    time_range_minutes: int = 30,
    severity: str = None,
    include_raw: bool = False,
):
    """Get device logs from InfluxDB."""
    return as_payload(
        get_toolkit().get_device_logs(
            device=device,
            time_range_minutes=time_range_minutes,
            severity=severity,
            include_raw=include_raw,
        )
    )


def get_interface_metrics(
    device: str,
    interface: str,
    time_range_minutes: int = 30,
    metric_type: str = "all",
    view: str = "summary",
    max_points: int = 120,
):
    """Get interface metrics (throughput/errors/discards/phy)."""
    return as_payload(
        get_toolkit().get_interface_metrics(
            device=device,
            interface=interface,
            time_range_minutes=time_range_minutes,
            metric_type=metric_type,
            view=view,
            max_points=max_points,
        )
    )


def get_all_bgp_status():
    """Get BGP status summary across all devices."""
    return as_payload(get_toolkit().get_all_bgp_status())


def get_pingmesh_summary(
    time_range_minutes: int = 10,
    start_time: str = "",
    end_time: str = "",
):
    """Get Pingmesh summary by path type."""
    return as_payload(
        get_toolkit().get_pingmesh_summary(
            time_range_minutes=time_range_minutes,
            start_time=start_time or None,
            end_time=end_time or None,
        )
    )


def get_pingmesh_hotspots(
    time_range_minutes: int = 10,
    limit: int = 10,
    start_time: str = "",
    end_time: str = "",
):
    """Get worst Pingmesh hotspots by latency/loss."""
    return as_payload(
        get_toolkit().get_pingmesh_hotspots(
            time_range_minutes=time_range_minutes,
            limit=limit,
            start_time=start_time or None,
            end_time=end_time or None,
        )
    )


TOOL_SPECS = [
    ToolSpec(name="get_device_logs", group="observability", handler=get_device_logs),
    ToolSpec(name="get_interface_metrics", group="observability", handler=get_interface_metrics),
    ToolSpec(name="get_all_bgp_status", group="observability", handler=get_all_bgp_status),
    ToolSpec(name="get_pingmesh_summary", group="observability", handler=get_pingmesh_summary),
    ToolSpec(name="get_pingmesh_hotspots", group="observability", handler=get_pingmesh_hotspots),
]
