from .context import as_payload, get_toolkit
from .contracts import ToolSpec


def traceroute(src: str, dst_ip: str):
    """Run traceroute from any network device (client, leaf, or spine) to a destination IP."""
    return as_payload(get_toolkit().traceroute(src=src, dst_ip=dst_ip))


def ping_test(
    src: str,
    dst_ip: str,
    count: int = 5,
    payload_size: int = None,
    dont_fragment: bool = False,
):
    """Run ping test from any network device (client, leaf, or spine) to a destination IP."""
    return as_payload(
        get_toolkit().ping_test(
            src=src,
            dst_ip=dst_ip,
            count=count,
            payload_size=payload_size,
            dont_fragment=dont_fragment,
        )
    )


TOOL_SPECS = [
    ToolSpec(name="traceroute", group="connectivity", handler=traceroute),
    ToolSpec(name="ping_test", group="connectivity", handler=ping_test),
]
