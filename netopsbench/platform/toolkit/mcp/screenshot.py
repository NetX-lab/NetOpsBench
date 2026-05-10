from .context import as_payload, get_toolkit
from .contracts import ToolSpec


def get_grafana_screenshot(
    panel_name: str,
    time_range: str = "1h",
    width: int = 1000,
    height: int = 500,
    include_base64: bool = False,
):
    """Capture screenshot for one Grafana panel."""
    return as_payload(
        get_toolkit().get_grafana_screenshot(
            panel_name=panel_name,
            time_range=time_range,
            width=width,
            height=height,
            include_base64=include_base64,
        )
    )


def get_dashboard_screenshot(
    time_range: str = "1h",
    width: int = 1920,
    height: int = 1080,
    include_base64: bool = False,
):
    """Capture screenshot for full dashboard."""
    return as_payload(
        get_toolkit().get_dashboard_screenshot(
            time_range=time_range,
            width=width,
            height=height,
            include_base64=include_base64,
        )
    )


def get_troubleshooting_screenshots(time_range: str = "30m", include_base64: bool = False):
    """Capture the standard troubleshooting screenshot set."""
    return as_payload(
        get_toolkit().get_troubleshooting_screenshots(
            time_range=time_range,
            include_base64=include_base64,
        )
    )


TOOL_SPECS = [
    ToolSpec(name="get_grafana_screenshot", group="screenshot", handler=get_grafana_screenshot),
    ToolSpec(name="get_dashboard_screenshot", group="screenshot", handler=get_dashboard_screenshot),
    ToolSpec(name="get_troubleshooting_screenshots", group="screenshot", handler=get_troubleshooting_screenshots),
]
