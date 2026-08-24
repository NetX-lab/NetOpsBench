from .context import as_payload, get_toolkit
from .contracts import ToolSpec


def traceroute(src: str, dst_ip: str):
    """Run traceroute from a client device to a destination IP."""
    return as_payload(get_toolkit().traceroute(src=src, dst_ip=dst_ip))


def ping_test(
    src: str,
    dst_ip: str,
    count: int = 5,
    payload_size: int | None = None,
    dont_fragment: bool = False,
    source_interface: str | None = None,
):
    """Run ping test from any topology device to a destination IP."""
    return as_payload(
        get_toolkit().ping_test(
            src=src,
            dst_ip=dst_ip,
            count=count,
            payload_size=payload_size,
            dont_fragment=dont_fragment,
            source_interface=source_interface,
        )
    )


def payload_integrity_test(src: str, dst_ip: str, count: int = 5):
    """Send bounded payloads and inspect destination-ingress ICMP checksums."""
    return as_payload(get_toolkit().payload_integrity_test(src=src, dst_ip=dst_ip, count=count))


def payload_integrity_link_test(
    device_a: str,
    interface_a: str,
    device_b: str,
    interface_b: str,
    count: int = 20,
):
    """Check payload integrity bidirectionally on a topology-validated link."""
    return as_payload(
        get_toolkit().payload_integrity_link_test(
            device_a=device_a,
            interface_a=interface_a,
            device_b=device_b,
            interface_b=interface_b,
            count=count,
        )
    )


def ping_link_test(
    src: str,
    target_device: str,
    source_interface: str,
    target_interface: str,
    count: int = 5,
    payload_size: int | None = None,
    dont_fragment: bool = False,
):
    """Ping a topology-validated peer interface to isolate one physical link."""
    return as_payload(
        get_toolkit().ping_link_test(
            src=src,
            target_device=target_device,
            source_interface=source_interface,
            target_interface=target_interface,
            count=count,
            payload_size=payload_size,
            dont_fragment=dont_fragment,
        )
    )


def latency_link_test(
    device_a: str,
    interface_a: str,
    device_b: str,
    interface_b: str,
    count: int = 7,
):
    """Measure one-way latency in both directions on a validated physical link."""
    return as_payload(
        get_toolkit().latency_link_test(
            device_a=device_a,
            interface_a=interface_a,
            device_b=device_b,
            interface_b=interface_b,
            count=count,
        )
    )


TOOL_SPECS = [
    ToolSpec(name="traceroute", group="connectivity", handler=traceroute),
    ToolSpec(name="ping_test", group="connectivity", handler=ping_test),
    ToolSpec(name="ping_link_test", group="connectivity", handler=ping_link_test),
    ToolSpec(name="latency_link_test", group="connectivity", handler=latency_link_test),
    ToolSpec(name="payload_integrity_test", group="connectivity", handler=payload_integrity_test),
    ToolSpec(name="payload_integrity_link_test", group="connectivity", handler=payload_integrity_link_test),
]
